---
name: plainfolio-dossier
description: 素章法院自动装卷器（独立版）的办案规程：先确认文件访问围栏，再扫描材料目录生成清单草案并请承办人逐条确认，只读预检页码与缺件，最后装卷出包（合订 PDF＋Word/CSV 目录＋网上立案分项材料）。零数据库、免装工作台，原件只读，输出绝不覆盖输入材料。
---

# 法院自动装卷规程（SOP · 零数据库独立版）

输入是一个材料目录或一份清单，输出是一份可直接打印装订、也可网上立案逐条上传的
证据卷宗 ZIP。全程只读写本地文件，不需要工作台、不需要数据库。

## 一、边界与红线

1. **涉密、涉个人隐私或当事人禁止上云的案卷，严禁接入云端模型。** 材料内容会随
   MCP 工具返回进入模型上下文；这类案卷只能在本机离线模型下处理。开工前先确认
   这份材料能不能进当前对话。
2. **原件只读，输出绝不覆盖输入。** 本工具从不改名、移动、删除材料；输出 ZIP 与
   任何输入材料（含清单本身）同路径时会直接报错。不要用"先备份再覆盖"绕过它。
3. **清单草案不是结论。** `dossier_init_manifest` 只按文件名与目录机械分组，
   证据名称、证明目的、页码范围、分组顺序都必须由承办人逐条确认后才可装卷。
4. **不做法律判断。** 哪份材料能证明什么、某页是否属于本案争议范围，由承办人决定。
5. **交付必附复核提示。** 把 `warnings`（页码越界、文件损坏、分项超限）原样转述给
   承办人，不得只报"已生成"。

## 二、四步执行流程

### 1. `dossier_policy` — 先确认围栏，再动材料

先调用 `dossier_policy`，确认允许的根目录（`allowed_roots`）与来源（`source`：
`cwd` 或环境变量 `PLAINFOLIO_ALLOWED_ROOTS`）。

- 目标材料不在任何允许根目录下时，**停下来请承办人设置**
  `PLAINFOLIO_ALLOWED_ROOTS`（`os.pathsep` 分隔；Windows 用 `;`），然后再继续。
- 不要为了"能跑通"把根目录设成 `/`、`C:\` 或用户主目录；那等于取消围栏。

```bash
plainfolio-dossier policy
# {"allowed_roots": ["$HOME/cases/甲案"], "source": "cwd", ...}
```

### 2. `dossier_init_manifest` — 生成草案，交承办人确认

```bash
plainfolio-dossier init-manifest --source-dir ./材料 --case-title "某设备买卖纠纷案" \
    --case-number "(2026)沪0105民初1号" --plaintiff "甲公司" --defendant "乙公司" \
    --output ./清单.json
```

只读递归扫描（跳过隐藏文件），按子目录分组、按文件名稳定排序。把返回的
`group_count` / `item_count` / `binding_count` 与草案内容一并给承办人，请其确认：

- 每条证据的名称与**证明目的**；
- 一份 PDF 只取其中若干页时，补 `page_start` / `page_end`；
- 分组顺序是否符合起诉状的举证逻辑；
- 取得方式（`acquisition`，如"原件""复印件""扫描件"）是否需要标注。

确认方式：直接编辑 `清单.json`（或换成同样列的 CSV：
`group,title,purpose,file_path,page_start,page_end,acquisition`）。

### 3. `dossier_validate` — 只读预检，先把问题清干净

```bash
plainfolio-dossier validate --manifest ./清单.json
```

读 `valid` 与 `issues`：

| `code` | 含义 | 处理 |
|---|---|---|
| `file_not_found` | 清单里的材料不存在 | 让承办人重新指路径，别自己猜 |
| `file_forbidden` | 路径越权或经由符号链接 | 换到允许根目录下，或由承办人放行 |
| `page_range_inverted` | 页码倒挂 | 回问承办人正确页码 |
| `page_range_incomplete` | 只填了结束页 | 补齐起始页 |
| `page_range_exceeds_pdf` | 页码超出原件页数（warning） | 告知承办人，装卷会截断或出提示页 |
| `empty_group` / `unbound_item` | 空分组 / 未绑定材料（warning） | 确认是否为有意留空 |

`valid = false` 时**不要**继续装卷；先把 `error` 级问题解决。

### 4. `dossier_build` — 装卷出包

```bash
plainfolio-dossier build --manifest ./清单.json --output ./证据卷宗.zip
```

返回 `output_path` / `filename` / `item_count` / `total_body_pages` / `total_pages` /
`warnings` / `size_bytes`。交付时向承办人说明：

- ZIP 内四件套：`00_证据合订卷宗.pdf`（A4、左侧 20mm 装订线、封面＋精确页码目录＋
  每项分隔页＋正文右下角连续页码、右上角留白供法院归档编码）、`01_证据目录.docx`
  （可编辑，可补案号与落款）、`01_证据目录.csv`、`02_分项证据材料/`（逐条上传用）；
- 正文页码与目录页码一一对应，**页码变了就要重新装卷**；
- `warnings` 中"分项超过 50/100 MB"的条目，提示承办人按分册拆分后再上传。

## 三、MCP 工具对照（客户端里直接调用时）

| 工具 | 参数 | 说明 |
|---|---|---|
| `dossier_policy` | — | 报告围栏根目录与来源 |
| `dossier_init_manifest` | `source_dir`, `output_path?`, `case_title?`, `case_number?`, `plaintiff?`, `defendant?` | 生成草案（不装卷） |
| `dossier_validate` | `manifest_path` | 只读预检，返回结构化 `issues` |
| `dossier_build` | `manifest_path`, `output_path` | 装卷并原子写出 ZIP |

## 四、失败时的做法

- 工具报 `DossierError`（`code` + `message` + `details`）：如实转述给承办人，
  **不要**自行改清单里的路径、页码去"试到能跑通"。
- 报"路径不在允许的根目录内"：请承办人放行目录，或把材料复制进允许目录后重跑；
  不要改 `PLAINFOLIO_ALLOWED_ROOTS` 扩大范围。
- 报"输出路径不得覆盖输入源文件"：换一个输出文件名；不要移动或改名原件。
