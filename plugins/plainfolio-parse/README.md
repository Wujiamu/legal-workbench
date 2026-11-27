# 素章 · 离线阅卷解析器（独立 Agent 插件）

零数据库、免装工作台：把一个本地材料目录解析成**可复核的结构化文本**——正文按
页／工作表／段落分单元，并带有精确金额（分币种）、日期与跨文件强指纹碰撞清单。

本插件只是"接线"：一个 stdio MCP 服务（6 个工具）＋ 一份办案 Skill，真正干活的
是仓库内独立包 `packages/plainfolio-parse`。

## 安装

```bash
# 1) 核心解析内核（TXT/DOCX/XLSX/PDF 原生文字层，无需 OCR 运行时）
pip install ./packages/plainfolio-parse

# 2) 需要读扫描件与图片时再加 OCR 可选组
pip install './packages/plainfolio-parse[ocr]'

# 3) 在插件里指给 MCP 客户端：python3 ${PLUGIN_ROOT}/scripts/run-mcp.py
```

`python3 scripts/run-mcp.py --print-plan` 会打印实际将要执行的命令（JSON），用来
排查"MCP 客户端连不上"到底是解释器没装还是包没找到。定位顺序：
`PLAINFOLIO_PARSE_BIN` → `PATH` 中的 `plainfolio-parse-mcp` → 仓库 `backend/.venv`
或 `packages/plainfolio-parse/.venv`（配 `PYTHONPATH=…/packages/plainfolio-parse/src`）
→ 当前解释器（须已装 `plainfolio_parse`）。

## 两个环境变量

| 变量 | 作用 |
|---|---|
| `PLAINFOLIO_ALLOWED_ROOTS` | 文件访问白名单（`os.pathsep` 分隔，Windows 用 `;`）。**默认只允许当前工作目录**，不设置就只能处理 CWD 及其子目录。 |
| `PLAINFOLIO_OCR_MODEL_DIR` | RapidOCR 模型根目录，其下 `rapidocr/` 需含 `manifest.json` 与 `det/rec/cls.onnx`。也接受工作台的 `LWB_OCR_MODEL_DIR`。 |

`PLAINFOLIO_OCR_MODEL_DIR` 只指目录，不会在运行期下载任何模型：`manifest.json` 里
记录的 SHA-256 必须与实际文件一致，否则该目录视为无效。

## 6 个 MCP 工具

| 工具 | 用途 |
|---|---|
| `parse_policy` | 只读：当前允许的根目录、来源（cwd / 环境变量）、OCR 是否可用及原因、支持的扩展名 |
| `parse_file` | 解析单个文件，可选写出 Obsidian Markdown（不得覆盖输入文件） |
| `parse_folder` | 解析整个目录（跳过隐藏文件与目录），逐份返回状态与单元 |
| `scan_exact_values` | 精确金额（按币种分别汇总，绝不跨币种相加）与日期分布 |
| `scan_identifier_bridges` | 跨文件强指纹碰撞：手机号 / USCC / 银行账号 / 邮箱 |
| `preview_table` | 表格列名与前 N 行预览（`.xlsx` / `.csv`） |

## 缺 OCR 时会发生什么

不会因为没装 OCR 就整份解析失败：

- 纯文本、DOCX、XLSX 与**带原生文字层**的 PDF 照常返回 `complete`；
- 需要 OCR 的页面/图片逐页标为 `failed`，`error` 以 `ocr_unavailable:` 开头并写明
  是缺依赖还是缺模型；
- `parse_policy` 的 `ocr.reason` 给出可执行的修复指令。

需要"宁可整体失败也不要半份结果"的场景（工作台自身就是这样）可把
`ParseConfig.ocr_missing_policy` 设为 `"error"`，本插件默认是 `"degrade"`。

## 红线

- **涉密、涉个人隐私或当事人禁止上云的案卷，严禁接入云端模型**，只用本机离线模型。
- **原件只读**：输入只以只读方式打开；输出路径与任何输入文件相同即报错，不做
  "覆盖前备份"。
- 解析结果不是事实认定：OCR 会认错字，金额与编号必须回到原件页码复核。

## 许可

Apache-2.0，随仓库 `LICENSE` 分发。办案规程见
[`skills/plainfolio-parse/SKILL.md`](skills/plainfolio-parse/SKILL.md)。
