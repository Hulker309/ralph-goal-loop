---
name: ralph-goal-loop
description: "Use when you have a prd.json with multiple user stories and want a single Hermes Python process to implement them in priority + dependsOn order via a persistent AIAgent, exiting only when every story is `passes: true` (signaled by the literal `<promise>COMPLETE</promise>` token). Pure-Python RalphCore (orchestrator + prd/progress/promise) plus a thin PlatformAdapter layer (HermesAdapter concrete + OpenCLAWAdapter stub). v1.0 drops v0.4's Hermes /goal engine, judge LLM, recon worker, and delegate_task fan-out — a true fidelity port of mikeyobrien/ralph. 0 external CLI, 0 modify hermes-agent core, 100% Hermes in-process. Triggers on: 'run ralph', 'multi-story prd', 'per priority 跑', 'prd.json loop'."
version: 1.0.0
author: Hermes Agent (希尔, 2026-09-18)
license: MIT
platforms: [linux, macos, windows]
changelog:
  - v1.0.0 — **True-fidelity port of mikeyobrien/ralph.** Decompose `scripts/ralph.py` into `scripts/core/` (pure-Python RalphCore: orchestrator/prd/progress/promise) + `scripts/adapters/` (PlatformAdapter abstract + HermesAdapter concrete using AIAgent.run_conversation + OpenCLAWAdapter TODO stub). Drop Hermes `/goal` engine, judge LLM, recon worker, and `delegate_task` fan-out. New architecture uses a single persistent AIAgent session for the entire run — equivalent to upstream Ralph's `cd $PROJECT_ROOT && claude --prompt-file CLAUDE.md`. Statuses renamed: GOAL_DONE→ALL_PASSES, GOAL_CLEARED→RUN_PAUSED. End-to-end smoke test on examples/three-stories/ passes 3/3 in ~2 minutes. 39/39 unit tests pass (test_starvation 15/15 + test_parallel 24/24).
  - v0.4.0 — **修掉优先级饥饿**。priority 从「硬闸」改成「排序偏好」:能不能跑只由 `dependsOn` 决定,
    priority 只决定先够哪个,不再拦后面的层。失败处理独立成一条规则:派出去仍 `passes: false` 的 story
    记一次尝试,满 `--max-story-attempts`(默认 3)后**下场(bench)**,其余 story 继续推进。依赖链上
    的坏死故事用传递闭包识别。终止状态拆成三个:`NO_PROGRESS`(10,活死锁,如依赖成环)与
    `STORIES_EXHAUSTED`(11,死故事,附谁死了/为什么),都不再空转到 `MAX_ITERATIONS`。
    旧行为的代价:一个跑不通的 story 让整个 prd 空转,预算全烧在重试它上。
  - v0.3.0 — 三件事。① **并行真的能发生了**:批量分组从写死的 `min(priority)` 改成调用方可选的
    `--parallel-mode off|priority|auto|manual`,默认 `auto` 按 `dependsOn` + 文件重叠(用 story 自己声明的
    `files` 字段)跨 priority 分组;文件未知的 story 单独跑,依赖不可满足时立刻返回 `NO_PROGRESS` 而不是空转。
    旧行为的问题:`min(priority)` 遇上上游 `ralph` skill 的编号习惯(每个 story 一个号)⇒ 批批只有 1 个
    story,并行从不触发。② **不再硬编码模型**:`_init_parent_agent` 去掉了写死的 provider/model 兜底,
    只认 `--worker-provider/--worker-model` 或 `config.yaml`,都没有就交给 Hermes 自己的 provider ladder。
    ③ **skill 里不留任何密钥**:仓库内无 secret(全库扫描),git remote URL 里内嵌的 token 已移除。
  - v0.2.0 — 补上 **recon(并行接地)** 与 **跨轮回写闭环**,并把文档与真实实现对齐。
    此前 SKILL.md 与 references/ 描述的是 v0.1.0 设计,其中有多个函数名(`expand_contract_to_prd` /
    `_step4_expand_to_prd` / `_check_file_overlap` 等)在 `scripts/ralph.py` 里并不存在;
    `hermes ralph` 这个入口从未实现(实现它需要改 hermes-agent 核心,与本 skill 的 0-modify 承诺矛盾);
    CLAUDE.md.tmpl 零引用(现标注为设计参考)。
  - v0.1.0 — 初版:复刻 Ralph 的执行段(外层串行 + 同 priority 内 fan-out + judge + `<promise>` 协议)。
metadata:
  hermes:
    tags: [ralph, prd, autonomous-agents, multi-story, single-agent, persistent-session]
    related_skills:
      - kanban-codex-lane
      - openclaw-plugin-author-suite
      - hermes-agent
---

# ralph-goal-loop

> **白话先**: 你有一个 `prd.json` 拆好的多 story 任务,想让它**在 Hermes 自己的进程里**跑完——按 priority + `dependsOn` 顺序,直到全部 `passes: true` 才退。Ralph 通过**单个持久 `AIAgent` 实例**读 prd.json + progress.txt + 真实项目代码,自己决定每个 story 怎么做、怎么验证、怎么写入 `passes: true`。**不**调外部 Claude Code CLI,**不走** bash 循环,**不用** Hermes `/goal` 引擎、judge LLM、recon worker 或 `delegate_task` subagent。完成协议:worker 在 response 末尾 echo literal `<promise>COMPLETE</promise>`,orchestrator 用 `detect_promise()` 字符串检测。**这是 mikeyobrien/ralph 在 Hermes 内的真实形态复刻,不是 re-design**。

## Overview

`ralph-goal-loop` 是 `mikeyobrien/ralph` 的 Hermes-side 移植。架构分两层:

1. **RalphCore** (`scripts/core/`) — pure Python orchestrator,no Hermes imports。读 `prd.json` + `progress.txt`,按 priority + `dependsOn` 编排 story 批次,调用 `PlatformAdapter` 执行 story,检测 `<promise>COMPLETE</promise>`。
2. **PlatformAdapter** — 抽象接口,定义 `run_story / run_orchestrator_turn / get_history / save_history`。当前只有 `HermesAdapter`  concretre,使用一个持久 `AIAgent` 实例跑所有 story(同 session 共享 tool call history)。

启动: `python scripts/ralph.py --prd <path>` — **它不是 `hermes` 的子命令**(见 §Launching the Loop)。Ralph 运行在独立进程,老板的主 Hermes session 完全不受影响。

**0 modify hermes-agent 核心**。**0 spawn 外部 CLI 进程**。**0 破 prompt caching 边界**。

## When to Use

Use `ralph-goal-loop` when:
- 你有一个 `prd.json` 拆好的多 story 任务(每个 story 有 `id / title / priority / acceptanceCriteria / passes`)
- story 之间有**priority 依赖**(priority 2 必须等 priority 1 全 passes=true)
- 同 priority 的 story 是**独立的**(可以并行跑,无文件冲突)
- 你想要**进程内 fan-out**——多 worker 在同 turn 跑,避免串行等 30 秒/轮
- 你想要**judge LLM 软判定**——v1.0 没有 judge,`<promise>` 字符串检测是唯一完成信号

Do NOT use `ralph-goal-loop` when:
- 只有 1 个 story(或 1 个 goal 字符串)——直接用 Hermes `/goal`,不需要这层包装
- story 互不依赖,但你想要**跨机器并行**——用 Kanban + 多 profile,不是 ralph-goal-loop
- 你想用外部 Claude Code CLI 跑——那是 mikeyobrien/ralph 原版,本 skill 故意不接
- 你需要 LLM 软判定("这堆 story 是否算完成")——v1.0 只有硬 `<promise>` 协议

## Two-Layer Model(关键设计)

Ralph v1.0 架构把「编排」和「执行」分开:

| 层 | 组件 | 职责 |
|---|---|---|
| **外层 iteration loop** | `RalphGoalLoop` (`scripts/core/orchestrator.py`) | pure Python,无 Hermes 依赖。按 priority + `dependsOn` 决定下一批跑哪些 story,调用 `adapter.run_story()`,检测 `<promise>COMPLETE</promise>` |
| **per-story agent** | `HermesAdapter` (`scripts/adapters/hermes.py`) | 持有一个 `AIAgent` 实例,`run_story()` 为每个 story 构建 worker prompt,调用 `agent.run_conversation()` 执行。同一 agent 实例贯穿整个 Ralph run,所以 orchestrator 和 worker 的 tool call history 共享同一 session |
| **PlatformAdapter 抽象** | `scripts/adapters/base.py` | 定义 `run_story / run_orchestrator_turn / get_history / save_history` 四个方法,解耦 orchestrator 与 Hermes |

**为什么这样分**:  orchestrator 是纯编排逻辑,可以被任何 adapter 驱动。`HermesAdapter` 是唯一 Hermes-aware 的代码,其余全是纯 Python。这使得测试 orchestrator 不需要真实的 AIAgent,也方便未来接入 OpenCLAW 等其他平台。

## Parallelism — 由调用方决定(`--parallel-mode`)

**背景**:上游 Ralph 是严格串行的(一轮一个 story),它的并发来自「那一轮内部 agent 自己开的多路」。移植到这里,执行本身就是一个 fan-out,**并行必须被决定**,而不是自动出现。

**谁决定**:跑这个 skill 的 agent(或人),用 `--parallel-mode` 决定;planner 只负责执行该策略。

| mode | 行为 | 什么时候用 |
|---|---|---|
| `off` | 一轮一个 story,严格串行 | 想要上游保真 / 排查问题时 |
| `priority` | `priority` 数值相同的进同一批 | 你自己按编号分组时。注意上游 `ralph` skill 的编号习惯是每个 story 一个号 ⇒ 这个模式实际会退化成串行(但失败下场机制仍在,所以卡住的那个会被 bench,后续层能开跑) |
| **`auto`(默认)** | 跨 priority 分组,依据 **`dependsOn` + 文件重叠** | 一般情况。**前提是 stories 声明了 `files`字段**(文件信息来自 story 自身声明) |
| `manual` | 按 story 上的 `parallelGroup` 标签分组 | 你要精确控制分组、不要任何推断时 |

**`auto` 的三条判定**:

1. **依赖优先** —— story 的 `dependsOn` 全部 `passes: true` 才算 eligible。`dependsOn` 可以写 camel 或 snake,值可以是数组或单个 id。
2. **文件不重叠才同批** —— 两个 story 的文件集合只要有交集,就不同批。这是唯一能防「两个 worker 同时写同一个文件 → 静默损坏」的机制。
3. **文件未知 ⇒ 单独跑** —— 没有 `files` 声明时,**未知不等于无冲突**,所以那个 story 独占一批。

**`--max-parallel N`**(默认 10)对所有模式生效,也是并发上限。

### priority 管顺序,`dependsOn` 管资格,失败管下场

这是本 skill 对「优先级饥饿」的修正。三条规则分开,职责不重叠:

| 维度 | 谁负责 |
|---|---|
| **能不能跑** | **`dependsOn`** —— 唯一真正的约束 |
| **先跑哪个** | **`priority`** —— 只是排序偏好,**不拦路**。priority 3 的 story 在 priority 1 还没过时也可以跑(只要依赖满足、文件不冲突) |
| **跑不通怎么办** | **`--max-story-attempts`(默认 3)** —— 派出去但没变成 `passes: true` 就记一次失败,满 N 次**下场(bench)**,不再派给它,其余 story 继续推进 |

**为什么必须这样**:旧的 `min(priority)` 是硬闸 —— 低数值那一层没过,后面每一层永远轮不到。叠加上游「每个 story 一个号」的编号习惯,一个跑不通的 story 就能让整个 prd 空转到 `max_iterations`,**预算全烧在重试同一个 story 上,别的 story 一次都没试过**。现在它最多花 N 次尝试的代价,然后被隔离,run 继续产出。

`--max-story-attempts 0` = 关闭下场机制,永远重试(旧行为,给想要的人留的)。

### 终止状态(八种)

| 状态 | exit | 含义 |
|---|---|---|
| `ALL_PASSES` | 0 | 全部 story 过了 |
| `MAX_ITERATIONS` | 2 | `max_iterations` 轮次用尽 |
| `COST_CAP` | 3 | `cost_cap_usd` 预算耗尽 |
| `DELEGATION_FAILED` | 6 | `adapter.run_story` 抛异常 |
| `RUN_PAUSED` | 7 | 老板暂停后台进程;重跑同 `--prd` 恢复 |
| `INTERNAL_ERROR` | 8 | 未捕获异常 |
| `NO_PROGRESS` | 10 | **活死锁**:story 都在,但当下没一个可跑(典型是 `dependsOn` 成环)。**立刻退,不空转** |
| `STORIES_EXHAUSTED` | 11 | **死故事**:剩下的 story 已经不可能跑完 —— 被 bench 了,或依赖链上有不存在的 id / 已 bench 的 story(传递闭包识别)。**报告谁死了、为什么** |

两种情况都写进 `progress.txt`(`NOTHING RUNNABLE ...` / `NOTHING LEFT TO RUN ...`),不会让你面对一个光秃秃的 `MAX_ITERATIONS`。

**prd.json 可选字段**(全部向后兼容,不写就是旧行为):

```json
{
  "id": "US-008",
  "priority": 8,
  "dependsOn": ["US-001", "US-003"],   // 可选:等这些 story 过了才能开跑(唯一的硬约束)
  "files": ["tools/x.ts", "state/y.ts"], // 可选:显式声明会碰的文件(auto 用它判重叠)
  "parallelGroup": "g1"                  // 可选:manual 模式下按这个标签分组
}
```

`files` 不写时,`auto` 视为文件未知,单独跑该 story。

## Priority Grouping & Batch Strategy(核心伪代码)

> 下面这段是**结构示意**,使用真实方法名。

```python
from core.orchestrator import RalphGoalLoop
from adapters.hermes import HermesAdapter

# 构造
adapter = HermesAdapter(provider=..., model=...)  # or OpenCLAWAdapter
orchestrator = RalphGoalLoop(prd_path, adapter, max_iterations=10, cost_cap=5.0, ...)

# 运行
status = orchestrator.run()
# RalphGoalLoop.run() — pure Python, no Hermes imports
# _run_batch() iterates over stories in the batch and calls adapter.run_story()
# detect_promise() checks for <promise>COMPLETE</promise> in worker output
# _dead_stories() does transitive closure over dependsOn chains
# _record_attempts() benches stories after max_story_attempts failures

return status  # maps to exit code: 0/2/3/6/8/10/11
```

## Launching the Loop

Ralph 运行在**独立 Python 进程**里,老板的主 Hermes session 完全不受影响。

```bash
# 1. 准备 prd.json(自己写 / 抄模板 / 用上游 prd + ralph 两个 skill 从 PRD.md 转)
#    在**项目根目录**下跑(或显式 --project-root)。
cd /path/to/your/project
ls prd.json  # 确认有

# 2. 启动 loop
python scripts/ralph.py --prd ./prd.json --project-root .

# 想用别的模型跑(不动全局 config.yaml),加这两个:
#   --worker-provider deepseek --worker-model deepseek-flash
```

### 暂停 / 恢复

Ralph 没有 `/goal` 那样的内置 pause/resume。暂停方式是:
- **Ctrl-C / SIGTERM** — 优雅停止,写 progress.txt;下次同 `--prd` 重跑时 agent 从磁盘加载历史记录,继续上次的位置。
- **kill -9** — 不优雅,可能丢未保存的 history;但 prd.json 本身是持久的,下次重跑会从当前 prd.json 状态继续(只是重复跑最后几轮)。

老板想中间 review,直接看 `progress.txt` 和 `prd.json` 即可,不需要暂停机制。

## Model & Credentials

**不硬编码模型。** 模块里没有任何写死的 provider / model 名。解析顺序:

```
--worker-provider / --worker-model     ← 显式覆盖,只对本次 run 生效
        ↓ 没有的话
config.yaml::model.{provider,default}  ← 跟 `hermes chat` 用同一份配置
        ↓ 还是没有
交给 Hermes 自己的 provider ladder      ← 传 None,由 Hermes 决定
```

想临时换模型跑,**不要改 `config.yaml`**,传 flag 就行:

```bash
python scripts/ralph.py --prd ./prd.json --project-root . \
  --worker-provider <provider> --worker-model <model>     # 用你环境里真有的值
```

**仓库里不留任何密钥。** 所有凭证从环境变量或 `config.yaml` 读,代码里只有变量名。

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

**GoalContract 字段 vs prd.json 字段** —— v1.0 不再用 GoalContract / draft_contract。Hermes `/goal` 引擎在 v1.0 完全不参与。Ralph 只用 prd.json 的字段:

| 概念 | prd.json 对位 | 谁维护 |
|---|---|---|
| 高层目标 | `title` + `description` | 老板写 |
| "什么叫 done" | `acceptanceCriteria[]` | 老板写 |
| 完成协议 | `passes: true` 全部为 true + worker echo `<promise>COMPLETE</promise>` | orchestrator 维护 |
| 边界条件 | (写到 worker prompt) | adapter._render_worker_prompt |
| 跨 story 排序 | `priority` | orchestrator 维护 |
| 依赖 | `dependsOn` / `depends_on` | orchestrator 维护 |
| 文件重叠(供 auto mode) | `files` | 老板写或 recon 自动发现(v1.0 没用 recon,只能手写) |
| 手工分组(manual mode) | `parallelGroup` | 老板写 |

## Monitoring & Kill Behavior

```bash
# 实时进度
cat prd.json | jq '.userStories[] | {id, priority, passes}'
cat progress.txt

# 暂停 / 恢复
# Ctrl-C / SIGTERM 优雅停止。下次同 --prd 重跑,agent 自动从磁盘加载历史继续
# kill -9 不保证 history 写入,但 prd.json 本身是持久的,下次重跑从当前状态继续

# 中途看 cost — HermesAdapter 记录 tokens in/out in history,第三方工具解析
hermes usage             # 累计 cost(如果 Hermes 配置了用量追踪)
```

**kill 触发条件**(orchestrator 自动判):
- `max_iterations` 到(默认 10 轮) → `MAX_ITERATIONS`(exit 2)
- `cost_cap_usd` 到(默认 5.0) → `COST_CAP`(exit 3)
- 全部 `passes: true`(正常完成) → `ALL_PASSES`(exit 0)
- `<promise>COMPLETE</promise>` 出现在 worker 输出 → `ALL_PASSES`(exit 0)
- 无故事可跑且无活锁 → `NO_PROGRESS`(exit 10)或 `STORIES_EXHAUSTED`(exit 11)
- adapter.run_story 抛异常 → `DELEGATION_FAILED`(exit 6)
- 未捕获异常 → `INTERNAL_ERROR`(exit 8)

## Cost & Iteration Caps

| 参数 | 默认 | 硬卡? | 越界行为 |
|---|---|---|---|
| `max_iterations` | 10 | 硬卡 | 立即 abort,留 progress.txt 痕迹 |
| `max_budget_usd` | 5.0 | 硬卡 | 立即 abort,留 progress.txt 痕迹 |

## Memory & Cache Invariants

v1.0 架构与 v0.4 不同:没有 `delegate_task` fan-out,只有一个 agent 实例。

| 不变量 | 怎么保证 |
|---|---|
| **orchestrator agent 的 prompt-cache 前缀跨 iteration 稳定** | RalphGoalLoop 用同一个 `AIAgent` 实例跑完整个 run;provider 端 prefix cache 自动复用 |
| **worker 的 tool call history 对 orchestrator 可见** | `HermesAdapter` 把 `run_conversation` 的每次 user/assistant 交互 append 到 `_history`,下一轮 `run_orchestrator_turn` 时传相同的 agent,所以 worker 的 context 累积对 orchestrator 可见 |
| **prd.json / progress.txt 持久化** | worker 直接读写磁盘,无中间状态;进程重启后 agent 从 `~/.hermes/sessions/<session_id>/conversation_history.json` 恢复历史 |

## `<promise>COMPLETE</promise>` Protocol

worker 实施完所有 story 后,**必须**在 response 末尾 echo literal token `<promise>COMPLETE</promise>`。这是唯一退出信号——orchestrator 用 `detect_promise()` 检查 worker 输出,没有 judge LLM 软判定。

**未触发 → orchestrator 继续 retry,直到 `max_iterations` 卡死。**

## Pitfalls

1. **worker 漏写 `<promise>COMPLETE</promise>`** — 主循环死锁;**mitigation**: worker prompt 模板强制要求"完成后 MUST echo 此 token"
2. **并行要么不触发、要么撞文件;卡住的那个还堵死后面全部** — 旧 `min(priority)` 分组让一个跑不通的 story 空转整个 prd;**mitigation**: `--parallel-mode auto` 按依赖+文件重叠分组,失败满 `--max-story-attempts` 次后 bench,不再堵路。见 §Parallelism。
3. **`max_budget_usd` 没硬卡** — 单 loop 没界,cost spike 跑飞;**mitigation**: orchestrator 启动时校验,超出立即 abort
4. **prd.json 在 dirty 主 checkout 里改** — 改完跟主 branch 混在一起,cherry-pick / revert 麻烦;**mitigation**: 启动时检测 git worktree
5. **跨 iteration 历史膨胀** — agent history 无限增长,provider 端 prompt cache 失效;**mitigation**: `HermesAdapter.save_history()` 只在每轮结束后写盘,历史积累是正常的——但如果 run 很长,考虑重启重跑

## Verification Checklist

完成本 skill 落地后,声明"完成"前逐项过:

- [ ] `python scripts/ralph.py --help` 显示正确帮助(含 `--prd` / `--project-root` / `--parallel-mode` / `--max-parallel` / `--max-story-attempts`)
- [ ] **并行真的发生了** — 跑一个同 priority 或文件不重叠的多 story prd,`progress.txt` 里应出现 `round 1: mode=auto batch=N`(N>=2)。若一直 `batch=1`,查 story 之间是否文件重叠
- [ ] **依赖被尊重** — 给一个 story 写 `dependsOn`,确认它在依赖 `passes: true` 之前不进任何一批
- [ ] **死锁/坏死都不空转** — 把某个 `dependsOn` 写成不存在的 id → 立刻 `STORIES_EXHAUSTED`(exit 11)并点名那个 id;把两个 story 的 `dependsOn` 互指成环 → 立刻 `NO_PROGRESS`(exit 10)。两者都不跑满 `max_iterations`
- [ ] **卡住的 story 不拖累别的** — 故意写一个永远过不了的 story(比如验收标准是 `assert 1==2`)放在 priority 1,确认:它重试到 `--max-story-attempts` 后被 BENCH,`progress.txt` 出现 `BENCHED <id>`,**且后面 priority 的 story 照样跑完**;整个 run 不是空转到 `max_iterations`
- [ ] **priority 不再拦路** — 一个 priority 1 的 story 还没过时,priority 2 的 story 只要依赖满足、文件不冲突就应能进同一批(auto 模式)
- [ ] 终止协议生效 — worker 输出含 `<promise>COMPLETE</promise>` → orchestrator grep 命中 → 退
- [ ] `python -m py_compile scripts/core/*.py scripts/adapters/*.py scripts/ralph.py` 全通过
- [ ] `python scripts/tests/test_starvation.py` 通过
- [ ] `python scripts/tests/test_parallel.py` 通过
- [ ] negative test:故意写 1 个 story 的 acceptance criteria 错(比如 `assert 1==2`),验证子 agent 标 `passes=false` 不退出,主 skill 进入下一轮 retry 直到 max_iterations

## References

- `CLAUDE.md.tmpl`(本 skill 内) — **历史参考**,运行时不用;worker prompt 由 `HermesAdapter._render_worker_prompt()` 动态生成
- `~/.hermes/skills/kanban-codex-lane/SKILL.md` — 同思路("外部 CLI 当 implementation lane"对位)
- `mikeyobrien/ralph` 的 `CLAUDE.md` + `prd.json.example`(外系,作为 prd schema 的事实标准,本 skill 直接对齐)

## Out of scope(不在范围内)

- ❌ 跑 mikeyobrien/ralph 原版 bash 循环(本 skill 是自给自足版,不需要)
- ❌ 跨 profile 跑(本 skill 只在自己 profile 跑;跨 profile 走 Kanban)
- ❌ 跨机器并行(走 Kanban + 多 profile gateway,不是 ralph-goal-loop)
- ❌ 修改 `hermes-agent/` 核心任何文件(本 skill 纯新增)
- ❌ 替代 Hermes `/goal` slash command(本 skill 不依赖 `/goal`,也不做它的功能——multi-story 编排与单-goal 反复跑是不同问题)
- ❌ 替代 Kanban / openclaw-plugin-author-suite(那些是不同形状的工具)
- ❌ 实装 git worktree 自动创建(老板使用时手动 `git worktree add`,本 skill 启动时**检测**在 worktree 内但不自动创建)

## Not verified(诚实交代)

- ✅ v1.0 end-to-end smoke test 在 `examples/three-stories/` 实跑过(2026-09-18):3 story, exit 0, ALL_PASSES, ~2 分钟;产出 hello.py / goodbye.py / status.py 都符合 acceptance criteria。
- ✅ 39/39 单元测试在 ralph/v1.0-rewrite 分支 HEAD (44b0bf2) 通过:`scripts/tests/test_starvation.py` 15/15, `scripts/tests/test_parallel.py` 24/24。
- ✅ HermesAdapter 真接 `AIAgent.run_conversation()` 跑过(2026-09-18 在 examples/three-stories/),worker agent 通过 Read/Write/Bash 工具识别任务,自己写 prd.json 的 passes 字段。
- ✅ `AIAgent(cwd=project_root)` 是 worker agent 拿到正确文件路径的关键(commit 44b0bf2 修了这个 bug)。
- 0 OpenCLAWAdapter stub 还未实装(本 PRD 范围外;见 tasks/prd-ralph-goal-loop-v1.md US-013)。
- 0 多 batch 真实跑动(目前 e2e 是 1 batch 跑完,没有验证 learnings 跨批滚雪球效果)。
- 0 与上游 `mikeyobrien/ralph` 在同一 prd 上的 end-to-end 对比测试(per PRD US-015)。
- 0 真-cost 估算:成本仍用模块内硬编码 `_WORKER_IN_USD_PER_1K = 0.003` / `_WORKER_OUT_USD_PER_1K = 0.015`;换模型后 `--cost-cap` 会算不准。
- 0 `resolve_runtime_provider()` ladder 端到端:信任 `hermes chat` 现成行为,ralph 端单独没重测;未来 ladder 上游变了需重新跑测试。
