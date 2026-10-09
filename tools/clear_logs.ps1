param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$DryRun,
    [switch]$PriorityBackup
)

$ErrorActionPreference = 'Stop'
$cleanupRoot = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root).Path)
if ($cleanupRoot -eq [System.IO.Path]::GetPathRoot($cleanupRoot)) {
    throw 'Choose a project or runtime directory, not a drive root.'
}
if ((Get-Item -LiteralPath $cleanupRoot -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw 'The cleanup root must not be a link or junction.'
}
$rootPrefix = $cleanupRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
$files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()

foreach ($scope in @('logs', 'debug')) {
    $start = Join-Path $cleanupRoot $scope
    if (-not (Test-Path -LiteralPath $start -PathType Container)) { continue }
    $directories = [System.Collections.Generic.Stack[string]]::new()
    $directories.Push($start)
    while ($directories.Count -gt 0) {
        $directory = $directories.Pop()
        if ((Get-Item -LiteralPath $directory -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            Write-Warning "Skipped link or junction: $directory"
            continue
        }
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                Write-Warning "Skipped link or junction: $($item.FullName)"
                continue
            }
            if ($item.PSIsContainer) {
                $directories.Push($item.FullName)
                continue
            }
            $isLog = $item.Name -like '*.log' -or $item.Name -like '*.log.zip'
            $isImage = $scope -eq 'debug' -and $item.Extension.ToLowerInvariant() -in @('.png', '.jpg', '.jpeg', '.bmp', '.webp')
            if ($isLog -or $isImage) { $files.Add($item) }
        }
    }
}

if ($PriorityBackup) {
    foreach ($relative in @('config', 'config/hif')) {
        $directory = $cleanupRoot
        $linked = $false
        foreach ($part in $relative.Split('/')) {
            $directory = Join-Path $directory $part
            if (Test-Path -LiteralPath $directory) {
                if ((Get-Item -LiteralPath $directory -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { $linked = $true }
            }
        }
        if ($linked) { continue }
        $snapshot = Join-Path $directory 'cards_priority.before_visual_editor.json'
        if (Test-Path -LiteralPath $snapshot -PathType Leaf) {
            $item = Get-Item -LiteralPath $snapshot -Force
            if (-not ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $files.Add($item) }
        }
    }
}

# Validate the complete plan before deleting any file. Never recurse during deletion.
foreach ($item in $files) {
    if (-not $item.FullName.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "File outside cleanup root: $($item.FullName)"
    }
}
$removed = 0
$failed = 0
foreach ($item in $files) {
    $relative = $item.FullName.Substring($rootPrefix.Length)
    if ($DryRun) {
        Write-Host "[dry-run] $relative"
        continue
    }
    try {
        Remove-Item -LiteralPath $item.FullName -Force
        $removed++
    } catch {
        $failed++
        Write-Warning "Could not delete: $relative. Close MaaGakumasu and retry."
    }
}
if ($DryRun) {
    Write-Host "Dry run complete: $($files.Count) files would be deleted."
} else {
    Write-Host "Cleanup complete: $removed files deleted, $failed files kept after failures."
}
if ($failed -gt 0) { exit 1 }
