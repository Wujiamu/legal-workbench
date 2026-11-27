# 素章 · 法院自动装卷器（独立 Agent 插件）

零数据库、免装工作台：把本地材料目录装成中国法院可直接打印、也可网上立案的
证据卷宗 ZIP。

- **MCP 服务**：4 个工具（`dossier_policy` / `dossier_init_manifest` /
  `dossier_validate` / `dossier_build`），stdio 传输，stdout 只走协议帧。
- **办案 Skill**：`skills/plainfolio-dossier/SKILL.md`，四步流程 + 红线。
- **零本机路径**：插件里没有绝对路径，入口由 `scripts/run-mcp.py` 现找。

## 安装（三种机器状态都能用）

### A. 已 `pip install plainfolio-dossier`

```bash
python3 -m pip install /path/to/packages/plainfolio-dossier
```

启动器会先在 `PATH` 里找 `plainfolio-dossier-mcp`，找到就直接用它。

### B. 就是这个仓库的检出

工作台仓库根目录执行过 `cd backend && uv sync` 后，`backend/.venv` 里已经有本包
（可编辑引用）。启动器会自动用它，并补上
`PYTHONPATH=<仓库>/packages/plainfolio-dossier/src`。无需任何额外配置。

### C. 什么都没有

```bash
python3 -m venv ~/.venvs/plainfolio-dossier
~/.venvs/plainfolio-dossier/bin/pip install /path/to/packages/plainfolio-dossier
export PLAINFOLIO_DOSSIER_BIN=~/.venvs/plainfolio-dossier/bin/python
```

`PLAINFOLIO_DOSSIER_BIN` 也可以直接指向 `plainfolio-dossier-mcp` 可执行文件。

## 排障

```bash
python3 scripts/run-mcp.py --print-plan
# {"source": "仓库虚拟环境", "command": [".../backend/.venv/bin/python", "-m", "plainfolio_dossier.mcp_server"], "cwd": ".../packages/plainfolio-dossier", "pythonpath": ".../packages/plainfolio-dossier/src"}
```

`--print-plan` 只报告将要执行的命令，不启动服务。连接失败时先看它：
`source` 是 `PATH` / `仓库虚拟环境` / `当前解释器` / 环境变量，就说明解释器找对了；
报错"未找到 plainfolio_dossier"说明这一台还没装。

## 文件访问围栏（必须理解）

默认只允许**当前工作目录及其子目录**。要处理其他位置的材料，由承办人显式放行：

```bash
export PLAINFOLIO_ALLOWED_ROOTS="$HOME/cases/甲案:$HOME/卷宗/乙案"   # Windows 用 ; 分隔
```

- 白名单目录必须是存在的真实目录，自身不含符号链接 / NTFS Junction；
- 从根目录到目标的任意一级符号链接或联接点都拒绝，`..` 穿越无法逃出白名单；
- **原件只读**：输出 ZIP 不得与任何输入材料（含清单本身）同路径；
- 先 `dossier_policy` 确认围栏，再开始扫描材料。

## 客户端接入

通用 MCP 客户端配置（Claude Desktop / Antigravity / DSH / Codex 等）：

```json
{
  "mcpServers": {
    "plainfolio-dossier": {
      "command": "python3",
      "args": ["/绝对路径/plugins/plainfolio-dossier/scripts/run-mcp.py"]
    }
  }
}
```

插件市场已在本仓库 `.agents/plugins/marketplace.json` 登记为
`plainfolio-dossier@0.1.0`（`plainfolio` 仍为第 0 项）。

## 隐私红线

**涉密、涉个人隐私或当事人禁止上云的案卷，严禁接入云端模型。** MCP 工具返回的材料
名称、目录结构与装卷结果都会进入模型上下文；这类案卷只在本机离线模型下处理，或用
CLI（`plainfolio-dossier build ...`）在纯本机脚本里完成。

## 输出内容

| 文件 | 用途 |
|---|---|
| `00_证据合订卷宗.pdf` | A4、左侧 20mm 装订线、封面＋精确页码目录＋每项分隔页＋正文连续页码 |
| `01_证据目录.docx` | 可编辑 Word 目录，页码与合订本对齐 |
| `01_证据目录.csv` | 结构化目录（UTF-8 BOM，防公式注入） |
| `02_分项证据材料/` | 按「证据组-序_名称」命名的单份材料，网上立案逐条上传 |
| `00_装卷说明.txt` | 装卷范围与提示清单 |
