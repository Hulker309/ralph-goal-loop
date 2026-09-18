# ralph-goal-loop 架构设计

> 本文档是 `ralph-goal-loop` skill 的**架构级**说明,面向"为什么这样设计 / 各层边界 / 不变量 / 替代方案"的读者。
>
> 如果你只是想跑通一次,先看 [README.md](../README.md)。

## 1. 背景 — Ralph Wiggum 技术原始设计

Ralph Wiggum 技术由 **Geoffrey Huntley** 提出,在 [mikeyobrien/ralph](https://github.com/mikeyobrien/ralph) 仓库有完整 bash 实现。其核心思想是:**"把 AI 当 6 岁小孩,你得反复告诉它同样的事,直到它做对为止"**。

原版的 6 步机制:

1. **写 prd.json** — 把大目标拆成多个 `userStory`,每 story 有 `id / title / priority / acceptanceCriteria / passes: false`
2. **写 CLAUDE.md** — worker 提示词,告诉 Claude Code 当前 story 范围 + 完成协议(`<promise>COMPLETE</promise>`)
3. **写 progress.txt** — 跨 story 的上下文日志
4. **bash 循环** — `for i in $(seq 1 $MAX_ITERATIONS)`,每轮:
   - fork Claude Code 进程
   - 让它读 prd.json + progress.txt,挑最高 priority 的 pending story 实施
   - 实施完 echo `<promise>COMPLETE</promise>` → 改 `passes: true`
   - grep `<promise>` 字符串,命中 → `exit 0`
5. **重复** — 直到全部 `passes: true` 或 `MAX_ITERATIONS` 到
6. **可暂停** — 中途想 review 就 `Ctrl-C` 改 prd.json 再重启

**优势**:简单、文件即状态、可观察。**缺**:每次 fork 进程破 prompt cache 边界。

## 2. 为什么需要 2 层

v1.0 的架构是 **RalphCore(pure Python) + PlatformAdapter(Hermes 专用)**。这是因为:

1. Ralph 的核心逻辑(优先级排序、依赖图、失败下场)与平台无关,但 story 执行必须调用平台 API。把执行层抽象出来,RalphCore 可以单测纯 Python 逻辑而不调 LLM、未来支持 OpenCLAW 等其他平台而不改核心代码。
2. Hermes 是当前唯一实现的平台;把 Hermes 专用代码隔离在 adapter 里,未来可以加 OpenCLAWAdapter 而不碰核心。

### RalphCore(纯 Python,无 Hermes 依赖)

- `RalphGoalLoop` 类:`run()` / `_dead_stories()` / `_record_attempts()` / `_plan_batch()`
- 纯 Python,0 Hermes imports —— 可独立测试、可移植到其他平台
- 读 prd.json / progress.txt 做编排决策,调用 adapter 执行 story

### PlatformAdapter(Hermes 专用)

- `HermesAdapter` 实现 `run_story(story, context)` / `run_orchestrator_turn(prompt, history)` / `get_history()` / `save_history(history)`
- 唯一接触 `AIAgent` 的代码路径 —— 所有 Hermes 专用逻辑隔离在此
- `OpenCLAWAdapter` 是 TODO stub,可按同样接口实现

## 3. 设计决策

### 决策 1:RalphCore 是纯 Python;PlatformAdapter 是唯一 Hermes 专用代码

**背景**:v0.4 的 `scripts/ralph.py` 是 1154 行单体文件,核心编排逻辑和 Hermes API 调用混在一起。

**最终方案**:拆成三层:
- `scripts/core/` — `RalphGoalLoop` 类,纯 Python,0 Hermes imports
- `scripts/adapters/base.py` — `PlatformAdapter` 抽象接口
- `scripts/adapters/hermes.py` — `HermesAdapter` 实现,唯一接触 `AIAgent` 的地方

好处:编排算法可独立单测(`test_starvation.py` / `test_parallel.py`)、可移植到 OpenCLAW 等其他平台。

### 决策 2:失败下场 + 死故事识别(而不是无限重试)

**背景**:v0.4 `min(priority)` 硬闸让一个跑不通的 story 变成一堵墙 —— 它永远 `passes: false`,所以永远是最小 priority,所以后面每一层永远轮不到,run 一路空转到 `max_iterations`。

**最终方案**,拆成三条互不重叠的职责:

| 维度 | 谁负责 |
|---|---|
| **能不能跑** | `dependsOn` —— 唯一真正的约束 |
| **先跑哪个** | `priority` —— 只是排序偏好,**不拦路** |
| **跑不通怎么办** | `--max-story-attempts`(默认 3)—— 计数,满则 bench |

**死故事识别**:`_dead_stories()` 算出"永远跑不了"的集合,按**传递闭包**传播。

**终止状态拆成两个**:
- `NO_PROGRESS`(exit 10)—— **活死锁**:story 都在但当下没一个可跑(典型是依赖成环)
- `STORIES_EXHAUSTED`(exit 11)—— **死故事**:剩下的已不可能跑完,**点名谁死了、为什么**

`--max-story-attempts 0` = 关闭下场机制、永远重试。

### 决策 3:不硬编码模型

**背景**:v0.2 写死了一组 provider/model 兜底值,当 config 里挂着已失效的 provider 时,loop 永远走那条死路。

**最终方案**:解析顺序只有两条:
```
--worker-provider / --worker-model     <- 显式覆盖,只对本次 run 生效
        ↓ 没有的话
config.yaml::model.{provider,default}  <- 跟 `hermes chat` 用同一份配置
        ↓ 还是没有
交给 Hermes 自己的 provider ladder
```

### 决策 4:仓库里不留任何密钥

**最终方案**:所有凭证从环境变量或 `config.yaml` 读,代码里只有变量名。

## 4. 架构图

```
                        ┌────────────────────────────────────────────┐
                        │          Python 进程 (ralph-goal-loop)      │
                        │                                            │
   boss (human)         │  ┌──────────────────────────────────────┐  │
   ────────────────────►│  │  scripts/ralph.py (entry point)       │  │
                        │  │  ├─ argparse + env setup             │  │
                        │  │  └─ instantiate RalphGoalLoop        │  │
                        │  └────────────┬─────────────────────────┘  │
                        │               │                            │
                        │               ▼                            │
                        │  ┌────────────────────────────────────┐   │
                        │  │  RalphGoalLoop (orchestrator)       │   │
                        │  │  scripts/core/orchestrator.py       │   │
                        │  │  pure Python, no Hermes imports     │   │
                        │  │  ├─ run() — outer iteration loop   │   │
                        │  │  ├─ _plan_batch()                  │   │
                        │  │  ├─ _dead_stories()                │   │
                        │  │  └─ _record_attempts()             │   │
                        │  └────────────┬─────────────────────────┘   │
                        │               │                            │
                        │               ▼                            │
                        │  ┌────────────────────────────────────┐   │
                        │  │  PlatformAdapter (abstract)         │   │
                        │  │  scripts/adapters/base.py           │   │
                        │  └────────────┬─────────────────────────┘   │
                        │               │                            │
                        │     ┌─────────┴──────────┐                 │
                        │     ▼                     ▼                 │
                        │  ┌─────────────┐  ┌────────────────┐       │
                        │  │HermesAdapter│  │OpenCLAWAdapter │       │
                        │  │(concrete)   │  │(TODO stub)     │       │
                        │  └──────┬──────┘  └───────┬────────┘       │
                        │         │                │                 │
                        │         ▼                │                 │
                        │  ┌─────────────────┐     │                 │
                        │  │  AIAgent        │     │                 │
                        │  │  (single instance, shared session)     │
                        │  └────────┬────────┘     │                 │
                        │           │              │                 │
                        │           ▼              │                 │
                        │     prd.json + progress.txt (workers write)│
                        │                                            │
                        └────────────────────────────────────────────┘
```

**数据流(单 round)**:

```
Run start
  │
  ├─ RalphGoalLoop.run() — pure Python orchestrator
  │
  ├─ orchestrator: read prd.json
  │     pending   = filter(userStories where !passes)
  │     dead      = _dead_stories(...)     # bench 过的 + dependsOn 坏掉的
  │     runnable  = pending - dead
  │     if !runnable → return "STORIES_EXHAUSTED"   (exit 11)
  │     batch, reason = _plan_batch(runnable, passed_ids)
  │         off      : 1 个（最低 priority）
  │         priority : priority 数值相同的一批
  │         auto     : dependsOn 满足 + 文件不重叠（跨 priority）
  │         manual   : 按 story.parallelGroup 标签
  │     if !batch → return "NO_PROGRESS"            (exit 10，活死锁)
  │
  ├─ orchestrator: adapter.run_story(story, context)  — for each story in batch
  │     HermesAdapter.builds worker prompt internally
  │     worker: 读真代码 → 实施 story → 标 passes=true
  │           → append progress.txt `## [Story ID]`
  │           → echo "<promise>COMPLETE</promise>" if all passes
  │
  ├─ orchestrator: detect_promise() checks worker summary for <promise>
  │
  ├─ orchestrator: _record_attempts(batch)   # 没变绿的记一次，满 cap 则 bench
  │
  └─ next round 或 return "ALL_PASSES" / "MAX_ITERATIONS" / "COST_CAP"
                                      / "NO_PROGRESS" / "STORIES_EXHAUSTED"
```

## 5. Dropped Mechanisms (v1.0)

v1.0 删除了 v0.4 的以下机制:

### Hermes `/goal` integration (GoalManager, judge_goal, evaluate_after_turn)

**为什么删除**:上游 Ralph 是 bash 循环 + 字符串 grep,不借 judge LLM。v0.4 借 `/goal` 是为了"软判定",但引入了大量复杂度(`GoalManager` 状态机、`draft_contract`、judge 路由、auxiliary.goal_judge 配置)。

**v1.0 方案**:只用一个 `AIAgent` 实例跑所有 story,完成协议是 `<promise>COMPLETE</promise>` 字符串 grep,没有 judge LLM。Worker prompt 要求完成后必须 echo 该 token。

### Recon pass (_step_recon, _render_recon_prompt, _parse_recon_block)

**为什么删除**:recon 是 v0.2 为了解决"委派出去的 worker 失去项目上下文"而加的接地机制。但它引入了额外的 LLM 调用、recon block 解析、`## Grounding` section、`--no-recon` / `--recon-group` CLI flags。v1.0 的设计更贴近上游 Ralph:worker 自己读真实代码库,不需要预先接地。

**v1.0 方案**:worker prompt 里写明"读 `progress.txt` 的 `## Codebase Patterns` 节,读你要改的文件,再动手"。不单独跑 recon pass。

### delegate_task fan-out

**为什么删除**:v0.4 用 `delegate_task(tasks=[...])` batch mode 做同 priority 并行 fan-out。但上游 Ralph 是严格串行的(每轮一个 story);v0.3 的并行是后来加的,不是上游的设计。`delegate_task` 引入了 `parent_agent`/`skip_memory`/`DELEGATE_BLOCKED_TOOLS` 等复杂不变量。

**v1.0 方案**:批次的概念保留(`_plan_batch` 计算哪些 story 可以一起跑),但执行是严格串行的(`adapter.run_story()` 顺序调用,没有 fan-out)。并行仍然有意义——分组策略帮助识别哪些 story 可以安全地并行跑。

### Boss review pause/resume nodes

**为什么删除**:`_step_review_prd` + `goal_manager.pause("boss-review-prd")` + auto-resume 是 v0.1.0 留下的"半自动"停点。实际不是阻塞门。

**v1.0 方案**:没有内置的 pause/resume 机制。想中途 review,直接看 `progress.txt` + `prd.json`,下次同 `--prd` 重跑即可。

## 6. Adapter Interface

```python
class PlatformAdapter(ABC):
    def run_story(self, story, context):
        # Args:
        #   story: a prd.json user story dict
        #   context: dict with prd_path, progress_path, project_root
        # Returns: dict with summary, tokens, tool_calls
        raise NotImplementedError

    def run_orchestrator_turn(self, prompt, history):
        raise NotImplementedError

    def get_history(self):
        raise NotImplementedError

    def save_history(self, history):
        # Best-effort: failures are logged but do not raise
        raise NotImplementedError
```

## 7. 不变量

1. **RalphCore 是纯 Python** — `scripts/core/` 没有任何 `from hermes_cli` 或 `from run_agent` 导入,可在无 Hermes 环境下单测。
2. **PlatformAdapter 是唯一 Hermes 接触点** — `scripts/adapters/hermes.py` 是唯一 `from run_agent import AIAgent` 的文件;其他模块调 adapter,不直接调 AIAgent。
3. **`<promise>COMPLETE</promise>` 是唯一退出信号** — 不依赖 judge LLM;worker 必须在 summary 里包含该 token 才算完成。
4. **priority 不是闸** — 它只决定先够哪个,不拦路;能不能跑只由 `dependsOn` 决定。
5. **每个有终态的路径都留痕** — `NO_PROGRESS` / `STORIES_EXHAUSTED` / `COST_CAP` / `MAX_ITERATIONS` 都往 `progress.txt` 写明原因。
6. **adapter.run_story 不做编排** — 编排决策(RalphCore 负责)与执行(HermesAdapter 负责)严格分离。
7. **orchestrator agent 的 prompt-cache 前缀跨 iteration 稳定** — RalphGoalLoop 用同一个 `AIAgent` 实例跑完整个 run;provider 端 prefix cache 自动复用。
8. **prd.json / progress.txt 持久化** — worker 直接读写磁盘,无中间状态;进程重启后 agent 从 `~/.hermes/sessions/<session_id>/conversation_history.json` 恢复历史。

## 8. 决策时间线

| 时间 | 事件 | 决策 |
|---|---|---|
| 2026-09-17 早 | 老板决定 port Ralph 到 Hermes | 拒绝 bash 循环(fork 进程破 cache),决定走 Hermes 进程内 |
| 2026-09-17 中 | 可行性报告 | 确认 `/goal` v0.13.0+ 是 first-class primitive |
| v0.1.0 | 初始 skill 落地 | `scripts/ralph.py` 1 文件 + `SKILL.md` |
| v0.3.0 | 模型不硬编码;仓库清密钥;`--parallel-mode` | 分组策略可配 |
| v0.4.0 | 修优先级饥饿 | `priority` 从硬闸改排序偏好;`--max-story-attempts` 失败下场;`_dead_stories` 传递闭包 |
| v1.0 | 重构为 RalphCore + PlatformAdapter;删除 /goal、recon、delegate_task | 拆分 `scripts/ralph.py` → `scripts/core/` + `scripts/adapters/` + `scripts/ralph.py` |

## 9. 参考资料

- **Ralph 原版仓库**:[mikeyobrien/ralph](https://github.com/mikeyobrien/ralph) — `prd.json.example` + `CLAUDE.md` + `prompt.md` 的事实标准
- **Hermes Agent**:`~/.hermes/skills/hermes-agent/SKILL.md`

---

*本文档描述 ralph-goal-loop v1.0。*
