# 桌面安装包（macOS `.dmg` / Windows `.exe`）

> 适用基线：`v0.1.0` + `0009_case_metadata`（承接 [通用 Agent 插件包](changes/20261124-通用Agent插件包.md)）。
>
> 本文说明打包形态、目录布局、构建流程与实机自检清单；当前构建与真机验收进度统一见 [NEXT_STEPS.md](NEXT_STEPS.md)。仓库已配置 `.github/workflows/release-installers.yml` 自动产出未签名的双端安装包与 SHA-256 校验文件。

## 1. 两种运行形态

| 维度 | 源码态（开发机 / 现有用户） | 打包态（`.dmg` / `.exe`） |
|---|---|---|
| 程序目录 | 仓库根目录 | `.app` 内的 `Contents/MacOS`，或 `%LOCALAPPDATA%\Programs\PlainFolio`（只读） |
| 数据目录 | 仓库根目录（`var/`、`.env`） | macOS `~/Library/Application Support/PlainFolio`；Windows `%LOCALAPPDATA%\PlainFolio` |
| 解析顺序 | `LWB_HOME` → 实例指针 `home` → `ROOT` | `LWB_HOME` → 实例指针 `home` → 平台数据目录 |
| 启动 | `scripts/lwb start` / 双击 `法律工作台.bat` / `scripts/lwb-launch` | 双击应用图标 → `legal_workbench.desktop_launcher` |
| 子进程 | `python -m uvicorn …` / `-m legal_workbench.worker` | 同一可执行文件 `--run-server` / `--run-worker` |
| MCP 入口 | `plugins/plainfolio/scripts/run-mcp.py` → `backend/.venv/bin/python -m legal_workbench.mcp_server` | 同一启动器 → `<可执行文件> --run-mcp` |
| Agent 授权 | `scripts/lwb agent-*`（无依赖时自动转发到 `backend/.venv` 的 Python） | 安装目录里没有 `scripts/`：用 `<可执行文件> --run-cli agent-grant <case_id>`（同一份 `agent_access_cli` 实现）。策略文件落在平台数据目录下的 `var/runtime/agent-access.json` |

两态的判定只在一个地方：`legal_workbench/runtime_paths.py`。

- `is_packaged_mode()`：`sys.frozen` 或 `LWB_PACKAGED=1|true|yes|on`。
- `resolve_bundle_root()`：`LWB_BUNDLE_ROOT` → 冻结资源根（`sys._MEIPASS` / 可执行文件同级 / `_internal` / `Contents/Resources`）→ 仓库根。
- `resolve_home_dir()`：`LWB_HOME` → 指针 `home` → **仅打包态**平台数据目录 → 仓库根。
- 源码态严格保持「回退到 `config.ROOT`」，且 `config.ROOT` 被测试替换后仍然生效。

## 2. 磁盘布局

```
<数据目录>/                       macOS: ~/Library/Application Support/PlainFolio
  .env                            0600，含两个角色的随机口令（绝不随安装包分发）
  var/postgres/                   PostgreSQL 18 集群（initdb 创建）
  var/data/                       受管文件根（DATA_ROOT）
  var/backups/                    备份；升级前自动写 pre-upgrade-<时间戳>/
  var/runtime/postgres.log        集群日志
  var/runtime/pgpass              0600
  var/runtime/desktop.lock        单实例文件锁
  var/runtime/desktop-state.json  运行中的进程号（--stop 用）
  var/runtime/init-failure.log    首次初始化失败原因（不含口令）
  var/runtime/agent-access.json   0600，Agent 访问策略；**默认不存在**（= deny_all），
                                  只在承办人执行 scripts/lwb agent-grant 后生成
  var/models/rapidocr/            仅源码态；打包态改用内嵌模型
```

```
<程序目录>/_internal/             PyInstaller onedir 的资源根（sys._MEIPASS，只读）
  frontend/dist/                  已构建的网页
  backend/alembic.ini
  backend/migrations/
  plugins/plainfolio/
  runtime/postgres/{bin,lib,share}  便携 PostgreSQL 18
  runtime/models/rapidocr/          OCR 模型（det/rec/cls.onnx + manifest.json）
  bundle-manifest.json              各组件路径与 SHA-256（stage_bundle 生成）
```

## 3. 构建前提

通用：

- Python **3.13**（与 `backend/pyproject.toml` 的 `requires-python` 一致）。
- Node.js **22.22.2+ / 24.15.0+ / 26+** 与 `npm`（构建前端）。
- PyInstaller 6.x：`backend/.venv/bin/pip install pyinstaller`（仅构建机需要，**不写入** `pyproject.toml` / `uv.lock`）。
- 仓库内第一方独立包（`packages/plainfolio-dossier`、`packages/plainfolio-parse`）：桌面端由 [plainfolio.spec](../packaging/plainfolio.spec) 的 `pathex` 与 `hiddenimports` 随主包打入冻结产物；单独分发时可通过 `uv build --out-dir dist-packages packages/plainfolio-dossier` 与 `uv build --out-dir dist-packages packages/plainfolio-parse` 产出轻量 `.whl` / `.tar.gz`（`.github/workflows/release-installers.yml` 的 `build-packages` 任务同步上传 `plainfolio-python-packages` 产物）。
- 外部二进制（便携 PostgreSQL 18、OCR 模型）由构建机自备，脚本只校验、不下载。

macOS：

- Xcode Command Line Tools（`hdiutil`、`codesign`、`xcrun`）。
- 产物名带架构：`PlainFolio-<版本>-macos-arm64.dmg`。

Windows：

- 64 位 Windows 10/11 构建机；Python 3.13 x64。
- **Inno Setup 6**（`ISCC.exe`）。官方安装包**不自带简体中文**，需把 `ChineseSimplified.isl`
  （jrsoftware 的 Unofficial translations）放进 `Inno Setup 6\Languages\`。
  `build_windows_exe.py` 在编译前会检查该文件，缺失时直接报错并给出路径。
- 产物名固定为 `PlainFolio-Setup-<版本>-x64.exe`。

## 4. 外部二进制收集

### 4.1 便携 PostgreSQL 18（约 40–60 MB）

需要**可重定位**（relocatable）的二进制分发，而不是装到系统里的实例：

- Windows：EDB 官方 PostgreSQL 18 的 zip 二进制包，解压后目录内含 `bin/ lib/ share/`。
- macOS：同样使用可重定位构建（EDB binaries，或自建 `--prefix` 安装）。
  Homebrew 的 `postgresql@18` 目录带有指向 Cellar 的绝对路径，直接拷走会在别的机器上
  `dyld: Library not loaded`；若必须使用，先用 `otool -L bin/postgres` 逐个核对并修正
  `@rpath`。**这一项必须在真机验证**（见第 10 节）。

必须存在的程序（缺一即拒绝打包）：

```
initdb  pg_ctl  postgres  psql  createdb  pg_dump  pg_restore        （Windows 带 .exe）
```

`initdb` 还需要同目录的 `share/`（建库 SQL 脚本），因此要整体拷贝 PG 根目录，不能只拷 `bin/`。

校验：

```bash
python3 packaging/stage_bundle.py --dry-run --pg-dir /path/to/postgresql18 --models-dir /path/to/models
```

### 4.2 OCR 模型（约 16 MB）

```bash
cd backend && .venv/bin/python ../scripts/install_ocr_models.py
# 产物：<home>/var/models/rapidocr/{det,rec,cls}.onnx + manifest.json
```

`--models-dir` 既可以指向 `var/models`（含 `rapidocr/`），也可以直接指向 `var/models/rapidocr`；
两种都会归一到 `runtime/models/rapidocr/`，因为引擎固定读 `<ocr_model_dir>/rapidocr`。

### 4.3 前端

```bash
cd frontend && npm ci && npm run build      # 产物 frontend/dist/
```

`frontend/dist` 被 `.gitignore` 忽略，全新 clone 必须先构建，否则打包脚本会拒绝执行。

## 5. 构建命令

```bash
# 只校验、不写盘（macOS / Windows / Linux 均可执行，退出码 0 表示计划可用）
python3 packaging/stage_bundle.py --dry-run --allow-missing-runtime
python3 packaging/build_macos_dmg.py --dry-run
python3 packaging/build_windows_exe.py --dry-run

# macOS 实机构建
python3 packaging/build_macos_dmg.py \
    --pg-dir /path/to/postgresql18 --models-dir var/models

# Windows 实机构建（PowerShell / cmd，需先装 Inno Setup 6）
python packaging\build_windows_exe.py ^
    --pg-dir D:\postgresql18 --models-dir D:\models
```

`--dry-run` 的语义：

- 校验源文件（`AppIcon.icns`、`plainfolio.spec`、`frontend/dist/index.html`、`backend/migrations`、
  `plainfolio.iss` 的结构与卸载安全规则），编译 spec 检查语法；
- **不**要求便携 PostgreSQL 18 与 OCR 模型存在（报告 `runtime.bundled=false`），
  也不调用 PyInstaller / hdiutil / ISCC，不写任何文件；
- 输出确定的构建计划 JSON（含版本、架构、产物路径、阶段列表、工具可用性）。

`stage_bundle.py` 没有 `--dry-run` 时默认**严格**：缺少 PG18 或 OCR 模型即退出码 2；
只有显式 `--allow-missing-runtime` 才放行。

## 6. 首次运行与升级

首次双击（数据目录为空）时，启动器按顺序执行：

1. 取得 `var/runtime/desktop.lock` 文件锁（第二次双击只等待，超时后直接打开浏览器）；
2. 判定状态：`fresh`（无 `.env` 且无 `var/postgres`）/ `partial` / `ready`；
3. `fresh` → 建 `var/{postgres,data,runtime,backups}`，`initdb`（Windows 带
   `--locale-provider=builtin --builtin-locale=C.UTF-8`），写 `postgresql.conf`（`127.0.0.1:55432`），
   写 `var/runtime/pgpass`（0600），启动集群，建 `lwb_migrator` / `lwb_app` 与 `legal_workbench` 库，
   写 `.env`（0600，两个角色各自独立的 32 字节随机口令），跑 `alembic upgrade head` + 进度回填；
4. 写实例指针 `~/.plainfolio/instance.json`（只含 `home` / `project_root` / `updated_at`，
   打包态另加 `mode` 与 `executable`；**绝不含口令或连接串**），Codex / DSH 插件无需任何配置即可发现；
5. 启动集群 → 比较数据库 `alembic_version` 与代码 head → 一致则跳过备份；
6. 启动 worker 与 API（冻结态通过 `--run-worker` / `--run-server` 复用同一可执行文件）；
7. 轮询 `/api/v1/health` 与 `/api/v1/cases?limit=1`，两者都通过才打开浏览器；
8. 退出（关窗口、`Ctrl+C`、`--stop`）时先停子进程，再 `pg_ctl stop -m fast`。

失败处理（关键不变量）：

| 失败点 | 行为 |
|---|---|
| 初始化任一步失败 | 停止临时集群 → 删除 `.env` / `pgpass` / `var/postgres` → 写 `var/runtime/init-failure.log`（只记命令名与退出码，不记命令行）→ 不写实例指针 |
| 已有 `.env` 但无 `var/postgres`（或反之） | 判为 `partial`，拒绝自动继续，提示查看 `init-failure.log` |
| 有待执行迁移且备份失败 | **中止启动**，原库与备份原样保留 |
| 备份成功但迁移失败 | 报错退出，保留备份；下次启动重新比较 revision |

升级备份写到 `var/backups/pre-upgrade-<YYYYmmdd-HHMMSS>/`，复用 `backup.py`：
`complete: true` 的清单 + `verify_backup()` 全量哈希复核，两者都通过才会执行迁移。

## 7. 代码签名与分发

未签名安装包可以运行，但用户会看到系统警告，因此正式分发前应签名：

macOS：

```bash
codesign --force --deep --options runtime \
    --sign "Developer ID Application: <名称> (TEAMID)" PlainFolio.app
xcrun notarytool submit PlainFolio-0.1.0-macos-arm64.dmg \
    --keychain-profile <profile> --wait
xcrun stapler staple PlainFolio-0.1.0-macos-arm64.dmg
```

- 公证要求 hardened runtime；`--deep` 只是便捷写法，正式流程建议对嵌套二进制逐个签名。
- 内嵌的 `runtime/postgres/*` 与 `.onnx` 模型也要一并签名，否则公证会失败。
- 本机自测可用 `codesign --sign -`（ad-hoc），但不能分发给他人。

Windows：

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 PlainFolio-Setup-0.1.0-x64.exe
```

- 未签名的 `.exe` 会触发 SmartScreen「未知发布者」；EV/OV 证书可减少提示。
- 安装包与内嵌的 `PlainFolio.exe` 都应签名。

## 8. 实机验收自检清单（**待执行**）

> 本轮未执行以下任何一项。执行时请记录日期、机器、macOS/Windows 版本与构建机架构。

macOS：

- [ ] 干净用户账户（无 `~/Library/Application Support/PlainFolio`、无 `~/.plainfolio/instance.json`）。
- [ ] 挂载 `.dmg` → 拖入「应用程序」→ 首次双击：自动建库、迁移、打开浏览器，无终端窗口。
- [ ] `.env` 与 `var/runtime/pgpass` 权限为 `0600`；`instance.json` 不含口令/连接串。
- [ ] 连续快速双击两次：只有一个服务实例，第二次仅打开浏览器。
- [ ] 关掉浏览器后再次双击：不重复初始化，直接进入已有数据。
- [ ] 退出应用后 `pg_ctl status` 为停止；`--status` 报告 `postgres:false`。
- [ ] `--headless` 不打开浏览器；`--init-only` 只建库；`--stop` 停止服务与集群。
- [ ] 升级演练：保留旧数据目录，装新版本 → `var/backups/pre-upgrade-*` 存在且 `manifest.json`
      的 `complete` 为 `true`，`verify_backup()` 通过。
- [ ] 故障注入：令备份失败（如临时移走 `pg_dump`）→ 启动中止，数据库仍是旧 revision。
- [ ] Codex / DSH 通过 `plugins/plainfolio` 启动 MCP：`run-mcp.py --print-plan` 显示
      `interpreter: packaged`，命令为 `<可执行文件> --run-mcp`，`initialize` + `list_tools` 正常。
- [ ] 把 `.app` 拖到废纸篓后，数据目录仍在。

Windows：

- [ ] 中文 Windows 10/11 x64，**普通用户**账户安装，全程无 UAC 提权。
- [ ] 安装到 `%LOCALAPPDATA%\Programs\PlainFolio`；桌面与开始菜单快捷方式图标正常。
- [ ] 首次启动：`initdb` 在 CP936 环境下无编码错误，网页中文无乱码。
- [ ] 覆盖升级（同版本重装 + 新版本升级）：程序文件更新，`%LOCALAPPDATA%\PlainFolio` 内的
      案件、`.env`、`var\postgres` 保持不变。
- [ ] 卸载：程序目录被删除，`%LOCALAPPDATA%\PlainFolio` **仍然存在**（这是硬性要求）。
- [ ] 可选任务「加入 PATH」生效后，新开终端能直接运行 `PlainFolio --status`。
- [ ] 杀毒软件/Defender 不拦截（onedir 已避免每次启动解压，但仍需实测）。
- [ ] `%LOCALAPPDATA%\PlainFolio\var\runtime\postgres.log` 与 `init-failure.log` 中文正常。

## 9. 故障排查

| 现象 | 先看 | 说明 |
|---|---|---|
| 双击无反应 | `var/runtime/init-failure.log` | 首次初始化失败原因（不含口令） |
| 页面打不开 | `var/runtime/postgres.log` | 集群是否起来；`pg_ctl status` 退出码 |
| 提示「另一个实例正在启动」 | `var/runtime/desktop-state.json` | 锁被占用；`--stop` 后再试 |
| `--status` 显示 `service:false` | `var/runtime/postgres.log`、控制台输出 | 服务未就绪（数据库读不出来也算未就绪） |
| 升级后行为异常 | `var/backups/pre-upgrade-*` | 备份可 `verify_backup()` 后按 [备份与恢复](BACKUP_RESTORE.md) 回退 |

打包态下 `doctor` 不再把缺少 `node` / `npm` 判为失败（显示 `SKIP`），因为安装包自带构建产物；
`BUNDLE_ROOT` / `HOME` / `PACKAGED` 三行用于确认解析到的是哪个目录。

## 10. 验证进展与已知限制

已通过 CI 与本机实测确认：

1. **GitHub Actions 双端真实构建与冻结烟测通过**（Run `45628787812`）：产出 `PlainFolio-Setup-0.1.0-x64.exe`（~140 MB）与 `PlainFolio-0.1.0-macos-arm64.dmg`（~343 MB）；`plainfolio.spec` 收集清单（`pypdfium2_raw`/`lxml`/`rapidocr`/前端与迁移）及 `--version`、`--init-only`、`--start-db`、`--run-doctor`、`--run-cli check-consistency`/`agent-status`、`--stop` 在 `windows-latest` 与 `macos-15` 全部通过；Windows `.iss` 与 `ChineseSimplified.isl` 已完成云端编译。
2. **macOS `.dmg` 本机实测确认**：SHA-256、`arm64` 包体、内嵌 PG `18.3` 与 RapidOCR 模型完整性、隔离目录冻结 `--version`/`--status`/`run-mcp.py --print-plan` 均通过；`otool -L` 确认 `PlainFolio` 与内嵌 `postgres`/`initdb`/`psql`/`pg_ctl` 全部依赖系统库与 `@loader_path/../lib/...`，无 Homebrew 或构建机硬编码路径。
3. **当前签名状态（实测记录）**：macOS `codesign -dvv` 为 `adhoc` 临时签名、`TeamIdentifier=not set`，`spctl` 不通过（未签名的 `.dmg` / `.exe` 会触发 Gatekeeper 与 SmartScreen 警告，详见第 7 节）。

仍待实机验收：

1. **干净 macOS 用户账户的图形化双击冷启动**（拖入 `/Applications` 后双击建库并自动打开浏览器，此前因宿主机 `55432` 端口被在跑开发库占用暂缓）。
2. **真实中文 Windows 10/11 x64 普通用户宿主机安装与卸载测试**（见第 8 节 Windows 清单，含 CP936 与卸载保留 `%LOCALAPPDATA%\PlainFolio`）。
3. **正式代码签名与公证**（未接入 Developer ID / Authenticode 证书）。
4. **安装包内 MCP 派发在真实 Codex / DSH 客户端跑通完整会话**（目前仅在本机通过 `run-mcp.py --print-plan` 与云端 stdio 测试）。
5. 打包态的 `--stop` 通过进程号终止启动器；若启动器被强杀，`desktop-state.json` 会残留，`--stop` 仍会停集群，残留文件在下次启动时被覆盖。
