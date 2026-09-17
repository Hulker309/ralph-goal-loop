---
name: ralph-goal-loop
description: "Use when you have a prd.json with multiple user stories and want a single Hermes process to implement them in priority order with parallel fan-out within each priority level. Wraps Hermes /goal (judge + state persistence) as the execution engine and delegate_task batch mode for per-priority parallel worker fan-out. 0 external CLI, 100% Hermes in-process. Triggers on: 'run ralph', 'multi-story prd', 'goal + fan-out', 'prd.json 拆 story', 'per priority 跑'."
version: 0.1.0
author: Hermes Agent (希尔, 2026-09-17)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ralph, prd, goal, autonomous-agents, fan-out, delegate_task, multi-story]
    related_skills:
      - kanban-codex-lane
      - openclaw-plugin-author-suite
      - hermes-agent
---

# ralph-goal-loop

> **白话先**: 你有一个 `prd.json` 拆好的多 story 任务,想让它**在 Hermes 自己内部**跑完——按 priority 顺序、同 priority 内 fan-out 并行,直到全部 `passes: true` 才退。**不**调外部 Claude Code CLI,不走 bash 循环,直接用 Hermes 自己的 `/goal` 引擎(judge LLM + state 持久化)做"持续跑到目标完成",用 `delegate_task` batch mode 做"同 priority 内并行 fan-out"。**这是 mikeyobrien/ralph 在 Hermes 内的 1:1 复刻,底层引擎是 Hermes 官方 `/goal` 而非自造**。

## Overview

`ralph-goal-loop` 是 `mikeyobrien/ralph` 的 Hermes-side 移植。它复用 4 个上游原语(都在 `hermes_cli/goals.py`):

| 原语 | file:line | 用途 |
|---|---|---|
| `GoalContract` | `hermes_cli/goals.py:280` | 想法 → 可验收契约的 Python 数据类 |
| `draft_contract(objective)` | `hermes_cli/goals.py:1008` | auxiliary LLM 自动把一句话扩成完整 GoalContract |
| `judge_goal(goal, last_response, ...)` | `hermes_cli/goals.py:864` | 单独调 judge,返回 `(verdict, reason, parse_failed, wait_directive, transport_failed)` |
| `GoalManager(session_id)` | `hermes_cli/goals.py:1053` | 状态机(set / pause / resume / clear / evaluate_after_turn / next_continuation_prompt)+ `state_meta` SQLite 持久化 |

`ralph-goal-loop` 在这 4 个原语之上**做 1 件事**:把"prd.json 多 story 编排 + 内层并行 fan-out"包成 `hermes ralph` 1 个 CLI 命令。

**0 modify hermes-agent 核心**。**0 spawn 外部 CLI 进程**。**0 破 prompt caching 边界**。

## When to Use

Use `ralph-goal-loop` when:
- 你有一个 `prd.json` 拆好的多 story 任务(每个 story 有 `id / title / priority / acceptanceCriteria / passes`)
- story 之间有**priority 依赖**(priority 2 必须等 priority 1 全 passes=true)
- 同 priority 的 story 是**独立的**(可以并行跑,无文件冲突)
- 你想要**进程内 fan-out**——多 worker 在同 turn 跑,避免串行等 30 秒/轮
- 你想要**judge LLM 软判定**——主模型或 cheap model 看完 worker 报告判"继续/完成"

Do NOT use `ralph-goal-loop` when:
- 只有 1 个 story(或 1 个 goal 字符串)——直接用 Hermes `/goal`,不需要这层包装
- story 互不依赖,但你想要**跨机器并行**——用 Kanban + 多 profile,不是 ralph-goal-loop
- 你想"持续优化到目标完成"但不知道中间要做什么——用 `/goal draft`,让 auxiliary LLM 帮你拆
- 你想用外部 Claude Code CLI 跑——那是 mikeyobrien/ralph 原版,本 skill 故意不接

## Two-Layer Model(关键设计)

| 层 | Ralph 原版 | ralph-goal-loop(Hermes 侧) |
|---|---|---|
| **外层串行** | bash `for i in $(seq 1 $MAX_ITERATIONS)`,每轮 fork Claude Code 进程 | **`GoalManager` 状态机**,`evaluate_after_turn` 跑 judge,judge 说 `done` → 退。**同进程** |
| **内层并行 fan-out** | Claude Code 进程内单 turn 多个 `Task` tool_use | **`delegate_task(tasks=[...])`** batch mode,内部 `DaemonThreadPoolExecutor(max_workers=10)`,自动并发 |
| **完成协议** | grep `<promise>COMPLETE</promise>` in stdout | worker echo `<promise>` + judge LLM 双判定 |
| **状态持久化** | `prd.json` + `progress.txt` 磁盘 | `SessionDB.state_meta`(`goal:<session_id>`) + `prd.json` + `progress.txt` 磁盘 |
| **可暂停** | 重启脚本 | `/goal pause` / `/goal resume` / `/goal clear` 走 `GoalManager` |
| **想法→契约** | 人写 prd.json | `draft_contract()` + `/goal draft` 辅助 LLM 自动扩 |
| **用户中间 review** | 改 prd.json 文件 | `goal_manager.pause("等待老板 review prd.json")` + `/goal resume` |

**核心 trade-off**:`delegate_task(tasks=[...])` 的 fan-out 跟 `delegate_task(goal=..., context=...)` 是同一个 tool 的不同 mode——传 `tasks=[N>=2]` 走 batch,**只有 N>=2 才走 thread pool**(N=1 退化成单 child 串行)。本 skill orchestrator **必须在 N>=2 时才走 fan-out**,N=1 时 inline 跑避免不必要的 pool 开销。

## Priority Grouping & Batch Strategy(核心伪代码)

```python
def ralph_goal_loop(prd_path, max_iterations=10, cost_cap_usd=5.0):
    prd = read_json(prd_path)
    goal_text = f"实现 {prd['title']} 的所有 userStories(共 {len(prd['userStories'])} 个),全部 passes=true"

    # Step 1: /goal draft 自动扩契约(借 hermes_cli.goals::draft_contract)
    contract = draft_contract(goal_text)  # → GoalContract(outcome/verification/constraints/...)
    show_to_user(contract)  # ★ 老板 review 节点 ★
    if not user_approves(contract):  # /goal pause + 老板 /goal resume
        return "USER_REJECTED_CONTRACT"

    # Step 2: 借 GoalManager 设 goal + 写 state_meta
    goal_manager = GoalManager(session_id)
    goal_manager.set(goal_text, contract=contract)

    # Step 3: 拆 GoalContract → prd.json userStories(每 acceptanceCriteria 拆 1 个 story)
    prd = expand_contract_to_prd(contract)
    write_json(prd_path, prd)

    # ★ 拆完 prd.json 后,再 pause 一次让老板 review 拆解结果 ★
    goal_manager.pause("等待老板 review prd.json (story 拆分结果)")
    if not user_approves_prd():
        return "USER_REJECTED_PRD"

    # Step 4: 外层循环 — 每轮挑同一 priority 的 pending stories 一起 fan-out
    for round_idx in range(max_iterations):
        pending = [s for s in prd["userStories"] if not s["passes"]]
        if not pending:
            return "ALL_PASSES"  # 全部完成

        top_priority = min(s["priority"] for s in pending)
        batch = [s for s in pending if s["priority"] == top_priority]

        # Step 5: 内层并行 — delegate_task batch mode
        if len(batch) == 1:
            # 单 story 退化成 inline,避免 pool 开销
            result = run_single_story_inline(batch[0], prd_path)
        else:
            # 多 story 走 batch mode
            tasks = [build_story_task(s, prd_path) for s in batch]
            result_json = delegate_task(
                tasks=tasks,
                parent_agent=self,
                background=False,  # 阻塞等结果
                # skip_memory 默认 True(子 agent hard-coded,见 caveats)
            )
            # result_json 形如 {"results":[{task_index, status, summary, exit_reason, ...}, ...]}
            result = parse_batch_result(result_json)

        # Step 6: 借 /goal judge 判定
        last_response = aggregate_summaries(result["results"])
        decision = goal_manager.evaluate_after_turn(last_response)
        if not decision["should_continue"]:
            return "GOAL_DONE"
        # should_continue=True → worker 标 passes=false / 中途出错,触发下一轮 retry

        # Step 7: 成本控制
        if cost_so_far > cost_cap_usd:
            return "COST_CAP"

    return "MAX_ITERATIONS"
```

## Launching the Loop

```bash
# 1. 准备 prd.json(自己写 / 抄模板 / /goal draft 辅助生成)
cd C:/Users/Administrator/Desktop/hermes-ralph-goal-loop-test
ls prd.json  # 确认有

# 2. 启动 loop
hermes ralph --prd ./prd.json
# 等价于: /goal draft "<prd title>" → 老板 review → 拆 prd.json → 老板 review → /goal set + fan-out
```

**老板 review 节点有 2 个**:
- **节点 1**(GoalContract 拆完后):`draft_contract()` 输出 GoalContract,主 skill 显示给老板,老板可 `/goal show` 改 → `/goal resume`
- **节点 2**(prd.json 拆完后):orchestrator 写完 prd.json,`goal_manager.pause("等待老板 review prd.json")`,老板 `cat prd.json` 看 → `/goal resume` 继续

**两次 pause 是显式的**——任何 1 次老板不满意,都能直接 `/goal clear` 全盘放弃。

## PRD Contract Schema

顶层字段对齐 `mikeyobrien/ralph::prd.json.example`:

```json
{
  "branchName": "ralph-goal-loop/<feature>",
  "title": "短标题",
  "description": "目标 + 上下文",
  "userStories": [
    {
      "id": "US-1",
      "title": "单 story 标题",
      "priority": 1,
      "passes": false,
      "acceptanceCriteria": [
        "File xxx.py exists",
        "Running `python xxx.py` exits 0 and prints '...'",
        "test_xxx.py exists and `python -m unittest test_xxx` reports OK"
      ]
    }
  ]
}
```

**GoalContract 字段 vs prd.json 字段** 对位(本 skill 借 `/goal` 的契约思想,套到 Ralph schema 上):

| `/goal` `GoalContract` 字段 | prd.json 对位 | 用途 |
|---|---|---|
| `objective` | `title` + `description` | 高层目标 |
| `verification` | `acceptanceCriteria[]` 总和 | "什么叫 done" |
| `constraints` | (无对位,写到 worker prompt) | 边界条件 |
| `boundaries` | (无对位,写到 worker prompt) | 不要做啥 |
| `stop_when` | `passes: true` 全部为 true | 终止协议 |
| (n/a) | `priority` | 跨 story 排序(超出 `/goal` 原生范围) |
| (n/a) | `passes: bool` | orchestrator 维护的中间状态 |

`GoalManager` 不知道 `priority`——`/goal` 原生是单 goal 反复跑,**`priority` 是本 skill orchestrator 维护的额外维度**。这正是 `ralph-goal-loop` 跟 `/goal` 的边界:本 skill 接管 story 编排,`/goal` 接管 judge 引擎。

## Monitoring & Kill Behavior

```bash
# 实时进度
hermes goal show         # 当前 goal + verdict + 进度
cat prd.json | jq '.userStories[] | {id, priority, passes}'

# 暂停 / 恢复
hermes goal pause        # 在两个 review 节点之间可手动 pause
hermes goal resume       # 继续跑

# 中途看 cost
hermes usage             # 累计 cost

# 强 kill
hermes goal clear        # 删 goal state
# 然后 orchestrator 退出
```

**kill 触发条件**(orchestrator 自动判):
- `max_iterations` 到(默认 10 轮)
- `cost_cap_usd` 到(默认 5.0)
- 全部 `passes: true`(正常完成)
- judge LLM 返回 `done`(软判定)
- `goal_manager.evaluate_after_turn` 返回 `should_continue: False`(显式 done)

## Cost & Iteration Caps

| 参数 | 默认 | 硬卡? | 越界行为 |
|---|---|---|---|
| `max_iterations` | 10 | ✅ 硬卡 | 立即 abort,留 progress.txt 痕迹 |
| `max_budget_usd` | 5.0 | ✅ 硬卡 | 立即 abort,留 progress.txt 痕迹 |
| `goal.max_turns`(`config.yaml::auxiliary.goal_judge.max_turns` 或 `goals.max_turns`) | 20(走 `/goal` 默认) | ✅ 硬卡 | judge fail 兜底,见 Caveats |
| judge LLM 选哪个 | **默认走主对话模型**(同 worker world-view) | 软 | 老板可在 `config.yaml::auxiliary.goal_judge.{provider,model}` 改 |

**judge model 设计意图**(默认主对话 + 允许覆盖):

> **Default**: judge LLM uses the SAME model as the worker (the main conversation model). Rationale: 当 judge 跟 worker 是同一个模型时,它们共享同一个"worldview"——同套 domain knowledge,同套"什么算 done"直觉。不同模型 judge 可能在边界情况下不同意,不是 work 错,而是 default 不同。
>
> **User override**: set `auxiliary.goal_judge` in config.yaml to route the judge to a different model. The `/goal` engine + `agent.auxiliary_client.call_llm` already supports this routing — 本 skill 不加新机制。
>
> Example overrides:
> ```yaml
> auxiliary:
>   goal_judge:
>     provider: openrouter
>     model: google/gemini-3-flash-preview
>     timeout: 10
>     max_tokens: 500
> ```
>
> **Failure mode**: if `auxiliary.goal_judge` is configured but the call fails (network, auth, rate-limit), `/goal` official behavior is **fail-OPEN** — the judge returns `("continue", ...)` and the turn budget is the backstop. 本 skill 继承。
>
> **Cost note** (judge call is ~200 input tokens + ~50 output tokens per turn):
> - Same model: ~$0.0003/turn (M2.7-class)
> - Gemini Flash: ~$0.00003/turn
> - Local SLM: $0
> - Loop budget: 20 turns default → per-loop cost in the cents.

## Memory & Cache Invariants(硬规则)

| 不变量 | 怎么保证 |
|---|---|
| **子 agent 永远不写 parent memory** | `delegate_task` 在 `tools/delegate_tool.py:240` 硬编码 `AIAgent(..., skip_memory=True, ...)`。**传 `skip_memory=True` 不会让现状改变,因为本来就是**——但你必须**不传 `skip_memory=False`**(没这个 kwarg,传了会被忽略,见 Caveats) |
| **主 skill 自身正常写 memory** | orchestrator 不传 `skip_memory=True`(per `agent/agent_init.py:2211` `AIAgent.__init__(... skip_memory: bool = False, ...)`) |
| **不破 prompt cache** | orchestrator 在同一进程反复调 `delegate_task`,**system prompt prefix 不变**(provider 端 prefix cache 自动复用);子 agent 是 fresh conversation,parent 完全看不到 child 内部 context(per `tools/delegate_tool.py:5-11`) |
| **DELEGATE_BLOCKED_TOOLS 不动** | `terminal` / `file` / `edit` / `write` 全 OPEN(per `tools/delegate_tool_toolsets.py:14` 定义的 `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, cronjob_manage}`),子 agent 改 prd.json + 写 progress.txt 无阻碍 |
| **子 agent 不递归调 delegate_task** | `DELEGATE_BLOCKED_TOOLS` 包含 `delegate_task` 本身,Nesting 边界 hard-capped(per `tools/delegate_tool.py:449-456` depth check) |

## `<promise>COMPLETE</promise>` Protocol

worker 实施完所有自己 story 后,**必须**在 response 末尾 echo literal token `<promise>COMPLETE</promise>`,这是 worker 侧的硬要求(per `CLAUDE.md` 模板)。orchestrator 侧**双保险**:
1. grep worker `summary` 文本是否含 `<promise>COMPLETE</promise>`(worker 软协议)
2. `goal_manager.evaluate_after_turn(last_response)` judge 软判定(主 LLM 看 work 报告判 done)

**任一触发 → 退**。两条都失败(worker 漏写 + judge 误判 continue)→ orchestrator 继续 retry,直到 `max_iterations` 卡死。

## Pitfalls

1. **让 worker 改 prd.json 后没回传 `<promise>COMPLETE</promise>`** — Ralph 协议靠这个 grep 退出,worker 漏写 = 主循环死锁;**mitigation**: `CLAUDE.md` worker prompt 模板必须强制要求"完成后 MUST echo 此 token",orchestrator 也用 `judge_goal` 软判定兜底
2. **同 priority story 没分组 fan-out** — 直接把所有 pending story 一起跑,priority 2 撞 priority 1 产出 = 文件冲突 / 上下文错乱;**mitigation**: orchestrator 强制按 `min(priority)` 分组,跨 priority 等上一组全 `passes=true` 才开下一组
3. **子 agent 期望 `skip_memory` 透传** — `delegate_task` 根本没有这个 kwarg,`skip_memory=True` 在 `tools/delegate_tool.py:240` 硬编码到 child AIAgent 构造;**mitigation**: orchestrator 入口**不传** `skip_memory`,等价于"已自动开启"
4. **`max_budget_usd` 没硬卡** — 单 loop 没界,cost spike 跑飞;**mitigation**: `hermes ralph` 启动时校验,超出立即 abort 不 retry(不留中间状态)
5. **走 `delegate_task` 但只有 1 个 task** — 不会走 batch mode(`tools/async_delegation.py` 的 `_executor` 只在 `len(tasks) >= 2` 时启用),等于退化成串行;**mitigation**: orchestrator 主动判定,1 个 story 时直接 inline 跑不走 batch
6. **prd.json 在 dirty 主 checkout 里改** — Ralph 实施文件改动 + prd.json 改动混在同一次 commit 里,后面 cherry-pick / revert 都麻烦;**mitigation**: orchestrator 启动时**强制要求 git worktree**(跟 `kanban-codex-lane` 同一套 worktree pattern)
7. **退化成"借外部 Claude Code 跑"** — fork 进程 30 秒 exit,失去 Hermes 自己的 cache 边界 + cost 路径;**mitigation**: 本 skill description 第一句写明 "0 external CLI, 100% Hermes in-process"
8. **`auxiliary.goal_judge` 配错** — 老板配了一个不存在的 provider/model,judge call 静默 fail,主循环退化成"一直 continue"直到 max_iterations;**mitigation**: orchestrator 启动时 sanity check `goal_judge_setting()` 返回有效值,无效就 warn 但不 abort
9. **paused 状态被外部改** — 老板中途 `/goal clear`,orchestrator 还不知道,继续跑下一轮;**mitigation**: orchestrator 每轮 `goal_manager.is_active()` 校验,not active → abort 报 `GOAL_CLEARED`

## Verification Checklist

完成本 skill 落地后,声明"完成"前逐项过:

- [ ] `hermes ralph --help` 显示正确帮助(argparse 分支注册成功)
- [ ] 实跑本机 `C:/Users/Administrator/Desktop/hermes-ralph-goal-loop-test/` 的 3-story prd.json,验证:
  - [ ] 至少 3 轮迭代(每 priority 1 轮)
  - [ ] prd.json 全部 `passes: true` 后进程退出
- [ ] 内层并行真的发生 — 看 `~/.hermes/cache/delegation/live/<id>/task-*.log` 确认 `delegate_task(tasks=[N>=2])` 同 turn 触发,`DaemonThreadPoolExecutor` 实际并发
- [ ] 终止协议生效 — worker 输出含 `<promise>COMPLETE</promise>` → orchestrator grep 命中 → 退
- [ ] 中间 review 节点生效 — `goal_manager.pause("等待老板 review prd.json")` 后 orchestrator 真的停在等 `/goal resume`,老板 `/goal resume` 后继续
- [ ] `git diff` 在 `hermes-agent/` 是 **0 改动**(纯新增 skill + `hermes ralph` CLI argparse 分支)
- [ ] `agent/prompt_builder.py` / `tools/delegate_tool.py` / `DELEGATE_BLOCKED_TOOLS` 三件套 0 改动(不变量)
- [ ] `metadata.hermes.related_skills` 引用了 `kanban-codex-lane` + `openclaw-plugin-author-suite` + `hermes-agent`
- [ ] cost cap 真的生效 — 把 `max_budget_usd` 设成 $0.01,跑一个会超的 prd,验证立即 abort 不继续
- [ ] judge LLM 走 `auxiliary.goal_judge` 路由真的生效 — 配 OpenRouter Gemini Flash,跑 1 轮 judge,看 `~/.hermes/logs/` 里 judge call 的 model 字段
- [ ] negative test:故意写 1 个 story 的 acceptance criteria 错(比如 `assert 1==2`),验证子 agent 标 `passes=false` 不退出,主 skill 进入下一轮 retry 直到 max_iterations

## References

- `~/.hermes/skills/kanban-codex-lane/SKILL.md` — 同思路("外部 CLI 当 implementation lane"对位,本 skill 是它的"自给自足版"——`delegate_task` 替代 Codex CLI)
- `~/.hermes/skills/openclaw-plugin-author-suite/SKILL.md` — 主 skill,本 skill 跟它共享 Ralph 集成层
- `~/.hermes/skills/openclaw-plugin-author-suite/references/ralph-integration.md` — Ralph 集成进 Hermes skill 的"金矿"判断(为什么用 `/goal` 而不是新写 ralph-loop)+ 实测 demo fixture 模板(本 skill 的 `CLAUDE.md` worker prompt 部分借自该 reference)
- `~/.hermes/cache/hermes-ralph-feasibility-2026-09-17.md` — 可行性报告(本 skill 落地的依据 + 两层并行模型分析 + 三方案对比)
- `~/.hermes/plans/hermes-ralph-skill-port-2026-09-17.md` — 实施 plan(Phase 1-4 拆解 + 验证清单)
- `mikeyobrien/ralph` 的 `CLAUDE.md` + `prd.json.example`(外系,作为 prd schema 的事实标准,本 skill 直接对齐)

## Out of scope(不在范围内)

- ❌ 跑 mikeyobrien/ralph 原版 bash 循环(本 skill 是自给自足版,不需要)
- ❌ 跨 profile 跑(本 skill 只在自己 profile 跑;跨 profile 走 Kanban)
- ❌ 跨机器并行(走 Kanban + 多 profile gateway,不是 ralph-goal-loop)
- ❌ 修改 `hermes-agent/` 核心任何文件(本 skill 纯新增)
- ❌ 替代 Hermes `/goal` slash command(本 skill 是 `/goal` 之上的薄包装,不是替代品)
- ❌ 替代 Kanban / openclaw-plugin-author-suite(那些是不同形状的工具)
- ❌ 实装 git worktree 自动创建(老板使用时手动 `git worktree add`,本 skill 启动时**检测**在 worktree 内但不自动创建)

## Not verified(诚实交代)

- 0 step 2 的 `goal_manager.set(goal_text, contract=contract)` 在老板的 v0.21.3 + 已 update 的环境下是否真能传 `contract` 形参(`GoalManager.set` line 1132 标了 `contract: Optional[GoalContract] = None`,但没在 v0.21.3 实跑过 `set(..., contract=...)`)
- 0 step 5 的 `delegate_task(tasks=[...])` 在 `hermes chat` 外(纯 `hermes ralph` CLI 模式)是否能调通(`hermes_agent` 必传 `parent_agent=self`,CLI 模式下 orchestrator 没有 `parent_agent` 怎么处理——可能要走 `AIAgent(...)` 自己构造再传)
- 0 sub-agent 报告说 `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, cronjob_manage}`,这是从 `tools/delegate_tool_toolsets.py:14` 读出来的;但**实际的阻塞是在 child agent 收到 task 时 filter tools,不是 child 的 toolset 里没有**,这条对 worker 写 prd.json 不构成阻碍,但 precision 仍待 Phase 3 实测验
- 0 judge LLM 的具体 token 消耗 + 主模型 judge vs Gemini Flash judge 实际质量差(per `/goal` 官方说 ~200 token,Phase 3 实测验)
- 0 跨 priority batch 间的"等上一组全 passes=true 才开下一组"——这个 orchestrator 逻辑本 skill 自己实现,`/goal` 不管 priority。Phase 3 实测验
