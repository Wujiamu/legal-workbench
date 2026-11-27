# Windows 10/11 实机验收自检清单

本清单用于**在真实 Windows 10/11 宿主机**上独立核验本轮 Windows 适配成果。当前项目已在 macOS 完成全部代码改造、跨平台语义模拟单测与全量回归；在真机走通下表全部实测项之前，Windows 实机端到端状态仍记为**待真机验证**。

## 0. 环境准备（NTFS 本地磁盘）

1. 将项目放在 **NTFS 本地磁盘**目录（建议测试中文路径，如 `C:\工作区\法律工作台`；禁止放到网盘/同步盘或 FAT32/exFAT 分区，因无覆盖发布依赖 NTFS 同卷 `os.link`）。
2. 安装运行前提：
   - **Python 3.13**（确保安装时含 `pythonw.exe` 与 `py` 启动器）
   - **uv**
   - **Node.js `22.22.2+` / `24.15.0+` / `26+`** 与 `npm`
   - **PostgreSQL 18**（如使用官方安装包默认路径 `C:\Program Files\PostgreSQL\18\bin`，无需手动注册系统服务；若装在其他路径，后续在 `.env` 配置 `LWB_PG_BIN` 或在初始化前先设 `$env:LWB_PG_BIN`）
3. 确保端口 `55432`、`8765`、`5173` 未被占用。

---

## 1. 依赖安装、OCR 模型与初始化

在 PowerShell 进入项目根目录：

```powershell
cd backend; uv sync --locked; cd ..
cd frontend; npm ci; npm run build; cd ..
backend\.venv\Scripts\python.exe scripts\install_ocr_models.py
scripts\lwb.cmd doctor
```

- [ ] `doctor` 输出 `Python`、`node`、`npm`、`PG_BIN`、`OCR_MODEL_DIR` 均为 `OK`（`PostgreSQL` 与 `DATA_ROOT` 在 `init` 前可能提示未就绪，属正常）。
- [ ] 执行全新初始化（如 PG 装在自定义目录先设 `$env:LWB_PG_BIN = "D:\PostgreSQL\18\bin"`）：
  ```powershell
  scripts\lwb.cmd init
  ```
- [ ] 在中文 Windows（默认系统代码页 CP936）下，`initdb` 通过 `--locale-provider=builtin --builtin-locale=C.UTF-8` 顺利完成，无字符集编码冲突报错；`.env` 与 `var\runtime\pgpass` 正常生成，`alembic upgrade head` 与 `backfill_progress` 完成，输出中文无乱码。
- [ ] 浏览器访问 `http://127.0.0.1:8765/` 正常，按 `Ctrl+C` 可停止服务及解析 worker。
- [ ] 执行 `scripts\lwb.cmd stop` 停止本地 PostgreSQL；再执行 `scripts\lwb.cmd doctor`，确认 `PG_BIN`、`DATA_ROOT`、`OCR_MODEL_DIR` 为 `OK`。

---

## 2. 桌面双击启动器与断库自愈（`素章.bat`）

- [ ] 确保 PostgreSQL、Web 与 Worker 均已停止，在资源管理器**双击项目根目录 `素章.bat`**：
  - [ ] 无长期滞留的黑色 CMD/控制台窗口，也不弹出多余后台 `pg_ctl` / `python` 窗口；
  - [ ] 45 秒内默认浏览器自动打开 `http://127.0.0.1:8765/`，首页渲染正常；
  - [ ] `var\runtime\launcher.log` 记录启动过程且中文正常（UTF-8）。
- [ ] 模拟「网页进程仍在监听但数据库被停止」：
  1. 执行 `scripts\lwb.cmd stop` 仅停掉 PostgreSQL；
  2. 再次双击 `素章.bat`；
  3. 确认无需手工重开 Web，启动器自动拉起 PostgreSQL 并补启解析 worker，最终打开可用首页，`launcher.log` 记有「尝试拉起 PostgreSQL 并恢复解析 worker」。
- [ ] 连续快速双击两次 `素章.bat`，确认 `msvcrt.locking` 排他锁阻止重复拉起冲突，仅打开单个可用服务实例。

---

## 3. 自动化全量测试与构建验证

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
npm test
npm run build
```

- [ ] 后端全量 `pytest -q` **0 失败、0 跳过**——特别确认在真 Windows 上 `test_real_windows_ntfs_junction_rejected`（通过 `_winapi.CreateJunction` 创建真实 NTFS Junction 并被 `Storage.managed_path` 拦截）**实际执行并通过**（非 skipped）。
- [ ] 若使用普通账户且未开启 Windows「开发者模式」，确认符号链接用例仅按预期优雅跳过（`WinError 1314`），其余业务、OCR、并发、备份恢复用例全部通过；在开启开发者模式或管理员终端重跑一次，确认全量 0 skip。
- [ ] 前端 `npm test`（含 `src/ui/shortcuts.test.tsx`）与 `npm run build` 全部通过。

---

## 4. 仿真案卷全流程、OCR 超时与备份恢复（`TEST-ONLY`）

- [ ] 新建独立案件 `TEST-ONLY Windows 实机验收案`，通过网页上传 `acceptance_simulation\dossier\` 全部 10 份材料：
  - [ ] 后台 worker 自动消费解析队列；`03_最高额连带责任保证函（扫描件）.pdf` 与 `07_催款函与签收回执（拍照件）.png` 通过离线 RapidOCR 识别成功；`10_损坏扫描附件（模拟损坏）.pdf` 安全标记失败且不拖垮同批；
  - [ ] 验证**线程版 OCR 超时保护**：在测试环境或临时设 `LWB_PARSE_TIMEOUT_SECONDS=0.001` 验证扫描页超时返回 `failed / timeout` 且 worker 不崩溃退出；
  - [ ] 进入办案进度页与主体关系图工作区，核对提示文案展示 `Ctrl+Z`（而非 `⌘Z`），且键盘 `Ctrl+Z` 撤销生效；中文输入法候选确认按回车不误触发检索或表单提交；
  - [ ] 在工作区对某材料创建包含 Windows 边界字符或长名称的工作副本，验证读写、编辑与登记新版本均正常。
- [ ] CLI 与 MCP 连通及备份恢复往返：
  ```powershell
  cd backend
  .venv\Scripts\python.exe -m legal_workbench.cli list-cases --limit 20
  .venv\Scripts\python.exe ..\scripts\rehearse_backup_restore.py
  ```
  - [ ] CLI stdout 为单行 UTF-8 合法 JSON，中文显示正常无乱码；
  - [ ] `rehearse_backup_restore.py` 输出 `RESTORE_REHEARSAL_OK = True` 与 `RESTORED CONSISTENCY ok = True`；
  - [ ] 测试完按需清理测试案件并确认正式库 `check-consistency` 输出 `"ok": true`。
