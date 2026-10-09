param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$OutputDirectory,
    [switch]$PassThru
)

$ErrorActionPreference = 'Stop'
$exportRoot = (Resolve-Path -LiteralPath $Root).Path
$rootPrefix = $exportRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if (-not (Test-Path -LiteralPath (Join-Path $exportRoot 'config') -PathType Container)) {
    throw 'No config directory found. Choose the MaaGakumasu runtime directory.'
}
if ((Get-Item -LiteralPath $exportRoot -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw 'The runtime root must not be a link or junction.'
}
foreach ($process in Get-Process -Name MaaGakumasu, MFAAvalonia -ErrorAction SilentlyContinue) {
    if (-not $process.Path -or $process.Path.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Close MaaGakumasu completely before exporting settings.'
    }
}

# Include the whole config tree (including legacy strategies), global instances/timers, and layout.
$files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
$pending = [System.Collections.Generic.Stack[string]]::new()
foreach ($relative in @('config', 'appsettings.json', 'resource/mfa_layout.json')) {
    $path = $exportRoot
    foreach ($part in $relative.Split('/')) {
        $path = Join-Path $path $part
        if ((Test-Path -LiteralPath $path) -and
            ((Get-Item -LiteralPath $path -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "Cannot export a link or junction: $path"
        }
    }
    if (Test-Path -LiteralPath $path) { $pending.Push($path) }
}
while ($pending.Count -gt 0) {
    $item = Get-Item -LiteralPath $pending.Pop() -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Cannot export a link or junction: $($item.FullName)"
    }
    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) { $pending.Push($child.FullName) }
    } else {
        $files.Add($item)
    }
}
if (-not ($files | Where-Object { $_.FullName.StartsWith((Join-Path $rootPrefix 'config') + '\', [System.StringComparison]::OrdinalIgnoreCase) })) {
    throw 'The config directory is empty; no settings were exported.'
}
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $exportRoot 'backup' }
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($output.StartsWith((Join-Path $rootPrefix 'config') + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
    $output -eq (Join-Path $exportRoot 'config')) {
    throw 'Choose an output directory outside config/.'
}
[System.IO.Directory]::CreateDirectory($output) | Out-Null
$name = 'maa-settings-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.zip'
$destination = Join-Path $output $name
$temporary = $destination + '.partial'

Add-Type -AssemblyName System.IO.Compression
$stream = [System.IO.File]::Open($temporary, [System.IO.FileMode]::CreateNew)
$archive = $null
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $archive = [System.IO.Compression.ZipArchive]::new($stream, [System.IO.Compression.ZipArchiveMode]::Create, $true)
    $manifest = [ordered]@{ format = 1; created_utc = [DateTime]::UtcNow.ToString('o'); files = [ordered]@{} }
    foreach ($file in $files | Sort-Object FullName) {
        $relative = $file.FullName.Substring($rootPrefix.Length).Replace('\', '/')
        $data = [System.IO.File]::ReadAllBytes($file.FullName)
        $entry = $archive.CreateEntry($relative).Open()
        try { $entry.Write($data, 0, $data.Length) } finally { $entry.Dispose() }
        $manifest.files[$relative] = [ordered]@{
            bytes = $data.Length
            sha256 = [BitConverter]::ToString($sha.ComputeHash($data)).Replace('-', '').ToLowerInvariant()
        }
    }
    $instructions = @'
MAA SETTINGS BACKUP / 设置与个人策略备份

1. 将新版完整安装包解压到新目录，完全退出新旧 Maa 前台。
2. 在新版目录双击 import_settings_导入设置.bat，选择本 ZIP；也可把 ZIP 拖到该脚本上。导入前自动备份现有设置，失败回滚。
   若新版尚无导入脚本，可将本备份解压到临时目录，把 config/、appsettings.json（如有）复制到新版根目录，合并并覆盖同名文件。
3. 将 resource/mfa_layout.json（如有）复制到新版对应位置，保留布局。
4. 打开新版，检查实例、连接、任务选项及五个 HIF 策略面板。
5. 旧版策略若直接位于 config/，按新版迁移说明复制三个策略文件到 config/hif/，再执行 python/python.exe tools/hif_app.py migrate。

backup-manifest.json 中的 SHA-256 对应备份内原始文件。备份包含本人的连接信息、定时任务与策略，请自行保存。
删除旧目录前，将本 ZIP 另存到旧目录之外。导出仅包含已保存的设置，不修改源文件。
'@
    foreach ($metadata in @{'backup-manifest.json' = ($manifest | ConvertTo-Json -Depth 6); '恢复说明.txt' = $instructions}.GetEnumerator()) {
        $data = [System.Text.Encoding]::UTF8.GetBytes($metadata.Value)
        $entry = $archive.CreateEntry($metadata.Key).Open()
        try { $entry.Write($data, 0, $data.Length) } finally { $entry.Dispose() }
    }
    $archive.Dispose()
    $archive = $null
    $stream.Dispose()
    [System.IO.File]::Move($temporary, $destination)
} finally {
    if ($archive) { $archive.Dispose() }
    $stream.Dispose()
    $sha.Dispose()
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
}
Write-Host "Exported $($files.Count) settings files:"
Write-Host $destination
Write-Host 'Restore instructions are included in the ZIP. Keep it outside the old installation before deleting that directory.'
if ($PassThru) { Write-Output $destination }
