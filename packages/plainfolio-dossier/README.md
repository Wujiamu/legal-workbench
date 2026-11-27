# plainfolio-dossier · 素章法院自动装卷器（零数据库独立包）

把一堆案卷材料按中国法院装订习惯装成一份可直接打印、也可网上立案的卷宗包。
**不需要安装素章工作台，不需要数据库，不需要后端进程**——一个目录、一份清单、
一条命令。

产物（ZIP）：

| 文件 | 用途 |
|---|---|
| `00_证据合订卷宗.pdf` | A4 幅面、左侧 20mm 装订线、封面 + 精确页码目录 + 每项分隔页 + 正文右下角连续页码，右上角严格留白供法院归档编码 |
| `01_证据目录.docx` | 可编辑 Word 表格目录，页码与合订本完全对齐 |
| `01_证据目录.csv` | 结构化目录（UTF-8 BOM，Excel 直接打开；公式注入已中和） |
| `02_分项证据材料/` | 按「证据组-序_名称」命名的单份材料，用于「人民法院在线服务」逐条上传 |
| `00_装卷说明.txt` | 装卷范围、另附原件与格式提示 |

## 安装

```bash
python3 -m venv .venv-dossier
.venv-dossier/bin/python -m pip install /path/to/packages/plainfolio-dossier
# 或在工作台仓库内：cd backend && uv sync   （工作台已以可编辑方式引用本包）
```

需要 Python 3.13。依赖只有排版与文档渲染：`pypdf`、`pypdfium2`、`reportlab`、
`python-docx`、`pillow`、`pydantic`、`mcp`。

## 文件访问围栏（方案 A）

默认**只允许当前工作目录及其子目录**。需要处理其他目录时显式放行：

```bash
export PLAINFOLIO_ALLOWED_ROOTS="/Users/me/cases/2026-甲案:/Users/me/卷宗"  # Windows 用 ; 分隔
```

规则：

1. 白名单目录必须是存在的真实目录，且自身不含符号链接 / NTFS Junction；
2. 从根目录到目标的任意一级符号链接或联接点都拒绝，`..` 穿越无法逃出白名单；
3. **原件只读**：输出 ZIP 不得与任何输入材料（含清单本身）同路径，同名即报错。

先确认围栏再动手：

```bash
plainfolio-dossier policy
# {"allowed_roots": ["/Users/me/cases/2026-甲案"], "source": "env", ...}
```

## 四步用法

```bash
# 1. 扫描材料目录，生成待律师人工确认的清单草案（只读，不改材料）
plainfolio-dossier init-manifest --source-dir ./甲案材料 \
    --case-title "某设备买卖纠纷案" --output ./清单.json

# 2. 承办人核对分组、证据名称、证明目的、文献页码范围（这一步不能省）

# 3. 只读预检：文件缺失/越权、页码倒挂或越界、空分组、未绑定条目
plainfolio-dossier validate --manifest ./清单.json

# 4. 装卷（原子写入：先写临时文件再改名，绝不覆盖输入材料）
plainfolio-dossier build --manifest ./清单.json --output ./证据卷宗.zip
```

`validate` 在 `valid=false` 时退出码为 2；`build` 成功为 0。
**stdout 永远只有一行 JSON**，日志与错误都写 stderr，方便 Agent 直接解析。

## 清单格式

JSON（与 `DossierSpec` 一一对应）：

```json
{
  "case": {"title": "某设备买卖纠纷案", "case_number": "(2026)沪0105民初1号",
           "plaintiff": "甲公司", "defendant": "乙公司"},
  "groups": [
    {"title": "合同主体与效力",
     "items": [
       {"title": "设备采购合同（节选）", "purpose": "证明买卖合同成立",
        "bindings": [{"file_path": "材料/合同.pdf", "material_name": "成套设备.pdf",
                      "page_start": 2, "page_end": 3}]}
     ]}
  ]
}
```

CSV（给习惯 Excel 的承办人）列固定为
`group,title,purpose,file_path,page_start,page_end,acquisition`；同一
`(group,title)` 的多行表示同一证据绑定多份材料。`file_path` 相对清单文件所在
目录解析，编码支持 UTF-8 / UTF-8-SIG / GB18030。

可选的 `sha256` / `size` 字段用于完整性校验：填了就核对，不符即拒绝装卷。

## MCP 接入（Agent）

```bash
plainfolio-dossier-mcp            # stdio 服务，stdout 只走 MCP 协议帧
```

4 个工具：

| 工具 | 作用 |
|---|---|
| `dossier_policy` | 报告当前围栏（根目录、来源、红线） |
| `dossier_init_manifest` | 扫描目录生成清单草案 |
| `dossier_validate` | 只读预检，返回结构化 `issues` |
| `dossier_build` | 装卷并写 ZIP |

对应的 Agent 插件包在本仓库 `plugins/plainfolio-dossier/`，其中的
`scripts/run-mcp.py` 会自动寻找已安装的解释器，无需手工写路径。

## 红线

- **涉密、涉个人隐私或当事人禁止上云的案卷，严禁接入云端模型**：材料内容会随
  MCP 工具返回进入模型上下文。请只用本机离线模型。
- **原件只读**：本工具从不修改、移动或删除输入材料；输出路径与任何输入相同即
  直接报错。
- 装卷结果必须由承办人复核（页码、证明目的、分项命名）后再对外提交。

## 测试

```bash
cd backend && .venv/bin/pytest ../packages/plainfolio-dossier/tests -q
```

（在工作台仓库内运行；独立安装时也可用 `pytest` 直接跑本目录的 `tests/`。）
