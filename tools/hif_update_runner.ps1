param(
    [Parameter(Mandatory=$true)][string]$OperationPath,
    [int]$ParentId = 0,
    [long]$ParentStartTicks = 0,
    [switch]$Recover,
    [switch]$NoRestart,
    [switch]$ShowProgress,
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
function Hash-Installed([string]$Path) {
    # Antivirus or indexers may hold an installed file; one that cannot be read is simply replaced.
    try { $stream = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite, Delete') } catch { return $null }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    catch { return $null }
    finally { $stream.Dispose(); $sha.Dispose() }
}
$null = Target-Path $root 'temp/hif-update.lock'
$lock = [System.IO.File]::Open((Join-Path $root 'temp/hif-update.lock'), [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
$journal = Join-Path $backup 'changes.jsonl'
$newProcess = $null
$parentExited = $false
$ui = $null
# 进度阶段：所在步骤（0 关闭程序、1 校验文件、2 替换文件、3 启动新版）与总进度区间（百分比）。
$progressPhases = @{
    prepare = @(0, 0, 0); wait = @(0, 0, 5); verify = @(1, 5, 25); compare = @(1, 25, 40); cleanup = @(1, 40, 42)
    backup = @(2, 42, 47); replace = @(2, 47, 88); config = @(2, 88, 92); start = @(3, 92, 99); finish = @(3, 99, 99)
    restore = @(-1, 0, 100)
}

function New-RoundedPath([float]$X, [float]$Y, [float]$Width, [float]$Height, [float]$Radius) {
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $d = [Math]::Min($Radius * 2, [Math]::Min($Width, $Height))
    if ($d -le 0) { $path.AddRectangle([System.Drawing.RectangleF]::new($X, $Y, [Math]::Max($Width, 0.1), $Height)); return $path }
    $path.AddArc($X, $Y, $d, $d, 180, 90)
    $path.AddArc($X + $Width - $d, $Y, $d, $d, 270, 90)
    $path.AddArc($X + $Width - $d, $Y + $Height - $d, $d, $d, 0, 90)
    $path.AddArc($X, $Y + $Height - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}
function New-ProgressLabel([float]$Size, [string]$Color, [bool]$Bold = $false) {
    $label = [System.Windows.Forms.Label]::new()
    $style = if ($Bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
    $label.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', $Size, $style)
    $label.ForeColor = [System.Drawing.ColorTranslator]::FromHtml($Color)
    $label.BackColor = [System.Drawing.Color]::Transparent
    $label.AutoEllipsis = $true
    $label.UseMnemonic = $false
    return $label
}
function Set-DoubleBuffered($Control) {
    [System.Windows.Forms.Control].GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'NonPublic, Instance').SetValue($Control, $true, $null)
}
function Paint-Steps($Graphics, [int]$Width) {
    $s = $ui.scale
    $Graphics.SmoothingMode = 'AntiAlias'
    $Graphics.TextRenderingHint = 'ClearTypeGridFit'
    $count = $ui.steps.Count
    $radius = 10 * $s
    $cy = 12 * $s
    $centers = @(for ($i = 0; $i -lt $count; $i++) { $Width * (2 * $i + 1) / (2 * $count) })
    $track = [System.Drawing.ColorTranslator]::FromHtml('#E3E7ED')
    $accent = $ui.accent
    for ($i = 0; $i -lt $count - 1; $i++) {
        $color = if ($i -lt $ui.step) { $accent } else { $track }
        $pen = [System.Drawing.Pen]::new($color, 2 * $s)
        $Graphics.DrawLine($pen, $centers[$i] + $radius + 4 * $s, $cy, $centers[$i + 1] - $radius - 4 * $s, $cy)
        $pen.Dispose()
    }
    $numberFont = [System.Drawing.Font]::new('Microsoft YaHei UI', 8.5, [System.Drawing.FontStyle]::Bold)
    $labelFont = [System.Drawing.Font]::new('Microsoft YaHei UI', 9)
    for ($i = 0; $i -lt $count; $i++) {
        $box = [System.Drawing.RectangleF]::new($centers[$i] - $radius, $cy - $radius, 2 * $radius, 2 * $radius)
        $done = $i -lt $ui.step
        $current = $i -eq $ui.step
        if ($done) {
            $brush = [System.Drawing.SolidBrush]::new($accent); $Graphics.FillEllipse($brush, $box); $brush.Dispose()
            $pen = [System.Drawing.Pen]::new([System.Drawing.Color]::White, 2 * $s)
            $pen.StartCap = 'Round'; $pen.EndCap = 'Round'; $pen.LineJoin = 'Round'
            $Graphics.DrawLines($pen, [System.Drawing.PointF[]]@(
                [System.Drawing.PointF]::new($centers[$i] - 4.5 * $s, $cy + 0.5 * $s),
                [System.Drawing.PointF]::new($centers[$i] - 1.5 * $s, $cy + 3.5 * $s),
                [System.Drawing.PointF]::new($centers[$i] + 4.5 * $s, $cy - 3 * $s)))
            $pen.Dispose()
        } elseif ($current) {
            $brush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(36, $accent)); $Graphics.FillEllipse($brush, $box); $brush.Dispose()
            $pen = [System.Drawing.Pen]::new($accent, 2 * $s); $Graphics.DrawEllipse($pen, $box); $pen.Dispose()
            $mark = if ($ui.failed) { '!' } else { [string]($i + 1) }
            [System.Windows.Forms.TextRenderer]::DrawText($Graphics, $mark, $numberFont, [System.Drawing.Rectangle]::Round($box), $accent, 'HorizontalCenter, VerticalCenter, NoPadding')
        } else {
            $pen = [System.Drawing.Pen]::new($track, 2 * $s); $Graphics.DrawEllipse($pen, $box); $pen.Dispose()
            [System.Windows.Forms.TextRenderer]::DrawText($Graphics, [string]($i + 1), $numberFont, [System.Drawing.Rectangle]::Round($box),
                [System.Drawing.ColorTranslator]::FromHtml('#8C959F'), 'HorizontalCenter, VerticalCenter, NoPadding')
        }
        $textColor = if ($current) { [System.Drawing.ColorTranslator]::FromHtml('#1F2328') } else { [System.Drawing.ColorTranslator]::FromHtml('#6E7781') }
        $textBox = [System.Drawing.Rectangle]::new([int]($centers[$i] - $Width / (2 * $count)), [int]($cy + $radius + 6 * $s), [int]($Width / $count), [int](20 * $s))
        [System.Windows.Forms.TextRenderer]::DrawText($Graphics, $ui.steps[$i], $labelFont, $textBox, $textColor, 'HorizontalCenter, Top, NoPadding')
    }
    $numberFont.Dispose(); $labelFont.Dispose()
}
function Paint-Bar($Graphics, [int]$Width, [int]$Height) {
    $Graphics.SmoothingMode = 'AntiAlias'
    $track = New-RoundedPath 0 0 ($Width - 1) ($Height - 1) ($Height / 2)
    $brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#E3E7ED')); $Graphics.FillPath($brush, $track); $brush.Dispose(); $track.Dispose()
    $fill = ($Width - 1) * [Math]::Max(0, [Math]::Min(100, $ui.percent)) / 100
    if ($fill -ge 1) {
        $path = New-RoundedPath 0 0 $fill ($Height - 1) ($Height / 2)
        $brush = [System.Drawing.SolidBrush]::new($ui.accent); $Graphics.FillPath($brush, $path); $brush.Dispose(); $path.Dispose()
    }
}
function Open-ProgressWindow {
    if (-not $ShowProgress) { return }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $screen = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
    $s = $screen.DpiX / 96
    $screen.Dispose()
    $script:ui = @{
        scale = $s; step = 0; percent = 0; failed = $false; started = [DateTime]::UtcNow
        accent = [System.Drawing.ColorTranslator]::FromHtml('#2F6FEB')
        steps = @('关闭程序', '校验文件', '替换文件', '启动新版')
    }
    $width = 520
    $form = [System.Windows.Forms.Form]::new()
    $form.Text = 'HIF 更新'
    $form.ClientSize = [System.Drawing.Size]::new([int]($width * $s), [int](276 * $s))
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.ControlBox = $false
    $form.ShowInTaskbar = $true
    $form.TopMost = $true
    $form.BackColor = [System.Drawing.Color]::White
    $form.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 9)
    $ui.form = $form
    $inner = [int](($width - 56) * $s)
    $left = [int](28 * $s)

    $ui.title = New-ProgressLabel 15 '#1F2328' $true
    $ui.title.SetBounds($left, [int](20 * $s), $inner, [int](34 * $s))
    $ui.title.Text = if ($Recover) { '正在恢复更新' } elseif ($op.from_version -eq $op.version) { '正在重新安装 HIF' } else { '正在更新 HIF' }
    $ui.version = New-ProgressLabel 9.5 '#6E7781'
    $ui.version.SetBounds($left, [int](54 * $s), $inner, [int](22 * $s))
    $ui.version.Text = if ($op.from_version -eq $op.version) { [string]$op.version } else { "$($op.from_version)  →  $($op.version)" }

    $ui.stepPanel = [System.Windows.Forms.Panel]::new()
    $ui.stepPanel.SetBounds($left, [int](92 * $s), $inner, [int](48 * $s))
    Set-DoubleBuffered $ui.stepPanel
    $ui.stepPanel.Add_Paint({ Paint-Steps $_.Graphics $ui.stepPanel.Width })

    $ui.action = New-ProgressLabel 10.5 '#1F2328' $true
    $ui.action.SetBounds($left, [int](156 * $s), [int](($width - 56 - 64) * $s), [int](24 * $s))
    $ui.percentText = New-ProgressLabel 10.5 '#2F6FEB' $true
    $ui.percentText.TextAlign = 'MiddleRight'
    $ui.percentText.SetBounds([int](($width - 28 - 64) * $s), [int](156 * $s), [int](64 * $s), [int](24 * $s))

    $ui.bar = [System.Windows.Forms.Panel]::new()
    $ui.bar.SetBounds($left, [int](184 * $s), $inner, [int](8 * $s))
    Set-DoubleBuffered $ui.bar
    $ui.bar.Add_Paint({ Paint-Bar $_.Graphics $ui.bar.Width $ui.bar.Height })

    $ui.detail = New-ProgressLabel 9 '#6E7781'
    $ui.detail.SetBounds($left, [int](198 * $s), $inner, [int](20 * $s))
    $ui.file = New-ProgressLabel 9 '#8C959F'
    $ui.file.SetBounds($left, [int](218 * $s), $inner, [int](20 * $s))

    $ui.errorBox = [System.Windows.Forms.TextBox]::new()
    $ui.errorBox.Multiline = $true
    $ui.errorBox.ReadOnly = $true
    $ui.errorBox.BorderStyle = 'FixedSingle'
    $ui.errorBox.BackColor = [System.Drawing.ColorTranslator]::FromHtml('#F6F8FA')
    $ui.errorBox.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#57606A')
    $ui.errorBox.Font = [System.Drawing.Font]::new('Consolas', 9)
    $ui.errorBox.SetBounds($left, [int](202 * $s), $inner, [int](64 * $s))
    $ui.errorBox.Visible = $false

    $ui.hint = New-ProgressLabel 9 '#8C959F'
    $ui.hint.SetBounds($left, [int](244 * $s), [int](($width - 56 - 110) * $s), [int](20 * $s))
    $ui.hint.Text = '请勿手动打开程序，更新完成后会自动启动。'
    $ui.close = [System.Windows.Forms.Button]::new()
    $ui.close.Text = '关闭'
    $ui.close.FlatStyle = 'Flat'
    $ui.close.FlatAppearance.BorderSize = 0
    $ui.close.BackColor = $ui.accent
    $ui.close.ForeColor = [System.Drawing.Color]::White
    $ui.close.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 9.5, [System.Drawing.FontStyle]::Bold)
    $ui.close.SetBounds([int](($width - 28 - 96) * $s), [int](236 * $s), [int](96 * $s), [int](30 * $s))
    $ui.close.Visible = $false
    $ui.close.Add_Click({ $ui.form.Close() })
    $form.AcceptButton = $ui.close

    $form.Controls.AddRange(@($ui.title, $ui.version, $ui.stepPanel, $ui.action, $ui.percentText, $ui.bar, $ui.detail, $ui.file, $ui.errorBox, $ui.hint, $ui.close))
    $form.Show()
    Show-UpdateProgress prepare '正在准备更新'
}
function Show-UpdateProgress([string]$Phase, [string]$Action, [string]$Detail = '', [int]$Done = 0, [int]$Total = 0, [string]$File = '') {
    if (-not $ui) { return }
    $step, $from, $to = $progressPhases[$Phase]
    if ($Phase -ne $ui.phase) { $ui.phase = $Phase; $ui.phaseStarted = [DateTime]::UtcNow }
    # 启动验证没有可数的进度：按已等待时间逐渐逼近该阶段终点。
    $fraction = if ($Total -gt 0) { [Math]::Min(1.0, $Done / $Total) }
        elseif ($Phase -eq 'start') { 1 - [Math]::Exp(-([DateTime]::UtcNow - $ui.phaseStarted).TotalSeconds / 15) }
        else { 0 }
    if ($Phase -eq 'restore') {
        if (-not $ui.failed) {
            $ui.failed = $true
            $ui.accent = [System.Drawing.ColorTranslator]::FromHtml('#C47F00')
            $ui.percentText.ForeColor = $ui.accent
            $ui.title.Text = '正在恢复旧版本'
            $ui.hint.Text = '更新没有完成，正在还原程序文件和个人配置，请稍候。'
        }
    } else { $ui.step = $step }
    $ui.percent = [int]($from + ($to - $from) * $fraction)
    $ui.action.Text = $Action
    $ui.percentText.Text = "$($ui.percent)%"
    $ui.detail.Text = $Detail
    $ui.file.Text = $File
    $ui.stepPanel.Invalidate()
    $ui.bar.Invalidate()
    [System.Windows.Forms.Application]::DoEvents()
}
function Close-ProgressWindow {
    if (-not $ui) { return }
    $s = $ui.scale
    $result = switch ($op.state) {
        'complete' { @('更新完成', "新版 $($op.version) 已启动，可以继续使用。", '#1F883D') }
        'rolled_back' {
            if ($Recover) { @('已恢复旧版本', '旧版本程序文件和个人配置已恢复。', '#1F883D') }
            else { @('已恢复旧版本', '更新没有完成，程序文件和个人配置已还原，可稍后重试。', '#C47F00') }
        }
        'rollback_failed' { @('自动恢复未完成', '请关闭程序后运行 recover_update_恢复更新.bat。', '#CF222E') }
        'failed' { @('更新未安装', '当前版本未改动，可稍后重试。', '#C47F00') }
        default { @('更新未完成', '请查看下方原因。', '#CF222E') }
    }
    $color = [System.Drawing.ColorTranslator]::FromHtml($result[2])
    $complete = $op.state -eq 'complete'
    $ui.accent = $color
    $ui.failed = -not $complete
    if ($complete) { $ui.step = $ui.steps.Count; $ui.percent = 100 }
    $ui.title.Text = $result[0]
    $ui.title.ForeColor = $color
    $ui.action.Text = $result[1]
    $ui.action.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 10)
    $ui.percentText.ForeColor = $color
    $ui.percentText.Text = if ($complete) { '100%' } else { '' }
    $ui.detail.Text = if ($complete -and $null -ne $op.changed_files) { "替换 $($op.changed_files) 个文件，$($op.unchanged_files) 个未变已跳过。" } else { '' }
    $ui.file.Text = ''
    $ui.hint.Text = '记录：temp\hif-updates\' + $op.id + '\operation.json'
    if ($op.error) {
        $ui.detail.Visible = $false
        $ui.file.Visible = $false
        $ui.errorBox.Text = [string]$op.error
        if ($ui.errorBox.Text.Length -gt 160) { $ui.errorBox.ScrollBars = 'Vertical' }
        $ui.errorBox.Visible = $true
        $ui.form.ClientSize = [System.Drawing.Size]::new($ui.form.ClientSize.Width, [int](330 * $s))
        $ui.hint.Top = [int](298 * $s)
        $ui.close.Top = [int](290 * $s)
    }
    $ui.close.BackColor = $color
    $ui.close.Visible = $true
    $ui.form.ControlBox = $true
    $ui.form.TopMost = $false
    $ui.stepPanel.Invalidate()
    $ui.bar.Invalidate()
    if ($complete) {
        # 成功时倒计时自动关闭；新版已在前台，窗口不必常驻。
        $ui.remaining = 8
        $ui.close.Text = '关闭（8）'
        $ui.timer = [System.Windows.Forms.Timer]::new()
        $ui.timer.Interval = 1000
        $ui.timer.Add_Tick({
            $ui.remaining--
            if ($ui.remaining -le 0) { $ui.timer.Stop(); $ui.form.Close() } else { $ui.close.Text = "关闭（$($ui.remaining)）" }
        })
        $ui.timer.Start()
    }
    $ui.form.Hide()
    $null = $ui.form.ShowDialog()
    if ($ui.timer) { $ui.timer.Dispose() }
    $ui.form.Dispose()
}

function Commit-File([string]$Temporary, [string]$Destination) {
    # Antivirus or indexers may briefly open the target without delete sharing; ReplaceFile then fails but leaves both files usable.
    for ($attempt = 1; ; $attempt++) {
        try {
            if (Test-Path -LiteralPath $Destination) { [System.IO.File]::Replace($Temporary, $Destination, [System.Management.Automation.Language.NullString]::Value) }
            else { [System.IO.File]::Move($Temporary, $Destination) }
            return
        } catch [System.IO.IOException], [System.UnauthorizedAccessException] {
            if ($attempt -ge 10 -or -not (Test-Path -LiteralPath $Temporary)) {
                throw "Cannot replace $Destination after $attempt attempt(s), the file may be held by antivirus or another program: $($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}
function Save-State([string]$State, [string]$ErrorText = '') {
    $op.state = $State
    $op | Add-Member -Force NoteProperty error $ErrorText
    $text = $op | ConvertTo-Json -Depth 10
    $temporary = $operationFile + '.tmp'
    [System.IO.File]::WriteAllText($temporary, $text, $utf8)
    Commit-File $temporary $operationFile
}
function Atomic-Copy([string]$Source, [string]$Destination) {
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Destination)) | Out-Null
    $temporary = $Destination + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [System.IO.File]::Copy($Source, $temporary, $false)
        $flush = [System.IO.File]::Open($temporary, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        try { $flush.Flush($true) } finally { $flush.Dispose() }
        Commit-File $temporary $Destination
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
    Show-UpdateProgress restore '正在恢复旧版本'
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
            Show-UpdateProgress restore '正在还原程序文件' "$($records.Count - $index - 1) / $($records.Count)" ($records.Count - $index - 1) $records.Count $records[$index].name
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
function Remove-ObsoleteUpdates {
    # Runs after the frontend exited and while holding the update lock, so no other update is preparing or installing.
    # Keeps operations that may still need recovery and the newest completed update; removes other update folders and backups.
    $recoverable = @('installing', 'starting', 'rollback_failed', 'recovering', 'unreadable')
    $states = @{}
    $newest = $null
    $newestTime = [DateTime]::MinValue
    $updates = Join-Path $root 'temp/hif-updates'
    if ([System.IO.Directory]::Exists($updates)) {
        foreach ($directory in [System.IO.Directory]::GetDirectories($updates)) {
            $id = [System.IO.Path]::GetFileName($directory)
            if ($id -notmatch '^[a-f0-9]{32}$' -or $id -eq $op.id) { continue }
            $file = Join-Path $directory 'operation.json'
            $state = ''
            if ([System.IO.File]::Exists($file)) {
                try { $state = [string]([System.IO.File]::ReadAllText($file, $utf8) | ConvertFrom-Json).state } catch { $state = 'unreadable' }
                $time = [System.IO.File]::GetLastWriteTimeUtc($file)
                if ($state -eq 'complete' -and $time -gt $newestTime) { $newest = $id; $newestTime = $time }
            }
            $states[$id] = $state
        }
    }
    $obsolete = [System.Collections.Generic.List[string]]::new()
    foreach ($id in $states.Keys) {
        if ($states[$id] -notin $recoverable -and $id -ne $newest) { $obsolete.Add('temp/hif-updates/' + $id) }
    }
    $backups = Join-Path $root 'backup'
    if ([System.IO.Directory]::Exists($backups)) {
        foreach ($directory in [System.IO.Directory]::GetDirectories($backups)) {
            $name = [System.IO.Path]::GetFileName($directory)
            if ($name -notmatch '^hif-update-([a-f0-9]{32})$') { continue }
            $id = $Matches[1]
            if ($id -eq $op.id -or $id -eq $newest -or ($states.ContainsKey($id) -and $states[$id] -in $recoverable)) { continue }
            $obsolete.Add('backup/' + $name)
        }
    }
    $removed = 0
    foreach ($relative in $obsolete) {
        Show-UpdateProgress cleanup '正在清理过期的更新文件' '' 0 0 $relative
        # Directory.Delete removes links inside the folder without following them.
        try { [System.IO.Directory]::Delete((Target-Path $root $relative), $true); $removed++ }
        catch { Add-Content -LiteralPath (Join-Path $stage 'cleanup.log') -Value ($relative + ': ' + $_.Exception.Message) -Encoding UTF8 }
    }
    return $removed
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
    Open-ProgressWindow
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
    Show-UpdateProgress wait '正在等待主程序退出' '主程序保存配置并退出后开始安装。'
    if ($ParentId) {
        $deadline = [DateTime]::UtcNow.AddSeconds(120)
        while (-not $parent.HasExited) {
            if (Test-Path -LiteralPath (Join-Path $stage 'cancel')) { throw 'Installation cancelled before shutdown.' }
            if ([DateTime]::UtcNow -gt $deadline) { throw 'MaaGakumasu did not exit; no files were changed.' }
            Start-Sleep -Milliseconds 200
            Show-UpdateProgress wait '正在等待主程序退出' '主程序保存配置并退出后开始安装。'
            $parent.Refresh()
        }
        $parentExited = $true
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
    $verified = 0
    $verificationTotal = @($manifest.files.PSObject.Properties).Count
    Show-UpdateProgress verify '正在校验新版文件' "0 / $verificationTotal" 0 $verificationTotal
    foreach ($property in $manifest.files.PSObject.Properties) {
        $source = Target-Path $candidate $property.Name
        if (-not $seen.ContainsKey($property.Name) -or (Hash-File $source) -ne $property.Value) { throw "Program verification failed: $($property.Name)" }
        $verified++
        Show-UpdateProgress verify '正在校验新版文件' "$verified / $verificationTotal" $verified $verificationTotal $property.Name
    }
    foreach ($name in $op.defaults) {
        if ($name -notlike 'config/*' -and $name -ne 'resource/mfa_layout.json') { throw "Invalid default plan: $name" }
        $property = $manifest.defaults.PSObject.Properties[$name]
        if (-not $property -or (Hash-File (Target-Path $candidate $name)) -ne $property.Value) { throw "Default verification failed: $name" }
        $null = Target-Path $root $name
    }
    # Compare with the files actually installed rather than the old manifest, which hand-copied files make stale.
    $expected = @{}
    foreach ($property in $manifest.files.PSObject.Properties) { $expected[$property.Name] = $property.Value }
    $expected['hif-package-manifest.json'] = Hash-File (Target-Path $candidate 'hif-package-manifest.json')
    $plan = [System.Collections.Generic.List[string]]::new()
    $compared = 0
    foreach ($name in $op.files) {
        $compared++
        Show-UpdateProgress compare '正在比对已安装文件' "$compared / $($op.files.Count)，$($plan.Count) 个需要替换" $compared $op.files.Count $name
        $target = Target-Path $root $name
        $exists = Test-Path -LiteralPath $target -PathType Leaf
        if ($expected.ContainsKey($name)) {
            if (-not $exists -or (Hash-Installed $target) -ne $expected[$name]) { $plan.Add($name) }
        } elseif ($exists) { $plan.Add($name) }
    }
    $unchanged = $op.files.Count - $plan.Count
    foreach ($name in $op.defaults) {
        if (-not (Test-Path -LiteralPath (Target-Path $root $name))) { $plan.Add($name) }
    }
    $op | Add-Member -Force NoteProperty changed_files $plan.Count
    $op | Add-Member -Force NoteProperty unchanged_files $unchanged
    $op | Add-Member -Force NoteProperty cleaned_operations (Remove-ObsoleteUpdates)
    $requiredSpace = 64MB
    foreach ($name in $plan) {
        $target = Target-Path $root $name
        if (Test-Path -LiteralPath $target -PathType Leaf) { $requiredSpace += (Get-Item -LiteralPath $target).Length }
    }
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $root 'config') -Recurse -File -Force) { $requiredSpace += $file.Length }
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($root))
    if ($drive.AvailableFreeSpace -lt $requiredSpace) { throw 'Insufficient disk space; original installation retained.' }
    [System.IO.Directory]::CreateDirectory($backup) | Out-Null
    Show-UpdateProgress backup '正在备份个人配置' "共 $($plan.Count) 个文件需要替换，$unchanged 个未变已跳过。"
    $settingsBackup = & (Join-Path $stage 'export_settings.ps1') -Root $root -OutputDirectory $backup -PassThru
    $op | Add-Member -Force NoteProperty settings_backup ([string]$settingsBackup)
    [System.IO.File]::WriteAllText((Join-Path $root 'backup/hif-update-last.json'), (@{ operation = $operationFile } | ConvertTo-Json), $utf8)
    Save-State 'installing'
    $installed = 0
    foreach ($name in $plan) {
        Show-UpdateProgress replace '正在替换文件' "$installed / $($plan.Count)，$unchanged 个未变已跳过" $installed $plan.Count $name
        $installed++
        $target = Target-Path $root $name
        $source = Target-Path $candidate $name
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
    Show-UpdateProgress config '正在导回个人配置'
    & (Join-Path $stage 'import_settings.ps1') -Root $root -ArchivePath $op.settings_backup -UseExistingBackup
    Save-State 'starting'
    Show-UpdateProgress start '正在启动新版' '文件已替换，等待新版启动确认。'
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
        Show-UpdateProgress start '正在启动新版' '文件已替换，等待新版启动确认。'
    }
    if ([System.IO.File]::ReadAllText($ack).Trim() -ne ($op.id + ':' + $op.version)) { throw 'Invalid startup acknowledgement.' }
    Save-State 'complete'
    # Only remove files enumerated inside this operation's extracted candidate; retain the recovery journal and scripts.
    try {
        $pending = Join-Path $root 'temp/hif-update-pending.json'
        if ([System.IO.File]::Exists($pending) -and [System.IO.Path]::GetFullPath(
                ([System.IO.File]::ReadAllText($pending, $utf8) | ConvertFrom-Json).operation) -eq $operationFile) {
            Remove-Item -LiteralPath $pending -Force
        }
        foreach ($file in Get-ChildItem -LiteralPath (Join-Path $stage 'extracted') -Recurse -File -Force) {
            Show-UpdateProgress finish '正在清理临时文件' '新版已启动。'
            $relative = $file.FullName.Substring($stage.Length + 1).Replace('\', '/')
            Remove-Item -LiteralPath (Target-Path $stage $relative) -Force
        }
        foreach ($file in Get-ChildItem -LiteralPath $stage -File -Filter '*.zip*') { Remove-Item -LiteralPath $file.FullName -Force }
    } catch { Add-Content -LiteralPath (Join-Path $stage 'cleanup.log') -Value $_.Exception.Message -Encoding UTF8 }
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
    } else {
        Save-State 'failed' $failure
        # 主程序已为安装退出、但还没改动任何文件：重新打开旧版，前台据 failed 状态提示「更新未安装」。
        if ($parentExited -and -not $NoRestart -and -not (Runtime-Processes).Count) {
            try {
                $restoredProcess = Start-Runtime $true
                $op | Add-Member -Force NoteProperty restored_pid $restoredProcess.Id
                Save-State 'failed' $failure
            } catch { Save-State 'failed' ($failure + '; restart frontend: ' + $_.Exception.Message) }
        }
    }
    $failure | Out-File -LiteralPath (Join-Path $stage 'failure.log') -Encoding UTF8
    throw $failure
} finally { $lock.Dispose(); Close-ProgressWindow }
