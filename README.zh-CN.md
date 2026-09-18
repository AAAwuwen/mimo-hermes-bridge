# mimo-hermes-bridge · 让本机两个 AI agent 互相说话

> 中文说明。English: [README.md](README.md)

**同一台机器上跑着两个 AI agent —— 一个 OpenCode 系（小米 [MiMoCode](https://mimo.xiaomi.com)），
一个 [Hermes Agent](https://hermes.nousresearch.com)。它们都支持 MCP、都有一次性 CLI，
但默认互相不知道对方存在。**

本仓库就是一个**只用标准库**的 Python 文件：把两者接起来，并且**证明接通了** ——
省得后来人把同一批坑重踩一遍。

```
                    同一台机器
   +-------------------------------+      +------------------------------+
   |  MiMoCode（OpenCode 分支）      |      |  Hermes Agent                |
   |                               |      |                              |
   |  mimo run "..." -m 厂/模型      |----->|  hermes -z "..."             |  通道 A
   |  （一次性 CLI，可续会话）        |      |  （一次性 CLI）               |  出站
   |                               |      |                              |
   |  mcp.hermes = [hermes.exe,    |<-----|  hermes mcp serve            |  通道 B
   |                "mcp","serve"] | stdio|  （stdio 上的 MCP 服务端）     |  入站
   |    → 拿到 10 个 hermes_* 工具   |      |                              |
   +-------------------------------+      +------------------------------+
```

**为什么要两条通道**：它们的失效方式不同。

- **MCP 通道（B）**给 MiMoCode **结构化工具**（读会话、发消息、轮询事件），但只在**加载了配置的
  MiMoCode 进程里**有效。
- **CLI 通道（A）**是一次性、无状态的，但能在**脚本、cron、CI** 里用 —— 那些地方没有 MCP 宿主。

## 验证范围（看这一节再决定要不要用）

**端到端验证过的**：MiMoCode（OpenCode 分支）↔ Hermes Agent，平台 Windows 11，**两条通道都跑通**，
用的就是本 README 里的命令。本仓库就是在这套组合上开发与测试的，`README` 与 `DECISIONS.md` 里的数字
都来自那台机器。

**机制相同、但我们没验证的**：任何 MCP 客户端都能挂 `hermes mcp serve` ——
Codex 有 `codex mcp add <名字> -- <stdio 命令>`（配置在 `~/.codex/config.toml`，也支持项目级
`.codex/config.toml`），Claude Code 有 `claude mcp add`。
**MCP 那一半（通道 B）**应当可以照搬；**`ask --to mimo` 那一半（通道 A）**是 MiMoCode/OpenCode 专属，
换成别的 CLI 需要写适配器。这两条**我们都没在这台机器上跑过** —— 请当作「未验证」，不要当作「已支持」。

我们宁可少宣称。写着「到处都能用」却在你环境里挂掉的仓库，浪费你一下午；
把测过什么、没测什么写清楚的仓库，能给你省下这一下午。

## 通道 A 的前提（先看这条）

`ask --to mimo` 驱动的是**你**的 MiMoCode CLI。本仓库**不带** provider、不带模型清单、不带凭据，
也**从不往你的配置里写这些**。

如果你的 MiMoCode 没有登录、也没有自己配第三方 API，CLI 自己会回一句类似
`MiMo free API service has ended. Sign in or configure a third-party API.` —— 通道 A 就是不通。
**这件事归你决定，不归本仓库**：要么登录，要么把 MiMoCode 指向你自己选的 provider。
本项目刻意不碰这一层。

通道 B（`hermes mcp serve` 那条）没有这个依赖：它只运行你机器上已有的程序。

## 环境要求

- Python **3.10+**（推荐 3.11+）—— **永不引入第三方依赖**。
- MiMoCode 已安装（`mimo` 可用）。
- Hermes Agent 已安装（`hermes` 可用，且带 `hermes mcp serve`）。
- 主目标平台 Windows（Git-Bash/MSYS **与** PowerShell 都要能跑）；macOS/Linux 同样可用。

## 快速开始

```bash
python bridge.py doctor          # 只读：东西都在哪
python bridge.py setup           # 合并 MCP 配置项（先备份）
python bridge.py check           # 双向健康检查；退出码 0 = 健康
python bridge.py ask --to hermes "我今天有什么待办？"
python bridge.py ask --to mimo   "用三句话总结这个仓库"
```

`setup` 会打印备份路径，并且**可反复运行**：若 `mcp.hermes` 已经和它将要写入的等价，**一个字节都不写**。

## 命令

### `setup` —— 先探测，再合并（幂等）

```bash
python bridge.py setup [--config PATH] [--hermes-bin PATH] [--dry-run] [--restore [BACKUP]]
```

- 定位 MiMoCode 配置（`mimocode.jsonc`）与 `hermes` 可执行文件。
- 合并下面这一段（`mcp` 对象不存在则创建）：

  ```jsonc
  "mcp": {
    "hermes": {
      "type": "local",
      "command": ["<hermes 的绝对路径>", "mcp", "serve"],
      "enabled": true
    }
  }
  ```

- **改前先备份**并打印备份路径（`mimocode.jsonc.bak.YYYYmmdd_HHMMSS`）。
- **幂等**：值相同 ⇒ 零写入、零备份。判据是「**已经能用就别动**」，不是「和我算出的字节一致」。
- **原地文本编辑**，你的注释与排版都会保留。唯一例外：已存在的 `mcp.hermes` 必须**被替换**时，
  配置会被重新序列化为干净 JSON，工具会明确告诉你注释丢了。
- `--dry-run` 只展示计划，不动任何文件。
- `--restore [备份文件]` 回滚（不给参数则用最近一次备份）。

### `check` —— 双向健康检查

```bash
python bridge.py check [--live] [--no-mcp-probe] [--mcp-timeout 30]
```

输出 PASS/FAIL/WARN 表格，**有 FAIL 就返回非零退出码**（0 = 健康）。检查项：

| 检查 | 含义 |
|---|---|
| `hermes bin` / `mimo bin` | 可执行文件找得到 |
| `mimocode.jsonc` + `jsonc parse` | 配置存在且是合法 JSONC |
| `mcp.hermes` | 条目存在、`enabled: true`、有 `command` 数组 |
| `mcp handshake` | **真做一次 MCP stdio 握手** —— 启动 `hermes mcp serve`，发 `initialize` + `tools/list`，数 `hermes_*` 工具 |
| `model` / `credentials` | 能推导出 `厂/模型`；该厂的 `apiKey` 能解析（**值永不打印**） |

`--live` 才会真的两边各发一条消息（花 token、需要凭据）。**默认刻意离线**：
一次 MCP 握手就足以证明接线正确，且**一分钱不花**。

> 注意：`check` PASS **不等于**桌面版已经生效。默认 `check` 能证的是「接线正确 + Hermes 端点活着」，
> 证不了「MiMoCode 的桌面进程已经加载了 MCP」。真端到端只能靠 `--live`。

### `ask` —— 给对方发一条消息

```bash
python bridge.py ask --to hermes "消息"          # 执行：hermes -z "消息"
python bridge.py ask --to mimo   "消息"          # 执行：mimo run "消息" -m <模型>
```

参数：`--model 厂/模型`、`--dir DIR`（`mimo run` 的工作目录）、`--session <id>`（**显式**续接某个会话）、
`--timeout 秒`（默认 180）。

模型解析顺序：`--model` → `$MIMO_BRIDGE_MODEL` → 从你的 MiMoCode 配置里**第一个声明了 models 的 provider**自动探测。
解析不出来、或二进制/凭据缺失时，你会得到**一句说明该修什么的人话**，永远不是堆栈。
输出会剥掉 ANSI 转义码（`mimo run` 即使被管道接走也吐颜色）。

> **默认不续会话。** 要续必须显式给 `--session <id>` —— 因为「续最后一个会话」可能把消息
> 注入你桌面版正在用的那个会话里。

### `doctor` —— 只读诊断

```bash
python bridge.py doctor [--hermes-home PATH]
```

报告解析出的路径、各配置/凭据文件是否存在、声明了哪些 provider、`bridge.py` 会读哪些环境变量、
两侧 CLI 版本。**什么都不写。** 环境变量的值只报 `set`/`unset`，不报内容。

> `doctor` 与 `check` 不合并：**doctor 永不失败、只报事实；check 给判决 + 退出码。** 用途不同。

### `render` —— 展开示例片段

```bash
python bridge.py render --set HERMES_BIN=/path/to/hermes.exe
```

把 `examples/mimocode.jsonc.fragment` 渲染成 JSON 输出到 stdout。适合手动粘贴，或往 dotfiles 里做模板。
它让片段**可被测试**——否则那只是一段没人验证的死文本。

## 配置

一切都在运行时解析：**命令行参数 → 环境变量 → 平台默认值**。没有任何硬编码，仓库里也没有任何密钥。

| 变量 | 含义 | 默认 |
|---|---|---|
| `MIMO_CONFIG` | `mimocode.jsonc` 路径 | `$HOME/.config/mimocode/mimocode.jsonc` |
| `MIMO_BIN` | `mimo` 启动器路径 | 先 `PATH`，再若干已知安装目录 |
| `HERMES_BIN` | `hermes` 启动器路径 | 先 `PATH`，再若干已知安装目录 |
| `HERMES_HOME` | Hermes 数据目录 | `%LOCALAPPDATA%\Hermes` |
| `MIMO_BRIDGE_MODEL` | `ask --to mimo` 的默认 `厂/模型` | 自动探测 |
| `MIMO_BRIDGE_TIMEOUT` | 子进程超时（秒） | `180` |

每一项都有对应的命令行参数（`--config`、`--mimo-bin`、`--hermes-bin`、`--hermes-home`、`--model`、`--timeout`）。

## 坑（都是踩出来的）

1. **桌面会话 ≠ CLI 会话。** `mimo run` 永远开或续**它自己的**会话，从不碰桌面版打开着的那个会话。
   而且配置改动**只被新进程读到** —— 桌面版必须开新会话（或重启）才会看到 `hermes` 的 MCP 工具。
   桌面窗口里看不到 `hermes_*` 而 `check` 照样 PASS，**这是预期行为**。
2. **provider 白名单。** MiMoCode 开箱只认 `opencode/` 与 `mimo/` 两个前缀。其它（`zai`、`deepseek`、
   `openrouter`、私有网关）都需要**自定义 provider 块**。
3. **自定义 provider 的写法。** 用 `"npm": "@ai-sdk/openai-compatible"` 加
   `options.baseURL` / `options.apiKey` / `options.headers`，再把模型声明在 `models` 下。
   `ask` 用的模型串是 `<你的provider键>/<模型键>`，不是 `<npm包名>/<模型>`。
4. **`zen/go` 端点强制要 `x-opencode-session` 头。** 少了会报 `Request is missing x-opencode-session`。
   填进 `provider.<键>.options.headers`。`ask --to mimo` 认得这条报错并会告诉你。
5. **`mimo` 常常不在你以为的地方。** npm 经常把它装进 **Hermes 自带的那份 Node** 目录里，
   而那个目录**不在 MiMoCode 自己的 `PATH` 上**。所以 `setup` 往 MCP 的 `command` 数组里写的是**绝对路径**，
   `bridge.py` 也带了若干回退位置。探测失败就设 `MIMO_BIN` / `HERMES_BIN`。
6. **子进程不经过 shell。** MCP 的 `command[0]` 是**直接 spawn** 的 —— 不查 `PATH`、不展开 `~`、不处理引号。
   **只能写绝对路径。**
7. **桌面版会审计每一条输入**（`/api/audit/check`），并且默认对 `bash` 与 `external_directory`
   **自动放行权限**。目录边界得你自己守：`--dir` 要写明确，别把秘密粘进提示词。
8. **ANSI 颜色会活过管道。** `mimo run` 即使 stdout 不是终端也吐转义码，朴素地抓日志会变成
   `[0m[0m> build`。`bridge.py` 会剥掉；你自己的脚本大概也该剥。
9. **旧代码页。** 中文 Windows 的控制台代码页不是 UTF-8；`bridge.py` 会把自己的 stdout/stderr
   重配为 UTF-8，免得非 ASCII 路径把它弄崩（或在被抓取的输出里变成乱码）。
10. **Hermes 主目录可能瞬时不可读。** 现场案例：`hermes -z` 退出 1，报
   `Cannot initialize Hermes directory <HOME>: [WinError 5] 拒绝访问`，而**紧接着**重跑同一条命令就成功，
   磁盘上什么都没变 —— 目录被瞬时占用（另一个 Hermes 进程在收紧 ACL、杀软正在扫）。`ask` 现在把这类失败
   判为**瞬时**并自动**重试一次**，且打印 stderr 的**末尾几行**而不是只打第一行，免得一次抖动看起来像装坏了。
   真正的权限问题仍会报错，并给出下一步。
11. **`ask` 的消息放在标志前或后都行。** `bridge.py ask "消息" --to hermes` 以前会报
   `the following arguments are required: --to`（消息位置参数用了 `argparse.REMAINDER`，而 REMAINDER
   会终止选项解析）。现在两种顺序都可以。
12. **推不出模型时 `check` 只 WARN，退出码仍是 0。** 模型只挡 `ask --to mimo`；MCP 通道与
   `ask --to hermes` 都不需要它。这里刻意用告警而非失败：判 FAIL 会把读者推向 `setup` —— 也就是让本仓库
   往你的配置里写 provider，而它不会这么做（见「通道 A 的前提」）。

## 安全说明

- `bridge.py` **永不打印凭据值**。`doctor`/`check` 只报某个键**是否解析成功**，环境变量只报 `set`/`unset`。
- 你的 MiMoCode 配置里躺着**活的 `apiKey`**，那是秘密。本仓库的 `.gitignore` 覆盖 `*.bak.*`、`*.env` 之类，
  但**配置文件本身要留在任何仓库之外**。
- 含有密钥的配置的**备份同样是秘密**：`mimocode.jsonc.bak.*` 里有同样的明文 `apiKey`，调试完请删掉。
- 一切以你的用户身份运行。MCP 是本地 stdio 管道；**不监听任何端口**，
  除了你本来就配好的模型厂商之外，没有流量离开这台机器。

## 测试

```bash
python tests/test_bridge.py            # 标准库 unittest，不需要 pytest
python tests/test_bridge.py -v         # 详细输出
```

**测试套件从不碰你的真实配置**：每个用例都在临时目录里跑，行为重要的地方以子进程方式驱动 `bridge.py`。
覆盖：JSONC 解析（注释、尾逗号、字符串里含 `//`）、幂等合并、占位符替换，以及 `check`/`ask` 的降级路径。

## 本项目由两个 AI 协作完成

本仓库本身就是一个**双 agent 协作规程**的产物：一个 agent 出需求与验收，另一个实现。
**规则写在仓库里，不写在聊天记录里** —— `AGENTS.md` 会被进入本目录的 agent 自动加载。

| 规则 | 位于 |
|---|---|
| 通信一律**结构化 JSON**（`from/to/round/kind/...`），不用自由文本 | `AGENTS.md` §1 |
| 一个议题**最多三轮**；仍不一致 → **上报人类**，不许自己拍板后继续 | `AGENTS.md` §2 |
| **必须互相质问** —— 另一方必须先攻击这个断言，再决定接受；驳回要带理由，接受要写明采纳了哪条论据；鼓励自我批评 | `AGENTS.md` §3 |
| **不留任何个人数据**：只用占位符，发布前扫描用户名/机器名/真实路径/内网 IP/疑似密钥 | `AGENTS.md` §4 |
| **验收 = 复核方用同样的命令再跑一遍**，自报成功不算 | `AGENTS.md` §5 |
| **代码干净可验证**：`ruff check` + `ruff format --check` + 测试全绿 —— 不许 `noqa` 掩盖，不许为过 linter 改动行为 | `AGENTS.md` §6 |

`DECISIONS.md` 是账本：每处分歧、**双方原话**、谁因哪条论据胜出。它也记录**被推翻的原需求** ——
看 `D6-1`（「双向连通」的说法被撤回）与 `H1`。

## 目录结构

```
bridge.py                            整个工具
examples/mimocode.jsonc.fragment     可直接粘贴的 MCP 片段（{{HERMES_BIN}} 占位符）
tests/test_bridge.py                 标准库 unittest 套件
AGENTS.md                            协作规程（进入本目录自动加载）
DECISIONS.md                         决策账本（含双方原话与裁决）
PROJECT_BLUEPRINT.md                 需求与验收标准（工作文档）
```

## 许可

MIT
