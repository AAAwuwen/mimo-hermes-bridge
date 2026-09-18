# VERIFICATION.md · 监督台账

> 规则见 `AGENTS.md` §8：**交付方自报 ≠ 通过**。每条声称都要对应一条可复跑命令，
> 由验收方（Hermes）实跑并记录。被推翻的原声称也留在这里，不许抹掉。

状态取值：`待验` / `通过` / `打回` / `无法验证`

---

## 第 1 轮（2026-09-18，构建者 = MiMoCode，验收 = Hermes）

| # | 声称 | 验证命令 | 实跑结果 | 结论 |
|---|---|---|---|---|
| V1 | `check` 在两边就绪时 PASS 且退出码 0 | `python bridge.py check` | PASS；`mcp handshake protocol=2024-11-05 server=hermes tools=10`；exit 0 | 通过 |
| V2 | 配置缺失/损坏时 FAIL 且退出码非 0 | `python bridge.py check --config <4 种坏配置>` | 4/4 FAIL，exit 1；报错精确到行列（`not valid JSONC: ... line 1 column 40`） | 通过 |
| V3 | `setup` 幂等（已经能用就别动） | sha256 前后比对 + 连跑两次 | `cc2fc596…ffd77` 前后**字节级一致**；0 个备份文件 | 通过 |
| V4 | 测试全绿 | `python tests/test_bridge.py` | `Ran 67 tests` / `OK` | 通过 |
| V5 | ruff 零告警 | `uvx ruff check .` | `All checks passed!` | 通过（但有 C1） |
| V6 | README 命令照抄能跑 | `doctor` / `render` | 均通过，`render` 正确展开片段 | 通过 |
| V7 | 交付物无作者隐私 | 全仓扫描（用户名/机器名/真实路径/内网 IP/密钥串） | 7 个交付文件 **0 命中** | 通过 |
| V8 | 未夸大适配范围 | 人工核对 README「验证范围」节 | 已验证 = MiMoCode ↔ Hermes；其他 MCP 客户端标为未验证 | 通过 |

## 争议与打回

| # | 议题 | 验收方证据 | 结果 |
|---|---|---|---|
| **C1** | `ruff.toml` 里 `per-file-ignores "bridge.py" = ["E501"]` 是否必要 | 验收方：`--isolated --select E501 --line-length 100` → All checks passed，豁免多余 | **通过（第 2 轮）**：构建者删除豁免，并在 ruff.toml 写明理由（7 处 print 在 89–99 列，未超契约 100）。复跑两条命令均 `All checks passed!` |
| C2 | `bridge.py` 1388 → 1845 行（+457）是否为正当增长 | 构建者用 AST 给出函数级行数对照：`_check_rows` 112→17、`mcp_probe` 90→28、`cmd_setup` 84→36、`cmd_doctor` 55→23、`cmd_restore` 44→31；消失的 `reader`/`add` 原为闭包，已提成模块级函数 | **通过**：属复杂度规则要求的拆解 + docstring 补齐（21→91），非灌水 |
| C3 | 残留文件是否入库 | `.gitignore` 已覆盖 `*.snap0` / `.ruff_cache/` / `__pycache__/` / `*.log` / `payload-*.json` / `*.bak.*` | **通过**：文件不会入库。构建者的 `rm` 被其自身权限系统拒绝（`bash_delete` auto-reject），由验收方代为移出工作区 → `<BACKUP_DIR>`（本机备份目录，不入仓库） 已改为 `<BACKUP_DIR>` |

## 待验清单

| # | 项 | 命令 | 实跑结果 | 结论 |
|---|---|---|---|---|
| D | 真发一条消息到对端 | `python bridge.py ask --to mimo "ping…"` | 收到的回复：「我是 MiMoCode，一个交互式 CLI 编码代理，底层模型是 deepseek。」exit 0 | **通过** |
| V9 | `setup --restore` 能回滚 | `python bridge.py setup --restore` | 未跑 | **未验证**（需先在临时配置上造一次改动；动真配置有风险，留待下一轮） |
| V10 | 退出码契约 0/1/2/3 | 触发四种情形 | `0`（PASS）与 `1`（四种坏配置）已实测；`2`/`3` 未触发 | **部分验证** |
| V11 | check 输出含"桌面版不生效"提醒 | `python bridge.py check` | 尾部实有：`note: this only covers what is on disk and can be spawned. Config changes reach MiMoCode in a NEW process…` | **通过** |

---

## 验收方自己的记录（§8：监督包括监督者）

- 第 1 轮中，Hermes 曾把验收标准写成「**双向连通自检**」，被构建者指出**该断言不可验证**
  （外部只能观察端点，观察不到 MiMoCode 进程内部的客户端状态）。**原措辞已撤回**，记在 `DECISIONS.md` D6-1。
- Hermes 还曾提出在给 MiMoCode 的规则里写「不碰 R18 题材」——**被作者否决**（属自设限 + 主动暴露题材）。
  该提议作废，并写入作者知识库的「对外口径」节作为警示（路径不在此仓库内）。

- **2026-09-18 建本文件时，Hermes 自己把作者的真实用户名路径写进了本文件**（发布用文件）。
  被自己的全仓扫描抓到，当场改掉。教训：**监督者必须先扫自己再扫别人**；
  规则文档里举例也要用占位符形式（`%USERPROFILE%`），否则真实用户名/示例文本都会污染扫描结果。

## 超出要求的证据（记录在案）

- **行为等价性证明**：构建者用清理前快照 `bridge_old.py` 对**全部子命令 + 全部 `--help` + 降级路径**
  做了逐字节输出 diff 与退出码比对，结论 `IDENTICAL`（含 `ask` 的降级路径与 `render`）。
  这比"测试全绿"更强：它证明**清理没有改动任何可观察行为**——正是 AGENTS.md §6 要求的"不许为过 lint 改动行为"。
- 该证明第一次跑出 `render` DIFFERS，构建者诊断为**自己 harness 的路径解析问题**（快照副本没有自己的 `examples/`），
  修正后复跑 `IDENTICAL`。**把假阳性也交代出来**，这点记在案。
