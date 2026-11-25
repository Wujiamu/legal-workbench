; 素章 PlainFolio — Windows 安装向导（Inno Setup 6）
;
; 由 packaging/build_windows_exe.py 调用编译，也可手工执行：
;   ISCC.exe /DAppVersion=0.1.0 ^
;            /DSourceDir=packaging\dist\PlainFolio ^
;            /DOutputDir=packaging\dist packaging\windows\plainfolio.iss
;
; 安全边界（改动本文件前必读）：
;   1. 程序安装在 {localappdata}\Programs\PlainFolio，全程无需管理员权限；
;   2. 用户数据目录 %LOCALAPPDATA%\PlainFolio（案件库、受管文件、备份、.env）
;      完全由程序自己创建和管理，安装、升级、卸载都不得读写或删除它；
;   3. 升级采用覆盖安装程序文件的方式，AppId 保持不变，用户数据原地保留。

#define AppName "PlainFolio"
#define AppDisplayName "素章 PlainFolio"
#define AppExeName "PlainFolio.exe"
#define AppPublisher "素章 PlainFolio"
#define AppUrl "https://github.com/Wujiamu/legal-workbench"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\packaging\dist\PlainFolio"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\packaging\dist"
#endif
#define IconSource "..\..\scripts\legal-workbench.ico"

[Setup]
; AppId 必须永久稳定：升级安装靠它识别同一个产品，改掉会变成并存的两份安装。
AppId={{8C1F5A64-3B7E-4F52-9C2D-6E0A7B4D1F38}
AppName={#AppDisplayName}
AppVersion={#AppVersion}
AppVerName={#AppDisplayName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
DefaultDirName={localappdata}\Programs\PlainFolio
DefaultGroupName={#AppDisplayName}
DisableProgramGroupPage=yes
; 免管理员：普通用户即可安装，也避免 UAC 提权后把文件写到别人的配置文件里。
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=PlainFolio-Setup-{#AppVersion}-x64
SetupIconFile={#IconSource}
UninstallDisplayIcon={app}\legal-workbench.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
; Inno Setup 6 官方安装包不自带简体中文；构建机需把 ChineseSimplified.isl 放入
; Inno Setup 6\Languages\（见 docs/PACKAGING.md）。缺该文件时编译会直接报错，
; 由 build_windows_exe.py 在编译前先行检查并给出明确提示。
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "addtopath"; Description: "将 PlainFolio 加入当前用户 PATH（便于命令行与 MCP 客户端直接调用）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 只安装程序文件。SourceDir 由构建脚本指向 PyInstaller 的 onedir 产物
; （PlainFolio.exe + _internal\，其中 _internal\runtime 是便携 PostgreSQL 18 与 OCR 模型）。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#IconSource}"; DestDir: "{app}"; DestName: "legal-workbench.ico"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppDisplayName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\legal-workbench.ico"
Name: "{group}\卸载 {#AppDisplayName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\legal-workbench.ico"; Tasks: desktopicon

[Registry]
; 可选：只追加到当前用户 PATH（HKCU），不碰系统 PATH，也不需要管理员权限。
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Flags: preservestringtype

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppDisplayName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 仅清理安装目录内的临时日志。用户数据目录 %LOCALAPPDATA%\PlainFolio
; 不在任何卸载指令中出现：那里是案件库与备份，误删无法恢复。
Type: files; Name: "{app}\*.log"
