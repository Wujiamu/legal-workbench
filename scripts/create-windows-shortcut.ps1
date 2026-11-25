$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Bat = Join-Path $Root "法律工作台.bat"
$Icon = Join-Path $Root "scripts\legal-workbench.ico"
$Desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($Desktop)) {
    throw "无法解析当前用户的桌面路径"
}
$Lnk = Join-Path $Desktop "法律工作台.lnk"

if (-not (Test-Path $Bat)) {
    throw "未找到启动脚本：$Bat"
}

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($Lnk)
$Shortcut.TargetPath = $Bat
$Shortcut.WorkingDirectory = $Root
$HasIcon = Test-Path $Icon
if ($HasIcon) {
    $Shortcut.IconLocation = "$Icon,0"
}
$Shortcut.Description = "素章（PlainFolio）—— 离线诉讼案件工作台"
$Shortcut.WindowStyle = 7
$Shortcut.Save()
if ($HasIcon) {
    Write-Host "已在桌面创建带「素章」图标的快捷方式：$Lnk"
} else {
    Write-Host "已在桌面创建快捷方式（未找到图标文件 $Icon）：$Lnk"
}
