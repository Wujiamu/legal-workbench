# 素章 · PlainFolio 通用 Agent 插件包

把素章（PlainFolio）本地法律工作台接进 Codex、DSH（DeepSeek Harness）、Claude Desktop / Antigravity 等支持 MCP 与 Agent Skills 的客户端。插件包只包含下面这些文件：

- `plugin.json` —— 便携 Agent 插件清单（`$schema` 指向 Agent Plugins 1.0.0，同时保留 Codex 兼容字段 `skills` / `mcpServers`）。
- `mcp.json` / `.mcp.json` —— 同一个 stdio MCP 服务 `plainfolio`；前者供便携格式自动发现，后者供 Codex 兼容清单引用，两者内容一致（由测试守护不漂移）。
- `skills/` —— 三个面向用户的业务 Skill：`plainfolio-workbench`（办案操作）、`chart-substantive-review`（对外图表实质复核）、`nonstandard-table-script-sop`（非标表格外部脚本 SOP）。
- `scripts/run-mcp.py` —— 纯标准库的便携启动器，负责找到工作台、可选预启动数据库，然后把进程交给真正的 MCP 服务。

内部端到端验收规程（仓库 `.agents/skills/` 下的验收 SOP）**不随插件分发**：插件只装面向承办人的业务 Skill。

## 安全模型（先读这一段，再谈接入）

**默认锁死（`deny_all`）＋单案授权。** 工作台的 MCP 层默认拒绝一切案件访问：策略文件 `var/runtime/agent-access.json` 不存在时按 `deny_all` 处理，Agent 既不能列案、检索、读原文，也不能建案。放行只能由承办人在本机终端逐个案件打开：

```sh
scripts/lwb agent-status                  # 当前模式、已授权案件、风险提示
scripts/lwb agent-grant <case_id>         # 临时放行单个案件（切到 allowlist）
scripts/lwb agent-revoke <case_id>        # 收回该案；白名单清空即自动回到 deny_all
scripts/lwb agent-lock                    # 立即全局锁死并清空白名单
scripts/lwb agent-allow-all --confirm     # 对所有案件开放（风险，仅限本地离线模型）
```

（Windows 把 `scripts/lwb` 换成 `scripts\lwb.cmd`。桌面安装包没有 `scripts/` 目录，改用 `<可执行文件> --run-cli agent-grant <case_id>`。）

门禁规则：

- `deny_all`：只有 `list_cases(limit=0)` 可作连通探活（返回空列表），其余工具一律返回 `forbidden`，错误信息里带可照抄的 `agent-grant` 命令。`create_case` 在 `deny_all` 与普通 `allowlist` 下都拒绝。
- `allowlist`：只放行白名单内的 `case_id`。`list_cases` 只返回已授权且确实存在的案件，`total` 也只等于已授权案件数——**不会**暴露库里还有多少其他涉密案件。
- `allow_all`：恢复原来的全库行为，仅供本机离线模型，必须显式 `--confirm`。
- **MCP 侧没有任何授权、解锁或改写策略的工具**：只有只读自检 `get_access_policy`（返回模式、已授权案件数、是否允许建案与来源，不含案件标题）。授权永远在人这一侧。
- 策略文件原子写入、权限 `0600`，只含 `mode` / `allowed_case_ids` / `allow_create_case` / `updated_at`，**不含任何凭据或案件正文**。文件损坏或无法解析时安全降级为 `deny_all`。
- 环境变量 `LWB_MCP_ALLOWED_CASES`（逗号分隔 UUID）与 `LWB_MCP_ACCESS_MODE` 用于受控的单案启动与测试；非法 UUID 显式报错而不是忽略。它们只能放宽「没有策略文件」的默认，**不能**撤销文件里的锁：文件锁死优先于任何环境变量。

每个业务 Skill 的最前面都有同一段「授权前置与硬围栏纪律」：未获用户明确授权前不得连接、探测或列举案件；拿到 `forbidden` 必须停下来请承办人授权，不得改走数据库、`.env`、策略文件或其他客户端绕过。

## 前置要求

1. **先装好并初始化素章本体**：按工作台根目录 `README.md` 安装后端依赖（`backend/.venv`）、初始化数据库并跑通一次 `scripts/lwb start`（Windows 为 `scripts\lwb.cmd start`）。插件包不包含工作台，也不代替它初始化。
2. **登记一次实例指针**（每台机器一次，换目录后重做）：

   ```sh
   scripts/lwb register-instance          # macOS / Linux
   scripts\lwb.cmd register-instance      # Windows
   ```

   它把工作台路径与时间戳写入 `~/.plainfolio/instance.json`（`home`、`project_root`、`updated_at`），**不写数据库口令或任何凭据**。`scripts/run-mcp.py` 与工作台本体（`config.resolve_home_dir()`）都按 `LWB_HOME` → 该指针 → 自身仓库 的顺序找目录，所以同一份插件包可以跟着仓库走，不必改任何路径。
3. 客户端能找到 `python3`（≥ 3.8）。启动器只用标准库；真正的 MCP 服务由工作台自己的虚拟环境（`backend/.venv`）运行。

## 路径规范

清单里不写死任何本机路径：

- `mcp.json`（便携格式）用 `"${PLUGIN_ROOT}/scripts/run-mcp.py"`。`${PLUGIN_ROOT}` 是 Agent Plugins 规范定义的插件根占位符。
- `.mcp.json`（Codex 兼容）用同样的占位符。若你的客户端版本不展开 `${PLUGIN_ROOT}`，把该参数换成插件目录下 `scripts/run-mcp.py` 的绝对路径即可；也可以让 `cwd` 指向插件根、参数只写 `scripts/run-mcp.py`。
- 工作台目录**永远不写进清单**，由 `scripts/run-mcp.py` 在运行时按上面的顺序解析。

## 接入示例

### Codex

```sh
# 添加本仓库的插件市场（仓库内的 .agents/plugins/marketplace.json）
codex plugin marketplace add .            # 或用绝对路径 / owner/repo
codex plugin marketplace add owner/repo --ref main
# 安装插件（ID 为 <plugin>@<marketplace>）
codex plugin add plainfolio@plainfolio-local
```

本地目录也可直接添加：`codex plugin marketplace add <本仓库目录>`。Codex 读到 `$schema` 时按便携包处理，自动发现 `skills/` 与根级 `mcp.json`；旧版本走 `plugin.json` 的 `skills` / `mcpServers` 字段。`codex plugin validate` 可能提示 `component_declaration_ignored`（便携包忽略根级组件字段），属预期行为，不影响加载。

### DSH（DeepSeek Harness）

**不再提供常驻的 DSH Cordis 插件。** 早期方案（`plugins/dsh-plainfolio`，一个包同时挂 MCP 桥、注册业务 Skill、加设置页面板）已废弃并从仓库移除：常驻宿主插件会把「工作台在线、有哪些能力」变成一个长期存在的服务面，与「高保密案件按需授权、用完即锁」的隔离原则冲突。

在 DSH 里用工作台，走**按需的通用 MCP 通道**，并严格配合上面的默认锁死＋单案授权门禁：

```yaml
- insert:
    - id: mcp-plainfolio
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: plainfolio
        command: python3
        args: ["<插件目录>/scripts/run-mcp.py"]
        env:
          LWB_MCP_AUTO_START_DB: '1'
```

（字段以你安装的版本 `dsh web --dump-config` 输出为准。）这种接法只桥接 MCP 工具，不注册业务 Skill、也没有设置页面板。

在 DSH 里使用前必须同时满足三条：

1. 承办人在本机执行 `scripts/lwb agent-grant <case_id>` 只放行**本次要用的那一个案件**；
2. 该案卷允许接触当前所连接的模型——**涉密或当事人禁止上云的案卷，只能接纯本地离线模型**，不得经 DSH 接到任何云端模型；
3. 会话结束后执行 `scripts/lwb agent-revoke <case_id>` 或 `scripts/lwb agent-lock` 收回权限，不要让它长期开着。

业务 Skill 需要时，把 `skills/` 里的对应目录按 DSH 自身的 Skill 安装方式装进去即可，不依赖任何专用插件包。

### Claude Desktop

`claude_desktop_config.json`（macOS 在 `~/Library/Application Support/Claude/`，Windows 在 `%APPDATA%\Claude\`）：

```json
{
  "mcpServers": {
    "plainfolio": {
      "command": "python3",
      "args": ["<插件目录>/scripts/run-mcp.py"],
      "env": { "LWB_MCP_AUTO_START_DB": "1" }
    }
  }
}
```

### Antigravity / 其他 MCP 客户端

任何支持 stdio MCP 的客户端都可以手填同样的三项：`command = python3`、`args = [<插件目录>/scripts/run-mcp.py]`、`env` 按需加 `LWB_HOME` 或 `LWB_MCP_AUTO_START_DB=1`。不想用启动器时，也可以直接指向工作台的模块入口：

```json
{
  "command": "<工作台目录>/backend/.venv/bin/python",
  "args": ["-m", "legal_workbench.mcp_server"],
  "env": { "PYTHONPATH": "<工作台目录>/backend/src" }
}
```

（Windows 把解释器换成 `<工作台目录>\backend\.venv\Scripts\python.exe`。）

## 环境变量

| 变量 | 作用 |
|---|---|
| `LWB_HOME` | 直接指定工作台目录，优先级最高；设置后忽略实例指针 |
| `LWB_INSTANCE_CONFIG` | 覆盖实例指针文件路径（默认 `~/.plainfolio/instance.json`），便于多实例与测试 |
| `LWB_MCP_AUTO_START_DB` | `1`/`true`/`yes`/`on` 时，启动 MCP 前先跑 `scripts/lwb start-db`，输出全部重定向到 stderr |
| `LWB_AGENT_ACCESS_FILE` | 覆盖访问策略文件路径（默认 `<工作台目录>/var/runtime/agent-access.json`）；主要用于测试与多实例隔离 |
| `LWB_MCP_ALLOWED_CASES` | 逗号分隔的 `case_id` 列表，把本次启动限定为这些案件（受控单案启动）。含非法 UUID 时显式报错，不会静默忽略 |
| `LWB_MCP_ACCESS_MODE` | `deny_all` / `allowlist` / `allow_all`。与 `LWB_MCP_ALLOWED_CASES` 同时存在时取更严格的一个；两者都**不能**撤销策略文件里的锁 |

其余 `LWB_*`（`DATABASE_URL`、`DATA_ROOT`、`IMPORT_ROOTS` 等）由工作台的 `.env` 提供，插件包不读取也不转存。

## 隐私边界（必读）

- 素章本身**完全离线**：案卷、索引、数据库都只在本机，不联网、无账号、无遥测。
- **接入云端大模型后，工具返回的案卷片段会进入该模型的上下文**：检索命中的上下文、原文片段、主体与金额、分析正文都会随会话上传到你接入的那个服务。涉密或当事人禁止上云的案卷，请只接本地模型，或不用 Agent、直接在网页操作。
- 实例指针只含路径与时间戳；`.env`、连接串与口令不要写进插件配置、笔记或对话。

## 人工把关红线

Agent 的写入（进度、关系图、证据目录、诉请金额）一律进入**待确认收件箱**，确认、撤回、复核、冻结、归档只能由承办人在网页完成；插件不提供、也不应绕过这些入口。原件只读、不可删除，历史引用永远可读。Agent 的输出是待核对的草稿与线索，终审归承办人。

## 排错

| 现象 | 处理 |
|---|---|
| 启动即退出，stderr 提示找不到工作台 | 按提示执行 `scripts/lwb register-instance`，或设 `LWB_HOME` |
| stderr 提示没有可用解释器 | 工作台没装后端依赖：`backend/.venv` 缺失，按 README 安装；或 `pip install -e backend` 生成 `lwb-mcp` 入口 |
| 工具返回 `forbidden`，提示 `agent-grant` | 这是默认锁死，不是故障。由承办人在本机 `scripts/lwb agent-grant <case_id>` 放行该案；`scripts/lwb agent-status` 可看当前模式 |
| 工具调用报数据库不可用 | 启动桌面应用或 `scripts/lwb start`；只想起库用 `scripts/lwb start-db`，或设 `LWB_MCP_AUTO_START_DB=1` |
| stdout 出现非协议文本 | 启动器保证 stdout 只留 MCP 帧；若出现，说明客户端把 `command` 指向了别的脚本，检查 `args` 是否为本包的 `scripts/run-mcp.py` |
| 想看启动器解析到什么 | `python3 scripts/run-mcp.py --print-plan` 打印 home/source/command/cwd（不含环境变量内容） |
