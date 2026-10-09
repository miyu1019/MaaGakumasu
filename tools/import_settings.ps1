param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$ArchivePath,
    [switch]$UseExistingBackup
)

$ErrorActionPreference = 'Stop'
$importRoot = (Resolve-Path -LiteralPath $Root).Path
$rootPrefix = $importRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if (-not $ArchivePath) {
    Add-Type -AssemblyName System.Windows.Forms
    $picker = [System.Windows.Forms.OpenFileDialog]::new()
    $picker.Title = '选择 Maa 设置备份 / Select Maa settings backup'
    $picker.Filter = 'Maa settings backup (*.zip)|*.zip'
    $picker.InitialDirectory = Join-Path $importRoot 'backup'
    try {
        if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            Write-Host 'Import cancelled.'
            exit 0
        }
        $ArchivePath = $picker.FileName
    } finally { $picker.Dispose() }
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $ArchivePath).Path)
$sha = [System.Security.Cryptography.SHA256]::Create()
$settings = [ordered]@{}
try {
    $manifestEntry = $archive.GetEntry('backup-manifest.json')
    if (-not $manifestEntry) { throw 'Not a Maa settings backup: manifest missing.' }
    $reader = [System.IO.StreamReader]::new($manifestEntry.Open(), [System.Text.Encoding]::UTF8)
    try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    if ($manifest.format -ne 1 -or -not $manifest.files) { throw 'Unsupported settings backup format.' }
    $seen = @{}
    foreach ($entry in $archive.Entries) {
        $name = $entry.FullName
        if ($seen.ContainsKey($name)) { throw "Duplicate backup entry: $name" }
        $seen[$name] = $true
        if ($name -in @('backup-manifest.json', '恢复说明.txt')) { continue }
        if ($name -notlike 'config/*' -and $name -ne 'appsettings.json' -and $name -ne 'resource/mfa_layout.json') {
            throw "Unexpected backup entry: $name"
        }
        foreach ($part in $name.Split('/')) {
            if (-not $part -or $part -in @('.', '..') -or $part -match '[\\:*?"<>|\x00-\x1f]' -or
                $part.EndsWith('.') -or $part.EndsWith(' ') -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
                throw "Invalid backup path: $name"
            }
        }
        $expected = $manifest.files.PSObject.Properties[$name]
        if (-not $expected) { throw "Backup entry missing from manifest: $name" }
        $inputStream = $entry.Open()
        $buffer = [System.IO.MemoryStream]::new()
        try {
            $inputStream.CopyTo($buffer)
            $data = $buffer.ToArray()
        } finally { $inputStream.Dispose(); $buffer.Dispose() }
        $hash = [BitConverter]::ToString($sha.ComputeHash($data)).Replace('-', '').ToLowerInvariant()
        if ($data.Length -ne $expected.Value.bytes -or $hash -ne $expected.Value.sha256) {
            throw "Backup verification failed: $name"
        }
        $target = [System.IO.Path]::GetFullPath((Join-Path $importRoot $name))
        if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Backup path outside runtime: $name"
        }
        $path = $importRoot
        foreach ($part in $name.Split('/')) {
            $path = Join-Path $path $part
            if ((Test-Path -LiteralPath $path) -and
                ((Get-Item -LiteralPath $path -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                throw "Cannot import through a link or junction: $path"
            }
        }
        if (Test-Path -LiteralPath $target -PathType Container) { throw "A directory blocks the setting file: $name" }
        $previous = $null
        if (Test-Path -LiteralPath $target -PathType Leaf) { $previous = [System.IO.File]::ReadAllBytes($target) }
        $settings[$name] = @{ target = $target; data = $data; previous = $previous }
    }
    if ($settings.Count -ne @($manifest.files.PSObject.Properties).Count -or
        -not ($settings.Keys | Where-Object { $_ -like 'config/*' })) {
        throw 'Backup is incomplete or contains no config files.'
    }
} finally { $archive.Dispose(); $sha.Dispose() }

# Validate everything before backing up or changing the destination. Export also checks the frontend is closed.
if ($UseExistingBackup) {
    $backupPrefix = Join-Path $rootPrefix 'backup'
    if (-not (Resolve-Path -LiteralPath $ArchivePath).Path.StartsWith($backupPrefix + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'An existing automatic backup must be inside this runtime backup/ directory.'
    }
} else {
    & (Join-Path $PSScriptRoot 'export_settings.ps1') -Root $importRoot
}

function Write-Setting([string]$Target, [byte[]]$Data) {
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Target)) | Out-Null
    $temporary = $Target + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $stream = [System.IO.File]::Open($temporary, [System.IO.FileMode]::CreateNew)
    try {
        $stream.Write($Data, 0, $Data.Length)
        $stream.Dispose()
        if (Test-Path -LiteralPath $Target) { [System.IO.File]::Replace($temporary, $Target, [System.Management.Automation.Language.NullString]::Value) }
        else { [System.IO.File]::Move($temporary, $Target) }
    } finally {
        $stream.Dispose()
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

$changed = [System.Collections.Generic.List[string]]::new()
try {
    foreach ($name in $settings.Keys) {
        Write-Setting $settings[$name].target $settings[$name].data
        $changed.Add($name)
    }
} catch {
    $failure = $_
    $rollbackFailures = @()
    for ($index = $changed.Count - 1; $index -ge 0; $index--) {
        $setting = $settings[$changed[$index]]
        try {
            if ($null -ne $setting.previous) { Write-Setting $setting.target $setting.previous }
            else { Remove-Item -LiteralPath $setting.target -Force }
        } catch { $rollbackFailures += $setting.target }
    }
    if ($rollbackFailures.Count) {
        throw "Import failed; restore the automatic backup in backup/. Could not roll back: $($rollbackFailures -join ', ')"
    }
    throw "Import failed; original setting files restored. $failure"
}
Write-Host "Imported $($settings.Count) settings files. Previous settings are backed up in backup/."
Write-Host 'Start MaaGakumasu and check instances, connection, task options and HIF strategy panels.'
