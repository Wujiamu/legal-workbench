状态：已废弃（路线调整：常驻宿主插件与高保密案件隔离原则冲突，改用路线 2 默认锁死＋按案授权方案）
基线：v0.1.0 + 0009_case_metadata（承接：20261124-通用Agent插件包.md；后继：20261125-Agent访问硬围栏与授权收紧.md）
最近更新：2026-11-25

# DSH 专用 Cordis 双面插件（已废弃）

> **本单已废弃。** `plugins/dsh-plainfolio/` 已从仓库整体删除，不随本仓库发布。
> 废弃理由：常驻型宿主插件会把「工作台在线、有哪些能力」变成一个长期存在的服务面，
> 与「高保密案件按需授权、用完即锁」的隔离原则冲突。DSH 改为按需使用通用
> `@deepseek-ai/dsh-mcp-client` stdio 配置，并受 [Agent 访问硬围栏与授权收紧](20261125-Agent访问硬围栏与授权收紧.md)
> 的默认锁死＋单案授权门禁约束。下文保留调研与决策留痕，供后续需要重做宿主插件时参考。

## 原使用场景

通用插件包（`plugins/plainfolio`）解决了「把 MCP 服务与业务 Skill 分发给任意 Agent 客户端」，但在 DSH 里要手工拼 `cordis.yml`：MCP 服务、Skill 桥接、状态展示是三处独立配置，装完在 DSH 界面里看不到工作台是否连上，也看不到隐私边界与人工把关提示。原设想是让承办人在 DSH 设置页一眼看到：工作台在不在线、连的是哪个实例目录、Agent 到底拿到了哪些能力，以及「案卷片段会进云端模型」与「确认权在人」这两条红线。

## 关键调研结论（仍可复用）

本机实测 `@deepseek-ai/dsh@0.2.1-rc.2`，三处与原先设想不同，对任何未来的 DSH 宿主侧扩展都成立：

1. **客户端 bundle 不是 ESM，而是工厂信封**。`dsh-client-*/lib/client.js` 全部是 `window.__ModuleLoader__.load({ id, factory: (require) => { … return module.exports } })`；`dsh-client-modules/lib/client.js` 记载「执行 bundle 只注册工厂，模块体副作用（含 CSS 注入）在 materialization 时运行」。因此构建必须 `esbuild --format=cjs` 后再包信封，不能直接发 ESM。
2. **静态模块表的 cordis 名是 `@deepseek-ai/cordis`**，不是 `cordis`。`dsh-web-frontend` 的 `staticModules()` 精确为 `react`、`react/jsx-runtime`、`react-dom`、`react-dom/client`、`@deepseek-ai/cordis`、`zod`、`@tanstack/react-virtual`、`@deepseek-ai/dsh-client-store`、`@deepseek-ai/dsh-client-ui-slots`、`@deepseek-ai/dsh-client-ui-primitives`、`@deepseek-ai/dsh-client-ui-dockkit`。
3. **`settings.section` 由 `dsh-client-ui-settings-general` 在运行时声明**（作为 `sidebar.settings` 的 children），`dsh-client-ui-settings` 只持有类型契约；注册形态是 `ctx.slots.inject(name, () => ctx.slots.register({name,id,order,label,locale?}, Comp))`，owner props 为 `{ close }`，`id` 必填。

另外确认：`--patch` 是启动器级 flag（不在 `dsh web --help` 里）；`dsh plugin --profile web add <pkg>` 转发给 pnpm；patch 语义为 `- insert:` 追加、`- id:` 按 id 覆盖整段 config；`dsh-mcp-client` 的 `StdioConfig` 必填 `serverName`/`command`/`args`/`env`/`cwd`/`toolCallTimeoutMs`/`failOnStartupError`；`dsh-subprocess.scrubbedParentEnv()` 会清掉敏感名与 `DSH_*` 变量。

## 原实现要点（代码已删除，仅留结论）

- 用 `ctx.webServer.register()` 的 `GET /plainfolio/status` 而非 Typert Remote：该版本客户端可见的 Remote namespace 需要一对生成式 descriptor，而 `@deepseek-ai/dsh-typert-protocol` 不在客户端静态模块表内，第三方 bundle 无法仅靠配置完成 Remote 贡献。
- 环境变量只透传路径（`LWB_HOME`、`LWB_INSTANCE_CONFIG`、`LWB_MCP_AUTO_START_DB`），插件从不打开 `.env`。
- 未知配置键必须硬失败：`schemastery` 会透传未知键，需在 schema 的 Standard Schema 面（`Config['~standard'].validate()`）加守卫，`apply()` 入口另设一道。

## 原验证记录（2026-11-25，代码已移除，不构成当前证据）

删除前最后一次实测：`npm run typecheck` exit 0；`npm run build` 产出 `index.js 10697B / client.js 21529B`；`npm test` **67 passed**；反向验证四项均能检出；隔离 `DSH_HOME` 下 `dsh web --patch … --dump-config` exit 0。**未做**真实 DSH GUI 会话验收。

## 废弃处置

- `plugins/dsh-plainfolio/` 整目录删除（全部为本轮新建的源码、构建产物、依赖与测试，`git ls-files` 为空，无任何用户业务数据）。
- `.gitignore` 清理 `plugins/dsh-plainfolio/lib/` 条目；保留 `plugins/**/node_modules/` 与 `packaging/` 规则。
- `plugins/plainfolio/README.md` 与 `docs/AGENT.md` 的 DSH 段落改写为「不再提供常驻 Cordis 插件 + 按需通用 MCP + 门禁三条件」。
- 相关实机验收条目已从 [NEXT_STEPS](../NEXT_STEPS.md) 移除，替换为路线 2 的实机验收条目。

## 若将来重启

需要先回答：常驻插件暴露的「工作台在线/能力清单」是否本身构成对未授权案件的侧信道？如果是，宿主插件只能在 `allowlist` 生效期间才注册路由与工具，而不是常驻注册。这一条必须先有结论再动手。
