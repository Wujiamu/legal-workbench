---
name: plainfolio-parse
description: 素章离线阅卷解析器（独立版）的办案规程：先确认文件访问围栏，再逐份或整目录解析本地材料并导出带页码／工作表锚点的 Obsidian Markdown，然后扫描分币种精确金额、日期与跨文件强指纹碰撞（暗线排查），非标表格在写映射前先做表头预览。零数据库、免装工作台，原件只读，涉密案卷禁接云端模型。
---

# 离线阅卷解析规程（SOP · 零数据库独立版）

输入是一份材料或一个材料目录，输出是**可复核的结构化文本**：正文按页／工作表／
段落分单元，金额按币种分别汇总，跨文件重复出现的强指纹被单独列出。全程只读写本地
文件，不需要工作台、不需要数据库。

## 一、边界与红线

1. **涉密、涉个人隐私或当事人禁止上云的案卷，严禁接入云端模型。** 解析结果会随
   工具返回进入模型上下文；这类案卷只能在本机离线模型下处理。开工前先确认这份材料
   能不能进当前对话。
2. **原件只读，输出绝不覆盖输入。** 本工具从不改名、移动、删除材料；`--output` /
   `--output-dir` 与任何输入文件同路径时直接报错。不要用"先备份再覆盖"绕过它。
3. **解析结果不是事实认定。** OCR 会认错字，金额与编号必须回到原件页码复核；
   `status=partial`、`failed` 的单元要逐条向承办人说明，不得只报"已解析"。
4. **金额绝不跨币种相加。** 汇总只按同一 `currency` 进行；同一声明里出现 USD 与
   CNY 时分别列出，不得合并成一个数。
5. **不做法律判断。** 哪份材料能证明什么、某笔款项是否属于本案争议范围，由承办人决定。
6. **隐藏文件不解析。** 目录扫描会跳过 `.` 开头的文件与目录（`.DS_Store`、临时锁文件），
   这不是漏读；如果材料确实藏在隐藏目录里，请承办人先移出来。

## 二、四步执行流程

### 1. `parse_policy` — 先确认围栏与 OCR 状态，再动材料

先调用 `parse_policy`，确认三件事：允许的根目录（`allowed_roots`）与来源（`source`：
`cwd` 或环境变量 `PLAINFOLIO_ALLOWED_ROOTS`）、OCR 是否可用（`ocr.available` 与
`ocr.reason`）、支持哪些扩展名。

- 目标材料不在任何允许根目录下时，**停下来请承办人设置**
  `PLAINFOLIO_ALLOWED_ROOTS`（`os.pathsep` 分隔；Windows 用 `;`），再继续。
- 不要为了"能跑通"把根目录设成 `/`、`C:\` 或用户主目录；那等于取消围栏。
- `ocr.available=false` 时不要假装能读扫描件：先看 `ocr.reason` 是缺依赖
  （`pip install 'plainfolio-parse[ocr]'`）还是缺模型（`PLAINFOLIO_OCR_MODEL_DIR`）。

```bash
plainfolio-parse policy
# {"allowed_roots": ["…/甲案"], "source": "cwd", "ocr": {"available": false, "reason": "…"}}
```

### 2. `parse_file` / `parse_folder` — 解析并导出 Markdown

单份材料先看清结构再批量：

```bash
plainfolio-parse parse-file --path 案卷/02_核心合同/采购合同.pdf            # 一行 JSON，逐页单元
plainfolio-parse parse-file --path 案卷/02_核心合同/采购合同.pdf --format md --output 笔记/采购合同.md
plainfolio-parse parse-folder --dir 案卷 --format md --output-dir 笔记/解析
```

导出规则（必须遵守）：

- **保留锚点**：Markdown 的二级标题就是页码／工作表／段落定位（`## 第 3 页`、
  `## 工作表：往来明细 · 第 5 行`、`## 段落单元 2（第 8–14 行）`），不要改写成
  "第一部分"这类无定位标题；每个单元前的 `<!-- unit: pdf:3 -->` 注释锚也不要删。
- **frontmatter 原样保留**：`source` / `status` / `parser_version` / `unit_count` /
  `warnings` 是外部 Agent 判断可信度的唯一依据。
- 需要强制重认某几页（例如原件文字层是乱码）时用 `--force-ocr-pages 3 4`。
- 扫描件与图片单元若为 `failed` 且 `error` 以 `ocr_unavailable:` 开头，**明确告知
  承办人这一页没有被读到**，不要把它当作"该页无内容"。
- 一次目录解析超过 20 份材料时，先在 `parse_folder` 结果里看 `files[].status`，
  只对 `partial` / `failed` 的做单份深挖，避免把整个语料塞进对话。

### 3. `scan_exact_values` — 精确金额与日期（分币种）

```bash
plainfolio-parse scan-values --path 案卷/03_财务流水与对账/ 案卷/01_诉讼文书/
```

- `amount_totals` 按币种分开给出 `total` 与 `count`；**引用时必须带上币种与原文**。
- `date_counts` 是 ISO 日期到出现次数的映射；同一事实的两个日期（签约日／到期日）
  不要合并。
- 裸数字不算金额：只有带 `USD`/`CNY`/`人民币`/`元`/`万元` 等币种标记的数字才会被
  提取——所以"合同金额 8500000"若原文没写币种，需要人工补证，不要自行假定为 CNY。
- 表格里的「金额｜币种」按行配对提取（例如台账的 `D3: 3,500,000.00` 与 `E3: CNY`），
  金额本身用 `Decimal` 处理，不经过浮点。

### 4. `scan_identifier_bridges` — 跨文件强指纹碰撞（暗线排查）

```bash
plainfolio-parse scan-bridges --path 案卷/
```

- 只报告**出现在两份以上文件**的同一指纹：手机号（`cn_mobile`）、带 GB 32100 校验
  码的统一社会信用代码（`uscc`）、银行账号（`bank_account`，须有"银行/账号/开户行"
  等邻域关键字）、邮箱（`email`）。
- 每条 bridge 带 `files`、`file_count` 与逐条 `occurrences`（文件、单元键、页码/工作表、
  摘录）；**向承办人呈现时必须落到具体页码**，不要只给一个号码。
- 两个防误报规则不要绕开：18 位身份证号（校验位通过或有"身份证"字样）不算 USCC；
  没有银行上下文的 15–19 位数字（如物流单号）不算账号。若承办人怀疑某号码应当成
  账号，请人工核对原文，不要改规则。
- 跨文件同一指纹只是**线索**，不是关联关系认定；结论由承办人作出。

### 5. `preview_table` — 非标表格先看表头

银行流水、多表头对账单、合并单元格台账在写任何映射或算账脚本之前，先看真实表头：

```bash
plainfolio-parse preview-table --path 案卷/03_财务流水与对账/对账单.csv --header-row 5
```

- `headers` 里出现 `列2`、`列3` 表示该列表头为空（合并单元格或空表头），必须按数据
  行内容人工确认含义后再映射。
- `sample_rows` 默认 20 行；`total_rows` 与样例不符时（例如 25 条流水只看到 20 行）
  说明还有数据，别用样例当全集。
- CSV 会自动识别 UTF-8 / UTF-8-SIG / GB18030 与 `,` / TAB / `;` 分隔符；`sheets` 为
  `["CSV"]` 表示这是纯文本表，不要去找工作表名。

## 三、交付口径

每一次解析交付都必须包含：

1. 受检文件清单与各自的 `status`（`complete` / `partial` / `failed`）；
2. 全部 `warnings` 与失败单元的 `error` 原文，逐条说明影响；
3. 金额只报分币种结果，日期报 ISO 值，指纹碰撞报页码级定位；
4. 明确写出未解析到的部分（OCR 不可用页、损坏件、跳过的不支持格式），不用"基本完成"
   这类措辞掩盖缺口。
