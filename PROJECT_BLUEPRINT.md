# mimo-hermes-bridge · 项目蓝图 v1

> 内部工作文件（发布前处理掉：里面的本机路径不进仓库）。

## 一句话目标

让同一台机器上的两个 AI agent —— **OpenCode 系（以小米 MiMoCode 为代表）** 与 **Hermes Agent** ——
能互相发消息、互相派活，并且**一条命令完成配置与自检**。

## 为什么要做

现在两个 agent 各自有 CLI / 本地 API / MCP 能力，但互不相识。要让它们对话，得手工摸清：
配置写哪、凭据怎么给、会话隔离怎么算、路径怎么找。这些坑已经踩过一轮（见文末"参考事实"）。
把这个过程固化成工具 + 文档，别人（以及未来的我们）不用再摸一遍。

## 交付物

1. **`bridge.py`** —— 单文件 Python（**stdlib only**，3.10+）。子命令：
   - `setup` 检测两边环境 → 生成/合并配置（幂等，改前备份，打印备份路径）
   - `check` 双向连通性自检 → 输出 PASS/FAIL 表格 + 退出码（0 通、非 0 不通）
   - `ask --to mimo|hermes "消息"` 给对端发消息并打印回复
   - `doctor` 只读诊断：路径、版本、凭据存在性、配置片段是否就位（不写任何文件）
2. **`README.md`** —— 原理图（ASCII）、快速开始、坑清单、安全提示
3. **`examples/mimocode.jsonc.fragment`** —— 可直接拷进 MiMoCode 配置的 MCP 片段（占位符化）
4. **`tests/`** —— 至少覆盖：配置幂等合并、占位符替换、`check` 在缺件时的降级行为

## 硬约束

- **零第三方依赖**（只用 stdlib）。
- **不写死绝对路径 / 密钥**：一律从配置 + 环境变量取；找不到就报出**可操作的**错误（告诉用户该设哪个变量）。
- **改任何配置文件前必须备份**，并把备份路径打印出来。
- **幂等**：重复运行不产生重复写入。
- **Windows 优先**（Git-Bash/MSYS 与 PowerShell 都要能跑），路径处理用 `pathlib`。
- **隐私**：仓库里不留任何真实用户名、机器名、密钥——示例一律 `<USER>`、`$HOME`、`%LOCALAPPDATA%` 占位。

## 验收标准（Hermes 逐条实跑，不通过就打回）

| # | 检查 | 期望 |
|---|---|---|
| A | `python bridge.py check`（两边就绪） | PASS 表格，退出码 0 |
| B | 任一侧配置缺失/被破坏 | FAIL + 明确提示缺什么、怎么补；退出码非 0 |
| C | `python bridge.py setup` 连续跑两次 | 第二次零变更（幂等） |
| D | `python bridge.py ask --to mimo "ping"` | 拿到回复；缺凭据时**清晰说明缺什么**而不是崩栈 |
| E | 测试全绿 | `python tests/test_bridge.py`（无 pytest 也能跑） |
| F | README 里每条命令 | 照抄能跑通 |

## 分工

- **Hermes（我）**：需求、架构、验收、README 的「为什么这么设计」、最终拍板。
- **MiMoCode（你）**：实现、测试、README 的「怎么用」、跑通、把踩到的坑写进文档。

## 分歧处理（硬规矩）

- **双方意见平等**：谁的理由硬听谁的，**不预设"我拍板"**。
- **必须互相质问**：提方案的一方给可验证依据，另一方**先挑战再表态**；驳回要带理由，接受要写明采纳的是哪一条论据。鼓励自我批评。
- 有分歧 → 各自给出理由与替代方案，原话记进 `DECISIONS.md`。
- 讨论**最多三轮**；到第三轮仍不一致 → **原样上报作者（人类）定夺**，不许自己拍完继续。
- **不许为了一致而循环**；宁可记录"未达成一致 + 双方方案"。
- 通信格式一律 JSON；不得含作者隐私（占位符化）。
- 以上条款的权威副本在 `AGENTS.md`（进入本目录自动加载）。

## 参考事实（2026-09-18 实测，别重新发明）

- MiMoCode = **OpenCode 的 fork**。
  - 配置：`%USERPROFILE%\.config\mimocode\mimocode.jsonc`
  - 数据/会话库：`%USERPROFILE%\.local\share\mimocode\mimocode.db`（SQLite）
  - 技能：`.config\mimocode\skills\<id>\SKILL.md`（与 Hermes 同格式）
- 通道 A（→ MiMo）：`mimo run "<消息>" --model <provider>/<model> --dir <目录>`（一次性；`-c` 续会话）
- 通道 B（→ Hermes）：MiMoCode 配置里 `"mcp": {"hermes": {"type":"local","command":[<hermes.exe>, "mcp","serve"],"enabled":true}}`
  → MiMo 侧得到 10 个 `hermes_*` 工具（conversations / messages / channels / events / permissions）
- Hermes 侧另有 `hermes -z "<消息>"`（一次性提问）可作无 MCP 的兜底通道。

### 已踩的坑（必须写进 README）

1. **桌面会话 ≠ CLI 会话**：`mimo run` 每次新开会话，不碰桌面版正在用的会话；配置改动**只对新进程生效**，桌面版要新开会话/重启才加载 MCP。
2. **provider 白名单**：MiMoCode 只认 `opencode/` 与 `mimo/` 前缀，别家（zai / deepseek / openrouter）要**自定义 provider**。
3. **自定义 provider 写法**：`npm: @ai-sdk/openai-compatible` + `options.baseURL/apiKey/headers` + `models`。
4. **zen/go 端点要求 `x-opencode-session` header**，否则报 `Request is missing x-opencode-session`。
5. **CLI 位置陷阱**：`mimo` 常被 npm 装进 Hermes 的 node 目录（不在 MiMo 自己的 PATH）→ 调用要用完整路径。
6. 桌面版每次输入都过内容审查（`/api/audit/check`），且默认 `perm-autopass: bash / external_directory` → 敏感内容与目录边界要自己管。
