# plainfolio-parse · 素章离线阅卷解析内核（零数据库）

把本地案卷材料变成**可复核的结构化文本**：TXT / DOCX / XLSX / PDF / 图片逐单元解析
（页码、工作表、段落、行号定位齐全），并附带三件零数据库能力：

- **精确金额与日期**：金额必须带币种（`USD` / `CNY` / `人民币` / `元` / `万元` …），
  按币种分别汇总，绝不跨币种相加；表格单元格的「金额｜币种」按同行配对。
- **强指纹暗线排查**：手机号、带校验码的统一社会信用代码（GB 32100）、银行账号、
  邮箱，找出**出现在两份以上文件**里的同一指纹；身份证号与物流单号有专门的防误报
  规则。
- **表格列预览**：非标银行流水、多表头对账单在写映射脚本前，先看清真实表头与前 20 行。

不需要工作台、不需要 PostgreSQL、不装任何数据库驱动。

## 安装

```bash
# 核心：解析 TXT/DOCX/XLSX/PDF 与原生文字层 PDF 足够用，不装 OCR 运行时
pip install ./packages/plainfolio-parse

# 需要识别扫描件与图片时再装可选组
pip install './packages/plainfolio-parse[ocr]'
```

`requires-python = ">=3.13,<3.14"`。核心依赖只有 `pypdfium2`、`python-docx`、`openpyxl`、
`pillow`、`pydantic`、`mcp(<2)`。

## 命令行

```bash
plainfolio-parse policy                          # 当前围栏 + OCR 可用性（只读）
plainfolio-parse parse-file --path 合同.pdf      # stdout 一行 JSON（逐页单元）
plainfolio-parse parse-file --path 合同.pdf --format md          # 直接输出 Obsidian Markdown
plainfolio-parse parse-file --path 合同.pdf --format md --output 合同.md
plainfolio-parse parse-folder --dir 案卷 --format md --output-dir 笔记  # 逐份导出 Markdown
plainfolio-parse scan-values --path 案卷/         # 分币种金额汇总 + 日期分布
plainfolio-parse scan-bridges --path 案卷/        # 跨文件强指纹碰撞
plainfolio-parse preview-table --path 对账单.csv --header-row 5
```

stdout 契约：**只有一行 JSON**；唯一例外是 `parse-file --format md` 且未指定 `--output`
时直接输出 Markdown 正文。退出码 `0` 成功 / `2` 业务错误（stderr 为结构化 JSON）/ `1`
未预期错误。

## 目录围栏与原件只读

- 默认只允许读取**当前工作目录**及其子目录。
- 需要处理其他位置时，显式列出白名单（`os.pathsep` 分隔，Windows 为 `;`）：

  ```bash
  export PLAINFOLIO_ALLOWED_ROOTS="/Users/me/案卷:/Volumes/证据盘"
  ```

- 路径先词法归一（消解 `..`）再比对白名单；从根到目标的任意一级符号链接或 NTFS
  Junction 一律拒绝。
- **原件只读**：输入只以只读方式打开；`--output` / `--output-dir` 写出的路径不得与任何
  输入文件相同，否则直接报错，不做"覆盖前备份"。

## OCR（可选）

```bash
export PLAINFOLIO_OCR_MODEL_DIR="/path/to/models"   # 其下 rapidocr/ 需含 manifest.json 与 det/rec/cls.onnx
```

也接受工作台的 `LWB_OCR_MODEL_DIR`。缺依赖或缺模型时**不会**让普通 PDF/文本解析崩溃：
纯文本、DOCX、XLSX 与带原生文字层的 PDF 照常返回 `complete`；需要 OCR 的页面或图片
逐页标为 `failed` 并写明 `ocr_unavailable: …`，整份文档仍可交付。`policy` 会说明缺的是
依赖还是模型。

## 作为 MCP 服务接入 Agent

```bash
plainfolio-parse-mcp            # stdio；stdout 只走 MCP 协议帧
```

6 个工具：`parse_policy`、`parse_file`、`parse_folder`、`scan_exact_values`、
`scan_identifier_bridges`、`preview_table`。零代码插件形态见仓库
`plugins/plainfolio-parse/`（`plugin.json` + `mcp.json` + 便携启动器 + 办案 Skill）。

## Python 调用

```python
from pathlib import Path
from plainfolio_parse import ParseConfig, parse_file, extract_exact_values, scan_identifier_bridges

config = ParseConfig(ocr_model_dir=Path("/path/to/models"))  # 不传则读环境变量
result = parse_file(Path("合同.pdf"), config)
for unit in result.units:
    print(unit.locator.get("page"), unit.method, unit.status, len(unit.text))

bridges = scan_identifier_bridges([Path("a.txt"), Path("b.docx")], config=config)
```

## 红线

涉密、涉个人隐私或当事人禁止上云的案卷，**严禁**接入云端模型，只用本机离线模型；
本包不发起任何网络请求。
