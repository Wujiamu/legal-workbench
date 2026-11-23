# 复用与依赖来源

本文件记录 V0.1 实际使用的外部组件：版本、许可证、用途，以及本项目自己写的部分。

## 关于"六项目审查"的边界

任务包第 1.1 节说明：接受用户提供的六项目审查结论，不重复全面审查；第 4 节 T20 要求"原审查的 commit 未给出时明确区分，不能把新拉取版本冒充已审查版本"。

**本次交接没有收到原审查的六项目名称与对应 commit。** 因此：

- 下表记录的是**本项目实际安装并运行的版本**，来源为 `backend/uv.lock` 与 `frontend/package-lock.json` 的解析结果，不是"已审查 commit"。
- 下表**不表示**这些版本经过安全审计或与任何审查结论对应。
- 若后续拿到原审查 commit，应逐项比对版本是否一致；不一致时需重新评估，而不是沿用本表。

## Python 依赖（后端）

| 包 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| alembic | 1.16.1 | MIT | 迁移；URL 由 `LWB_MIGRATION_URL` 注入，`alembic.ini` 不写凭据 |
| fastapi | 0.115.12 | MIT | HTTP 路由与依赖注入 |
| uvicorn | 0.34.3 | BSD-3-Clause | ASGI 服务器，仅绑定 `127.0.0.1` |
| pydantic | 2.11.5 | MIT | 协议模型唯一来源（`schemas.py`） |
| sqlalchemy | 2.0.41 | MIT | ORM、CAS 更新、会话管理 |
| psycopg[binary] | 3.2.9 | LGPL-3.0 | PostgreSQL 驱动；`postgresql+psycopg` 方言 |
| httpx | 0.28.1 | BSD-3-Clause | 测试内的 HTTP 客户端 |
| mcp | 1.30.0 | MIT | FastMCP stdio 服务 |
| python-docx | 1.2.0 | MIT | DOCX 解析（含嵌套表格、文档顺序） |
| openpyxl | 3.1.5 | MIT | XLSX 解析（公式与缓存值、sheet 元数据） |
| pypdfium2 | 5.15.2 | BSD-3-Clause / Apache-2.0 / 依赖各自许可 | PDF 文本层与页面渲染（OCR 前的位图） |
| pillow | 13.0.0 | MIT-CMU | 图片读取、多帧 TIFF、图像尺寸限制 |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 | 本地 OCR 引擎（PP-OCRv4 模型） |
| onnxruntime | 1.31.1 | MIT | rapidocr 的推理后端（传递依赖） |
| pytest | 8.4.1 | MIT | 测试（dev） |
| reportlab | 5.1.0 | BSD | 测试中生成合成 PDF（dev） |

OCR 模型文件（`det.onnx`、`rec.onnx`、`cls.onnx`）由 `scripts/install_ocr_models.py` 从已安装的 `rapidocr_onnxruntime` 包内复制到 `var/models/rapidocr/`，并写入 SHA-256 清单。模型来自 PP-OCRv4（Apache-2.0）。运行时**绝不下载**：清单校验失败即抛 `dependency_missing`。

## 前端依赖

| 包 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| react / react-dom | 19.1.0 | MIT | 界面 |
| react-router | 7.18.5 | MIT | 路由 |
| vite | 6.4.6 | MIT | 构建和 Vitest 共用（dev） |
| vitest | 5.1.0 | MIT | 前端组件测试 CLI（dev） |
| typescript | 5.8.3 | Apache-2.0 | 类型检查（dev） |
| @vitejs/plugin-react | 4.5.1 | MIT | 构建插件（dev） |
| openapi-typescript | 7.8.0 | MIT | 由 `docs/openapi.json` 生成前端类型（dev） |
| @xyflow/react | 12.12.1 | MIT | 图形视图（React Flow）：自定义节点、复合嵌套、缩放平移；仅进度图形视图懒加载 |
| elkjs | 0.12.1 | EPL-2.0 OR GPL-3.0-or-later（选用 EPL-2.0） | 图形视图嵌套自动布局，在 Web Worker 中计算；布局坐标不入库 |

### 进度图形依赖评估（2026-11-16，P1）

- 选择依据见 [总体计划 K10](changes/20261116-办案进度可视化.md)：React Flow 节点即 React 组件，可复用现有样式与表单；elkjs 支持复合节点分层布局。两者均为 npm 包，随构建离线提供，运行时不下载。
- `npm audit`（完整锁文件）0 漏洞。React Flow  attribution 保留（MIT 要求）。
- 体积（Vite 生产构建，gzip）：主包 89.36 KB **不变**；进度页懒加载 chunk 74.23 KB；elk 布局 chunk 448.29 KB，仅在打开图形视图时加载一次，Worker 与主线程回退共享同一 chunk。
- Chromium 实测通过（布局、嵌套、关系边、交互）；Safari 与真实中文输入法待承办人验证。elkjs 为双许可，本项目按 EPL-2.0 使用；若后续分发方式变化需复核许可兼容性。

## 本项目自行编写

除上表所列库之外，`backend/src/legal_workbench/`、`backend/tests/`、`backend/migrations/`、`frontend/src/`、`scripts/` 均为本项目编写的代码，没有从其他仓库复制整段实现。

几个容易误判为"复用"的部分，实际是自写：

- `storage.py` 的无覆盖发布：用标准库 `os.link` + `fsync`，无第三方存储 SDK。
- `textnorm.py` 的归一化与偏移映射：自写，未用 `unicodedata.normalize` 之外的外部库。
- `api/security.py` 的会话与 CSRF：进程内 `dict` + `hmac.compare_digest`，未引入会话框架。
- 检索的四种模式：纯 SQLAlchemy 查询 + PostgreSQL `pg_trgm` 扩展，未使用检索引擎。

## 数据库扩展

| 扩展 | 来源 | 用途 |
|---|---|---|
| `pg_trgm` | PostgreSQL contrib（PostgreSQL License） | 字面检索的 trigram 索引；短词（<3 字）退化为扫描 |

## 系统依赖

- PostgreSQL 18（PostgreSQL License），macOS Homebrew 安装于 `/opt/homebrew/opt/postgresql@18/bin`，Windows 默认安装于 `C:\Program Files\PostgreSQL\18\bin`（均支持 `LWB_PG_BIN` 与 `PATH` 解析）。
- Python 3.13。前端构建与测试使用 npm，Node.js 版本范围见 [前端清单](../frontend/package.json)。
- 系统字体（macOS `Hiragino Sans GB` / Windows 微软雅黑 `msyh.ttc` / 黑体 `simhei.ttf` / 宋体 `simsun.ttc` / Linux Noto CJK）仅用于测试及仿真样本生成，不参与生产解析路径。
