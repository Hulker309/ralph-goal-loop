> ⚠️ **状态说明(2026-09-17 核对)**:本文是 **v0.1.0 设计文档,不是代码现状**。
> 文中带 `_stepN_` 前缀的方法名(`_step6_outer_loop` / `_step7_run_batch` / `_step8_evaluate` /
> `_step4_expand_to_prd` / `_build_story_task` / `_check_file_overlap` / `run_single_story_inline`)
> 在 `scripts/ralph.py` 里**全部不存在** —— 脚本已改道到另一套命名(`_outer_loop` / `_run_batch` /
> `_evaluate` / `_step_recon` / `_render_worker_prompt`)。本文保留,作为**设计意图**参考。
> 要核对真实行为,以 `scripts/ralph.py` 和 `SKILL.md` 为准。

# Two-Layer Model — 为什么 /goal + Ralph 是 hybrid,不是 replacement

> 配 `SKILL.md §Two-Layer Model` 段。本文件是它的 fact-only 展开:为什么混合两套东西、各层负责什么、明确边界、为什么不是 replacement。

## 1. /goal 原生 — 不知道 priority / 不知道 prd.json

Hermes `/goal` slash command 在 `hermes_cli/goals.py` 整个文件 1695 行里实现,核心 4 个原语:

| 原语 | file:line | 不知道什么 |
|---|---|---|
| `GoalContract` | `goals.py:280` | 不知道 multi-story,不知道 priority,只管"这 1 个 goal 的 outcome/verification/constraints/boundaries" |
| `draft_contract(objective)` | `goals.py:1008` | 不知道 prd.json schema,只把 1 句 objective → GoalContract |
| `judge_goal(goal, last_response, ...)` | `goals.py:864` | 不知道 prd.json,只 judge 单 goal |
| `GoalManager(session_id)` | `goals.py:1053` | 不知道 multi-story 编排,只管 1 个 active goal 的 set/pause/resume/clear/evaluate |

**关键不变量**:`GoalManager` 的 `GoalState`(per `goals.py:390`)**没有** priority 字段,**没有** userStories 列表,**没有** `passes: bool` 概念。它对单 goal 反复跑 judge 这一件事非常精,其它什么都不知道。

**完成协议**:verdict ∈ {`done`, `continue`, `wait`, `blocked`}(per `goals.py:1491` 跟 `goals.py:1485` 跟 `goals.py:1476` 跟 default)。`done` → 状态变 `done` + return `should_continue=False`。

## 2. Ralph 原版 — 不知道 judge / 不知道 state_meta

`mikeyobrien/ralph` 是 bash 循环(per SKILL.md §Overview "Two-Layer Model" 段直接引用):

```bash
for i in $(seq 1 $MAX_ITERATIONS); do
  # 调 Claude Code 进程
  claude --prompt-file prompt.md
  # grep <promise>COMPLETE</promise>
  if grep -q "<promise>COMPLETE</promise>" output.txt; then
    exit 0
  fi
done
```

**关键不变量**(per SKILL.md §Two-Layer Model):

| 原版 Ralph 不知道什么 | 为什么 |
|---|---|
| judge LLM | 靠 grep `<promise>` 字符串,不调独立 LLM 做软判定 |
| `state_meta` SQLite 持久化 | 状态靠 prd.json + progress.txt 文件系统,无 `goal:<session_id>` 持久层 |
| pause/resume 跟 host session 联动 | 进程死了就死了,重启脚本重来 |
| cost cap 自动卡 | 全靠 `MAX_ITERATIONS` + 人工看 usage |
| 跨 priority 依赖 | prd.json 里 priority 字段是 hint,真正的串行靠 bash 外的脚本逻辑 |

**完成协议**(per SKILL.md §`<promise>COMPLETE</promise>` Protocol 段):

> worker 实施完所有自己 story 后,**必须**在 response 末尾 echo literal token `<promise>COMPLETE</promise>`,这是 worker 侧的硬要求

**纯字符串 grep,无 LLM 兜底**。

## 3. ralph-goal-loop — 上两者叠起来

外层 Ralph 风格 prd.json + 内层 /goal 风格 judge 引擎。`RalphGoalLoop` 类(本 skill `scripts/ralph.py`)在 Hermes 进程内做:

```
┌──────────────────────────────────────────────────────────────────┐
│  orchestrator (RalphGoalLoop)                                    │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ outer loop: read prd.json → pick top-priority batch → fan-  │  │
│  │ out via delegate_task(tasks=[N>=2]) → aggregate results →   │  │
│  │ call goal_manager.evaluate_after_turn → continue / done     │  │
│  └────────────────────────────────────────────────────────────┘  │
│           ↓ GoalManager.set(goal_text, contract=...)              │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ inner engine: hermes_cli/goals.py 1695 行 /goal state      │  │
│  │ machine — judge_goal + state_meta + pause/resume + cost     │  │
│  │ cap, 0 modify                                                │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 3a. 数据流(单 round 1 内)

```
Round N start
   │
   ├─ orchestrator: read prd.json
   │     pending = filter(s.userStories where !s.passes)
   │     top_priority = min(s.priority for s in pending)
   │     batch = [s for s in pending if s.priority == top_priority]
   │
   ├─ orchestrator: delegate_task(tasks=[build_story_task(s) for s in batch],
   │                              parent_agent=self,
   │                              background=False)
   │
   ├─ delegate_task 内部: DaemonThreadPoolExecutor(max_workers=10) 起 N 个 child AIAgent
   │     每个 child: 读 prd.json → 读 progress.txt 顶部 Codebase Patterns → 实施自己 story
   │                 → 标 passes=true → append progress.txt ## [Story ID]
   │                 → echo "<promise>COMPLETE</promise>" if 全 passes
   │
   ├─ delegate_task 返回: json {"results":[{task_index, status, summary,
   │                                          exit_reason, tokens, ...}, ...]}
   │
   ├─ orchestrator: aggregate summaries → last_response
   │
   ├─ orchestrator: goal_manager.evaluate_after_turn(last_response)
   │     → 内部: judge_goal(state.goal, last_response, contract=state.contract)
   │            → 内部: _call_goal_judge_llm(call_llm, ...)
   │     → 返回: {"status", "should_continue", "continuation_prompt",
   │              "verdict", "reason", "message"}
   │
   ├─ orchestrator: 检查 should_continue / cost_cap / max_iter
   │
   └─ next round 或 return "GOAL_DONE" / "COST_CAP" / "MAX_ITERATIONS"
```

## 4. 为什么要 hybrid — 不是 replacement

(直接引用 SKILL.md §Two-Layer Model 跟 §Overview 的设计意图)

**A. /goal 已经有 380 行(实际 1695 行,核心 ~400 行)+ state_meta 持久化 + judge fail-OPEN + 可路由 `auxiliary.goal_judge`**

重写这套 = 重新造 `/goal` 的 judge 引擎 + state 持久化 + judge fail-OPEN 兜底 + cost cap + transport/parse failure pause 逻辑。这些 `hermes_cli/goals.py` 已写完且已测,**`/goal` 是 v0.13.0 "Tenacity Release" 起就是 first-class primitive**(per `references/openclaw-plugin-author-suite/references/ralph-integration.md:21` 直接引用)。

**B. Ralph 原版 bash 循环在 Hermes 上跑不动**

要 fork Claude Code 进程 → 破 prompt cache 边界(per SKILL.md §Memory & Cache Invariants "不破 prompt cache") + 失去 Hermes 自己的 cost 路径 + 无法联动 `/goal` state + 无法 fan-out children(per SKILL.md §Pitfalls 第 7 条直接引用 "退化成借外部 Claude Code 跑 — fork 进程 30 秒 exit,失去 Hermes 自己的 cache 边界 + cost 路径")。

**C. prd.json 编排是 /goal 原生不覆盖的**

/goal 原生是单 goal,没有 priority 概念,没有 userStories 列表,没有 `passes: bool`。要支持 multi-story 编排,**必须在 /goal 之外加一层 orchestrator**。这层 orchestrator 只做 1 件事:**按 priority 分组 + fan-out children**。

**结论**:hybrid 是**唯一**不浪费 1695 行 /goal 代码、不 fork 进程、不破 cache、不放弃 prd.json 编排的路径。**`replacement` 这条路在 Hermes 0.21.3 走不通**。

## 5. 跟 /goal 的 3 个明确边界

### 5a. priority 概念是 orchestrator 维护的,不是 /goal 的

```python
# GoalManager 不读 prd.json, 不知道 priority
# orchestrator 在 _step6_outer_loop 里:
top_priority = min(s["priority"] for s in pending)  # ← 这行是 orchestrator 逻辑
batch = [s for s in pending if s["priority"] == top_priority]
```

`GoalState` 字段(per `goals.py:390`):`goal / status / turns_used / max_turns / contract / subgoals / gates / paused_reason / waiting_* / consecutive_*_failures / last_verdict / last_reason / created_at / last_turn_at`。**没有** `priority`,**没有** `userStories`,**没有** `passes`。

`/goal` judge 看的 `last_response` 是 orchestrator aggregate 出来的,**不**直接看 prd.json。judge 只问 "你说的 goal 完成了吗?",不关心 prd.json 里 priority 1/2/3 的拆分。

### 5b. prd.json 是 orchestrator 写的,不是 /goal 的

`/goal draft` 跟 `draft_contract()` 输出 GoalContract(`goals.py:1008`),不输出 prd.json。**`GoalContract → prd.json userStories` 是 orchestrator 自己做的**(per SKILL.md §Two-Layer Model 表里 "`/goal` `GoalContract` 字段 vs prd.json 字段" 对位段)。

具体:orchestrator `_step4_expand_to_prd(contract)` 把 `contract.acceptance_criteria[]` 拆成 `userStories[]`(每 acceptance criterion → 1 个 story,priority 按顺序 1/2/3 默认)。`draft_contract()` 缺位时(per `goals.py:1037` 返回 None),orchestrator 走"老板自己写 prd.json + orchestrator 只校验 schema"路径。

### 5c. /goal 知道 story 完成,Ralph 知道 prd.json 状态

**互补而非重叠**:

- `/goal` 状态机判定 **"goal 这个抽象对象做完了吗"** —— `state.status == "done"`,`verdict == "done"`
- orchestrator 判定 **"prd.json 里所有 story 的 passes=true 了吗"** —— 直接 read prd.json + filter `s.passes`

这两条判定**不互相替代**:

| 场景 | /goal judge 怎么判 | orchestrator 怎么判 | 谁退? |
|---|---|---|---|
| worker 真做完所有 acceptance criteria + echo `<promise>` | verdict=done | pending=[] | 任意一边判 done 都退 |
| worker echo `<promise>` 但 acceptance criteria 没全做完 | verdict=continue(judge 看到 summary 矛盾) | pending=[] if worker 真改了 passes,else 还有 pending | judge + passes 双重判定,过严 |
| worker 没 echo `<promise>` 但 prd.json 全 passes=true | verdict=done(judge 看 summary 推断) | pending=[] | 都退 |
| worker 只做完一半 | verdict=continue | pending=[剩下一半] | 都不退,进下一轮 retry |

## 6. 跟 Ralph 原版的边界

| 边界 | Ralph 原版 | ralph-goal-loop |
|---|---|---|
| 进程模型 | fork Claude Code 进程(per fork) | Hermes in-process delegate_task 子 agent(per delegate_tool.py:240 硬编码 skip_memory=True) |
| 状态持久层 | prd.json + progress.txt 文件 | prd.json + progress.txt + `state_meta` SQLite (`goal:<session_id>`) |
| pause/resume | 重启脚本 | `goal_manager.pause("...")` + `/goal resume`(per goals.py:1150 跟 1158) |
| judge 机制 | 字符串 grep `<promise>` | grep `<promise>` + `_call_goal_judge_llm` 双判定 |
| cost cap | 人工看 usage | `max_budget_usd` hard cap + `goal.max_turns` budget pause |
| transport failure 兜底 | 无 | `consecutive_transport_failures >= DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES` 自动 pause(per goals.py:1499) |

## 7. 不在范围内(故意不做)

- ❌ 把 `GoalManager` 改成认识 prd.json / priority(改 hermes-agent 核心 = 0 modify 失败)
- ❌ 把 `judge_goal` 改成看 userStories(同上)
- ❌ 取消 `<promise>COMPLETE</promise>` 协议(去掉双保险 = 单点故障风险)
- ❌ 在 orchestrator 自己里写 judge 逻辑(重造 `_call_goal_judge_llm` = 浪费)
- ❌ 跑 bash `for i in $(seq 1 N)` 循环(per SKILL.md §Pitfalls 第 7 条,fork 进程 = 破 cache 边界)
