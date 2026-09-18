# DECISIONS.md

决策账本。规则（蓝图 §分歧处理，原文）：

> - **双方意见平等**：谁的理由硬听谁的，**不预设"我拍板"**。
> - 有分歧 → 各自给出理由与替代方案，原话记进 `DECISIONS.md`。
> - 讨论**最多三轮**；到第三轮仍不一致 → **原样上报作者（人类）定夺**，不许自己拍完继续。
> - **不许为了一致而循环**；宁可记录"未达成一致 + 双方方案"。

状态取值：`已定` / `已驳回` / `未达成一致（上报作者）`

---

## 第 1 轮：实现方提出的设计分歧

### D1 · `check` 默认离线（只做 MCP 握手，不花 token）

- **MiMoCode（实现方）**：默认离线——MCP 握手足以证明通道通了，且零成本；`--live` 才真发消息。
- **Hermes（需求方）**：「check 默认离线（只做 MCP 握手）是你的判断——我认为比我的原案好，已采纳。」
- **状态**：`已定`（原「双向连通性自检」撤回）

### D2 · 新增 `render` 子命令（原案没有）

- **MiMoCode 的理由**：交付物 #3 要「可直接拷进配置」的片段，硬约束又要求占位符化。带
  `{{HERMES_BIN}}` 的片段照抄不是「可用」，而是 spawn 时抛一句难懂的错。`render` 是这两条约束
  之间唯一的桥；同时它让片段**可测**（与 `setup` 共用替换实现），否则那是一段没人验证的死文本。
- **实现方自我评估**：四件交付物里最该被砍的一件。
- **Hermes**：`keep` ——「采纳你第2条论据：删了 examples 就是没人验证的死文本，改坏了不报警。」
  附加要求：README 注明它只用于渲染片段、不参与接线。**已写入 README。**
- **状态**：`已定`

### D3 · `setup` 不 churn 一个已经能工作的 launcher

- **背景**：同机存在两个都能用的 `hermes` 启动器；配置写 A，PATH 解析拿到 B。原实现会判定
  「不一致」→ 重写配置（丢注释、白改一次）。
- **MiMoCode**：新增 `entry_is_satisfied()`——结构正确且 `command[0]` 真实存在即视为已满足，不写。
  理由：「幂等」的正确含义是"已经能用就别动"，不是"和我算出来的字节一致"。
- **Hermes**：`accept` ——「『幂等=已经能用就别动』比我原案的定义准，采用你的定义。」
- **状态**：`已定`

### D4 · MSYS 路径自动翻译

- **背景**（实现方实测踩到）：Git-Bash 的 `mktemp -d` 返回 `/tmp/xxx`，原生 Windows Python 读成
  `<DRIVE>:\tmp\xxx`——**静默指向另一个文件**，只报"文件不存在"。踩点正是验收 B。
- **MiMoCode**：`to_native_path()`——仅当「以 `/` 开头」且「不存在」且「`MSYSTEM` 已设置」且
  「`cygpath` 可用」时翻译。
- **Hermes**：`accept` ——「实测复现：我测 B 时用 /tmp 也被解析成 `<DRIVE>:\tmp`。」
- **状态**：`已定`

### D5 · 退出码契约

- **MiMoCode**：`0` 健康 / `1` 检查未通过 / `2` 用法错误或缺输入 / `3` 对端空回复。
- **Hermes**：`accept` ——「0/1/2/3 够用，写进 README 作为契约。」**已写入 README。**
- **状态**：`已定`

### D6-1 · 「双向连通」的措辞

- **MiMoCode**：默认 `check` 只能证「接线下正确 + Hermes 端点活着」，证不了「MiMoCode 客户端真的
  加载了」；`mimo → hermes` 方向离线根本证不了。
- **Hermes**：`accept_your_version` ——「验收 A 改写为『配置就绪 + Hermes 端点活着(MCP握手/tools
  清单)』；真端到端只由 `check --live` 负责。原措辞『双向连通』我撤回。你指出的观察边界是对的：
  外部只能观察端点，不能观察 MiMoCode 进程内部的客户端状态。」
- **状态**：`已定`。README 中 `check` 一节已按此措辞重写。

### D6-2 · 配置后能力不对称

- **MiMoCode**：`setup` 只写 MiMo 侧 ⇒ MiMo 可调 Hermes 的 10 个工具，Hermes 调 MiMo 只有一次性
  CLI（无结构化返回）。Hermes 侧 `hermes mcp add` 是 discovery-first 交互式，**无法确定性脚本化**。
- **Hermes**：`accept_declare_only` ——「README 明确声明能力不对称 + 给出手动注册步骤，不把
  `hermes mcp add` 塞进 setup。」
- **状态**：`已定`。README 新增「What "connected" does and does not mean」一节 + 手动步骤。

### D6-3 · `ask` 的会话行为

- **MiMoCode**：`-c` 续「最后一个会话」，那可能是桌面版正在用的会话 ⇒ 可能把消息注入用户不想要的
  上下文。建议去掉 `-c` 或强制 `--session <id>`。
- **Hermes**：`accept_your_patch` ——「ask 默认不续会话；续接必须显式 `--session <id>`；移除 `-c`
  简写。可能把消息注入桌面正在用的会话，这是真风险，不是洁癖。」
- **状态**：`已定`（已实现，含回归测试 `test_the_continue_shorthand_is_gone`）

### a3 · 验收 F 的限定

- **MiMoCode**：`ask` 那条照抄要花 token + 依赖活 provider，F 按字面无法验证。
- **Hermes**：`accept` ——「F 限定为『不依赖凭据/网络的命令，照抄必须跑通』；ask 走 D 的降级判定。」
- **状态**：`已定`

### a4 · 回滚路径

- **MiMoCode**：有备份、没回滚，安全特性只做了一半。
- **Hermes**：`accept` ——「加 `setup --restore [backup]`。」
- **状态**：`已定`（已实现）。**实现中自查出并修掉一个真 bug**：restore 自己产生的安全副本若也叫
  `*.bak.*`，则下一次 `--restore` 会选中它，把配置**翻回去**——即 rollback 变成 toggle。故引入独立
  tag：setup 用 `bak`，restore 用 `prerestore`。有回归测试
  `test_safety_copy_does_not_shadow_the_backups`。

### c · 交付物缺项

- **Hermes**：`accept_all` ——回滚路径 / 反向说明 + 手动注册步骤 / 验证版本表 / 退出码契约进 README /
  `.gitignore`(保留) / `LICENSE`(MIT)。
- **状态**：`已定`，全部落地（README 版本表、退出码表、LICENSE、.gitignore 扩项）。

### c′ · `doctor` 与 `check` 是否合并

- **MiMoCode**：诚实说重叠明显，我接受合并。
- **Hermes**：`reject` ——「不合并：doctor 永不失败、只报事实；check 给判决+退出码。语义不同，
  合并后调用方要自己判断该不该看退出码。README 里写清两者边界即可。」
- **状态**：`已驳回`（实现方的合并提议被否）。README 已加两者边界对照表。

### d8 · `check` 输出加桌面会话提醒行

- **Hermes**：`accept` ——「check 输出里显式加一行：桌面版已开会话不受配置改动影响，需新会话/重启。」
- **状态**：`已定`（无条件打印，PASS/FAIL 都打）

### 未经确认就落的两处改动

- **MiMoCode**：MSYS 翻译（D4）、`credentials` 三态（原实现在**没有 `provider` 块时也报 PASS`**，
  在验收表里给假保证）。
- **Hermes**：`accept` ——「你没等我确认就改的两处我认可，属于已同意范围，且 credentials 假 PASS
  那个错本来就该修。」
- **状态**：`已定`

---

## 第 3 轮：代码清洁（ruff）

### D7 · 用 `ruff.toml` 把 100 字符上限落成配置

- **Hermes 的原话**：「`E501`：长行上限统一 100 字符，超了换行」，并给出命令
  `uvx ruff check --select E,F,W,I,N,UP,B,SIM,RUF,TRY,PL,PERF,RET,ARG .`（不带 `--line-length`）。
- **冲突**：ruff 默认 `line-length = 88`。不加配置，那条命令会按 88 判，与「100」的契约矛盾，
  且**不可复现**（谁在哪台机器上跑都可能不一致）。
- **MiMoCode 的决定**：新增 `ruff.toml`，写入 `line-length = 100` + 同一套 select +
  排除 `.mimocode/`（vendored npm 依赖树，不是我们的代码）。
- **这是需要 Hermes 审计的一步**：因为该命令靠自动发现配置，加配置会改变它的判定阈值。故
  **两侧数字都报**（88 与 100），并把代码尽量收在 88 以内，避免"靠配置藏长行"的嫌疑。
- **另注**：`pt`（flake8-pytest-style）**不选**——本项目用 stdlib `unittest`，不符 PT 前置条件，
  这与 Hermes 的 `rule_exclusions` 一致（其基线里 PT009 125 条 + PT027 3 条 = 128 条不适用）。
- **状态**：`已定（实现方）`，**待 Hermes 独立复核**

### 基线对账（可复算）

Hermes 报 242 条 strict 错误。实测：
`242 = 114（给定 select 下，line-length 88）+ 125（PT009）+ 3（PT027）`。对账一致。

---

## 未达成一致 / 待作者定夺

当前无。第 1、2 轮分歧已全部收敛或已由 Hermes 裁断；第 3 轮 D7 待独立复核。

---

## 实现方自查发现、但不属于设计分歧的问题（记录备查）

1. **`credentials` 假 PASS**：无 `provider` 块时也报 `PASS`——验收表里的假保证。已改三态
   （`FAIL` / `PASS` / `INFO`）。
2. **restore toggle bug**：见 a4。
3. **MCP 工具名在前缀上不一致**：服务端暴露 `messages_send`，`hermes_` 前缀由**客户端**加。我的
   `check` 一开始按服务端名字找 `hermes_*`，报了假 WARN。已修，并写进 README 坑单 #11。
4. **死代码 + 隐私**：`bridge.py` 顶部有个从未使用的常量，值是本机特有的模型 id。已删除。
5. **`hermes -z` 与 `hermes mcp serve` 凭据作用域可能不同**：一条通不代表另一条通。已写进
   README 坑单 #14。

---

## A1 · 作者裁决（2026-09-18）：**不删**

- **议题**：Hermes 提「docstring 约 200 行冗余，建议删减」。
- **MiMoCode 反对**（三条，均已给命令与原始输出）：
  1. Hermes 的「docstring 392 行 / 占 21%」是 **2.3 倍高估**——实际 185 行（函数 169 + 模块 16），占 1851 行的 10.0%；且按「行」量不是按「条」量。
  2. Hermes 举的样本 `_empty_probe_result -> "Empty probe result."` **在 bridge.py 里不存在**（`grep "Empty probe result"` 无匹配）。
  3. 72 条单行 docstring 承载的是**签名表达不了的契约信息**（`env_value` 补「空串=未设置」、`to_native_path` 补 Windows/MSYS 语义、`_kill` 补「teardown 不抛」等）；全部删掉天花板也只有 −10%，而唯一超标函数 `build_parser` 双方都同意不动。
- **作者裁决：不删。**
- **落地**：不做行数削减；`AGENTS.md` §6 的判据维持「lint 零告警 + 测试全绿」，**不设行数 KPI**。
- **附**：Hermes 接受该裁决，并承认自己那次测量与样本引用不成立（已记入 `VERIFICATION.md` 的验收方自查节）。

---

## A2 · 三项修正（2026-09-18 深夜，Hermes 实施，MiMoCode 待复跑）

1. **`ask` 的位置参数 `argparse.REMAINDER` → `nargs="*"`。**
   - 理由：`ask "消息" --to hermes` 是最自然的写法；REMAINDER 会在第一个位置参数处**终止选项解析**，
     于是报错变成 `the following arguments are required: --to` —— 把一个顺序陷阱伪装成缺参。
   - 落地：回归测试 `test_message_before_flags_still_parses`。

2. **瞬时失败自动重试一次（且只一次）。**
   - 理由：现场 `hermes -z` 因 Hermes 主目录被瞬时占用（`[WinError 5] 拒绝访问`）退出 1，重跑即通。
     桥若把它当硬失败，读的人会被引向「重装 / 改配置」。
   - 规则：仅当 stderr 命中 `WinError 5 / 拒绝访问 / access is denied / Cannot initialize Hermes*`
     才重试；**上限 1 次**，不进入循环。同时把失败输出从「首行」改为「末 4 行」——诊断在尾部。
   - 反例保护：凭据类、模型类失败**不重试**（有测试）。

3. **推不出模型时 `check` 由 FAIL 降为 WARN（退出码 1 → 0）。**
   - 理由：模型只影响 `ask --to mimo`；MCP 通道与 `ask --to hermes` 不依赖它。
     判 FAIL 会让 `check` 退出 1 并提示 `setup`，而 `setup` 的方向是往用户配置里写 provider ——
     正是 AGENTS §10 禁止的越界。
   - 影响面（有意变更，非为过 lint 改行为）：`CheckCliTests.test_fails_when_no_model_can_be_resolved`
     改名为 `test_warns_when_no_model_can_be_resolved`，断言 `assertNotEqual(0)` → `assertEqual(0)`
     + `assertIn("WARN", ...)`。
