param(
    [Parameter(Mandatory=$true)][string]$OperationPath,
    [int]$ParentId = 0,
    [long]$ParentStartTicks = 0,
    [switch]$Recover,
    [switch]$NoRestart,
    [int]$StartupTimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$utf8 = [System.Text.UTF8Encoding]::new($false)
$operationFile = (Resolve-Path -LiteralPath $OperationPath).Path
$op = Get-Content -LiteralPath $operationFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($op.format -ne 1 -or $op.id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid update operation.' }
$root = [System.IO.Path]::GetFullPath($op.root).TrimEnd('\')
if ($root -eq [System.IO.Path]::GetPathRoot($root).TrimEnd('\')) { throw 'A drive root cannot be updated.' }
$stage = [System.IO.Path]::GetFullPath((Join-Path $root ('temp/hif-updates/' + $op.id)))
$backup = [System.IO.Path]::GetFullPath((Join-Path $root ('backup/hif-update-' + $op.id)))
$candidate = [System.IO.Path]::GetFullPath((Join-Path $stage 'extracted/MaaGakumasu-HIF'))
if ($op.stage -ne $stage -or $op.backup -ne $backup -or $op.candidate -ne $candidate -or
    $operationFile -ne (Join-Path $stage 'operation.json')) { throw 'Update operation paths do not match the runtime.' }

function Target-Path([string]$Base, [string]$Name) {
    $path = $Base
    foreach ($part in $Name.Split('/')) {
        if (-not $part -or $part -in @('.', '..') -or $part -match '[\\:*?"<>|\x00-\x1f]' -or
            $part.EndsWith('.') -or $part.EndsWith(' ') -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') { throw "Invalid update path: $Name" }
        $path = [System.IO.Path]::Combine($path, $part)
    }
    $path = [System.IO.Path]::GetFullPath($path)
    $current = $path
    while ($current) {
        if (([System.IO.File]::Exists($current) -or [System.IO.Directory]::Exists($current)) -and
            ([System.IO.File]::GetAttributes($current) -band [System.IO.FileAttributes]::ReparsePoint)) { throw "Linked update path: $current" }
        $current = [System.IO.Path]::GetDirectoryName($current)
    }
    return $path
}
function Hash-File([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}
$null = Target-Path $root 'temp/hif-update.lock'
$lock = [System.IO.File]::Open((Join-Path $root 'temp/hif-update.lock'), [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
$journal = Join-Path $backup 'changes.jsonl'
$newProcess = $null

function Save-State([string]$State, [string]$ErrorText = '') {
    $op.state = $State
    $op | Add-Member -Force NoteProperty error $ErrorText
    $text = $op | ConvertTo-Json -Depth 10
    $temporary = $operationFile + '.tmp'
    [System.IO.File]::WriteAllText($temporary, $text, $utf8)
    if (Test-Path -LiteralPath $operationFile) { [System.IO.File]::Replace($temporary, $operationFile, [System.Management.Automation.Language.NullString]::Value) }
    else { [System.IO.File]::Move($temporary, $operationFile) }
}
function Atomic-Copy([string]$Source, [string]$Destination) {
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Destination)) | Out-Null
    $temporary = $Destination + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [System.IO.File]::Copy($Source, $temporary, $false)
        $flush = [System.IO.File]::Open($temporary, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        try { $flush.Flush($true) } finally { $flush.Dispose() }
        if (Test-Path -LiteralPath $Destination) { [System.IO.File]::Replace($temporary, $Destination, [System.Management.Automation.Language.NullString]::Value) }
        else { [System.IO.File]::Move($temporary, $Destination) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
function Record-Change([string]$Name, [bool]$Existed) {
    $line = @{ name = $Name; existed = $Existed } | ConvertTo-Json -Compress
    $stream = [System.IO.File]::Open($journal, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try { $data = $utf8.GetBytes($line + "`n"); $stream.Write($data, 0, $data.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}
function Runtime-Processes {
    return @(Get-Process -Name MaaGakumasu,MFAAvalonia -ErrorAction SilentlyContinue | Where-Object {
        -not $_.HasExited -and (-not $_.Path -or [System.IO.Path]::GetDirectoryName($_.Path) -eq $root)
    })
}
function Restore-Original {
    if (Test-Path -LiteralPath $journal) {
        $records = [System.Collections.Generic.List[object]]::new()
        $lines = [System.IO.File]::ReadAllLines($journal, $utf8)
        for ($lineIndex=0; $lineIndex -lt $lines.Length; $lineIndex++) {
            $line = $lines[$lineIndex]
            if (-not $line.Trim()) { continue }
            try { $records.Add(($line | ConvertFrom-Json)) }
            catch {
                if ($lineIndex -eq $lines.Length-1 -and -not [System.IO.File]::ReadAllText($journal,$utf8).EndsWith("`n")) { break }
                throw 'Recovery journal is damaged; retained old files require manual recovery.'
            }
        }
        for ($index = $records.Count - 1; $index -ge 0; $index--) {
            $record = $records[$index]
            $target = Target-Path $root $record.name
            if ($record.existed) {
                $saved = Target-Path (Join-Path $backup 'old') $record.name
                if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Hash-File $target) -ne (Hash-File $saved)) { Atomic-Copy $saved $target }
            } elseif (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
        }
    }
    if ($op.settings_backup -and (Test-Path -LiteralPath $op.settings_backup)) {
        # Remove config files created by the attempted version before restoring the complete snapshot.
        $zip = [System.IO.Compression.ZipFile]::OpenRead($op.settings_backup)
        try { $originalNames = @($zip.Entries | ForEach-Object { $_.FullName }) } finally { $zip.Dispose() }
        foreach ($file in Get-ChildItem -LiteralPath (Join-Path $root 'config') -Recurse -File -Force) {
            $relative = $file.FullName.Substring($root.Length + 1).Replace('\', '/')
            $validated = Target-Path $root $relative
            if ($relative -notin $originalNames) { Remove-Item -LiteralPath $validated -Force }
        }
        foreach ($name in @('appsettings.json', 'resource/mfa_layout.json')) {
            if ($name -notin $originalNames -and (Test-Path -LiteralPath (Target-Path $root $name))) { Remove-Item -LiteralPath (Target-Path $root $name) -Force }
        }
        & (Join-Path $stage 'import_settings.ps1') -Root $root -ArchivePath $op.settings_backup -UseExistingBackup
    }
}
function Start-Runtime([bool]$Restored) {
    $env:HIF_UPDATE_OPERATION = $operationFile
    $env:HIF_UPDATE_RESTORED = if ($Restored) { '1' } else { '0' }
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $root 'MaaGakumasu.exe'
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    return [System.Diagnostics.Process]::Start($info)
}

try {
    if ($Recover) {
        if ((Runtime-Processes).Count) { throw 'Close MaaGakumasu before recovering an update.' }
        if ($op.state -notin @('installing', 'starting', 'rollback_failed', 'recovering')) { throw 'This operation does not require recovery.' }
        Save-State 'recovering'
        Restore-Original
        Save-State 'rolled_back'
        if (-not $NoRestart) {
            $restoredProcess = Start-Runtime $true
            $op | Add-Member -Force NoteProperty restored_pid $restoredProcess.Id
            Save-State 'rolled_back'
        }
        exit 0
    }
    if ($op.state -ne 'prepared') { throw 'This operation is not ready to install.' }
    if ($ParentId) {
        $parent = Get-Process -Id $ParentId -ErrorAction SilentlyContinue
        if (-not $parent -or $parent.StartTime.ToUniversalTime().Ticks -ne $ParentStartTicks -or
            [System.IO.Path]::GetDirectoryName($parent.Path) -ne $root) { throw 'The parent process identity does not match.' }
    }
    [System.IO.File]::WriteAllText((Join-Path $stage 'runner.ready'), $op.id, $utf8)
    if ($ParentId) {
        $deadline = [DateTime]::UtcNow.AddSeconds(120)
        while (-not $parent.HasExited) {
            if (Test-Path -LiteralPath (Join-Path $stage 'cancel')) { throw 'Installation cancelled before shutdown.' }
            if ([DateTime]::UtcNow -gt $deadline) { throw 'MaaGakumasu did not exit; no files were changed.' }
            Start-Sleep -Milliseconds 200
            $parent.Refresh()
        }
        if (-not (Test-Path -LiteralPath (Join-Path $stage 'commit.ready'))) { throw 'The frontend did not authorize installation.' }
    }
    for ($attempt=0; $attempt -lt 20 -and (Runtime-Processes).Count; $attempt++) { Start-Sleep -Milliseconds 250 }
    if ((Runtime-Processes).Count) {
        $busy = Runtime-Processes | ForEach-Object { "$($_.Id):$($_.Path)" }
        throw "Another MaaGakumasu process is still using the installation: $($busy -join ', ')"
    }
    $manifest = Get-Content -LiteralPath (Join-Path $candidate 'hif-package-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.format -ne 1 -or $manifest.version -ne $op.version -or $manifest.repository -ne 'miyu1019/MaaGakumasu') { throw 'Invalid package manifest.' }
    $private = @('config','resource','appsettings.json','plugins','logs','debug','backup','temp','tests')
    $seen = @{}
    foreach ($name in $op.files) {
        if ($name.Split('/')[0] -in $private -or $seen.ContainsKey($name)) { throw "Invalid program plan: $name" }
        $seen[$name] = $true
        $null = Target-Path $root $name
    }
    $allowed = @{'hif-package-manifest.json'=$true}
    foreach ($property in $manifest.files.PSObject.Properties) { $allowed[$property.Name]=$true }
    $oldManifestPath = Target-Path $root 'hif-package-manifest.json'
    if (Test-Path -LiteralPath $oldManifestPath) {
        $oldManifest = Get-Content -LiteralPath $oldManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($oldManifest.format -ne 1 -or $oldManifest.repository -ne 'miyu1019/MaaGakumasu' -or $oldManifest.version -ne $op.from_version) { throw 'Invalid installed program manifest.' }
        foreach ($property in $oldManifest.files.PSObject.Properties) {
            if ($property.Name.Split('/')[0] -in $private) { throw 'Installed program manifest contains private files.' }
            $allowed[$property.Name]=$true
        }
    }
    if ($allowed.Count -ne $seen.Count -or @($seen.Keys | Where-Object { -not $allowed.ContainsKey($_) }).Count) { throw 'Program plan contains unowned files.' }
    foreach ($property in $manifest.files.PSObject.Properties) {
        $source = Target-Path $candidate $property.Name
        if (-not $seen.ContainsKey($property.Name) -or (Hash-File $source) -ne $property.Value) { throw "Program verification failed: $($property.Name)" }
    }
    foreach ($name in $op.defaults) {
        if ($name -notlike 'config/*' -and $name -ne 'resource/mfa_layout.json') { throw "Invalid default plan: $name" }
        $property = $manifest.defaults.PSObject.Properties[$name]
        if (-not $property -or (Hash-File (Target-Path $candidate $name)) -ne $property.Value) { throw "Default verification failed: $name" }
        $null = Target-Path $root $name
    }
    $requiredSpace = 64MB
    foreach ($name in $op.files) {
        $target = Target-Path $root $name
        if (Test-Path -LiteralPath $target -PathType Leaf) { $requiredSpace += (Get-Item -LiteralPath $target).Length }
    }
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $root 'config') -Recurse -File -Force) { $requiredSpace += $file.Length }
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($root))
    if ($drive.AvailableFreeSpace -lt $requiredSpace) { throw 'Insufficient disk space; original installation retained.' }
    [System.IO.Directory]::CreateDirectory($backup) | Out-Null
    $settingsBackup = & (Join-Path $stage 'export_settings.ps1') -Root $root -OutputDirectory $backup -PassThru
    $op | Add-Member -Force NoteProperty settings_backup ([string]$settingsBackup)
    [System.IO.File]::WriteAllText((Join-Path $root 'backup/hif-update-last.json'), (@{ operation = $operationFile } | ConvertTo-Json), $utf8)
    Save-State 'installing'
    foreach ($name in @($op.files) + @($op.defaults)) {
        $target = Target-Path $root $name
        $source = Target-Path $candidate $name
        if ($name -in $op.defaults -and (Test-Path -LiteralPath $target)) { continue }
        $existed = Test-Path -LiteralPath $target -PathType Leaf
        if ($existed) {
            $saved = Target-Path (Join-Path $backup 'old') $name
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($saved)) | Out-Null
            Atomic-Copy $target $saved
        }
        Record-Change $name $existed
        if (Test-Path -LiteralPath $source -PathType Leaf) { Atomic-Copy $source $target }
        elseif ($existed) { Remove-Item -LiteralPath $target -Force }
    }
    & (Join-Path $stage 'import_settings.ps1') -Root $root -ArchivePath $op.settings_backup -UseExistingBackup
    Save-State 'starting'
    $newProcess = Start-Runtime $false
    $op | Add-Member -Force NoteProperty launched_pid $newProcess.Id
    $op | Add-Member -Force NoteProperty launched_ticks $newProcess.StartTime.ToUniversalTime().Ticks
    Save-State 'starting'
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $ack = Join-Path $stage 'startup.ok'
    while (-not (Test-Path -LiteralPath $ack)) {
        $newProcess.Refresh()
        if ($newProcess.HasExited -or [DateTime]::UtcNow -gt $deadline) { throw 'The new frontend failed startup verification.' }
        Start-Sleep -Milliseconds 250
    }
    if ([System.IO.File]::ReadAllText($ack).Trim() -ne ($op.id + ':' + $op.version)) { throw 'Invalid startup acknowledgement.' }
    Save-State 'complete'
    # Only remove files enumerated inside this operation's extracted candidate; retain the recovery journal and scripts.
    try {
        foreach ($file in Get-ChildItem -LiteralPath (Join-Path $stage 'extracted') -Recurse -File -Force) {
            $relative = $file.FullName.Substring($stage.Length + 1).Replace('\', '/')
            Remove-Item -LiteralPath (Target-Path $stage $relative) -Force
        }
        foreach ($file in Get-ChildItem -LiteralPath $stage -File -Filter '*.zip*') { Remove-Item -LiteralPath $file.FullName -Force }
    } catch { $_.Exception.Message | Out-File -LiteralPath (Join-Path $stage 'cleanup.log') -Encoding UTF8 }
    Write-Host 'HIF update completed; configuration and previous program files retained in backup/.'
} catch {
    $failure = $_.Exception.Message
    if ($newProcess -and -not $newProcess.HasExited) { $newProcess.Kill(); $newProcess.WaitForExit() }
    if ($op.state -in @('installing', 'starting')) {
        try {
            Restore-Original
            Save-State 'rolled_back' $failure
            try {
                if (-not $NoRestart) {
                    $restoredProcess = Start-Runtime $true
                    $op | Add-Member -Force NoteProperty restored_pid $restoredProcess.Id
                    Save-State 'rolled_back' $failure
                }
            }
            catch { Save-State 'rolled_back' ($failure + '; restart old frontend: ' + $_.Exception.Message) }
        } catch { Save-State 'rollback_failed' ($failure + '; recovery: ' + $_.Exception.Message) }
    } else { Save-State 'failed' $failure }
    $failure | Out-File -LiteralPath (Join-Path $stage 'failure.log') -Encoding UTF8
    throw $failure
} finally { $lock.Dispose() }
