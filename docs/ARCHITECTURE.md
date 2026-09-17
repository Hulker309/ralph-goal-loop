# ralph-goal-loop 架构设计

> 本文档是 `ralph-goal-loop` skill 的**架构级**说明,面向"为什么这样设计 / 各层边界 / 不变量 / 替代方案"的读者。
>
> 如果你只是想跑通一次,先看 [README.md](README.md);如果你想看完整 CLI 用法,看 [README.md §CLI 完整参数表](README.md)。

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

**优势**:简单、文件即状态、可观察。**缺**:每次 fork 进程破 prompt cache 边界、judge 只靠字符串 grep 无 LLM 软判定、cost cap 靠人工看 usage。

## 2. 为什么需要 2 层

ralph-goal-loop 不是简单地"用 Hermes 实现 bash 循环",而是把 Ralph 原版的**单一外层循环**拆成**两层**:

### 外层串行(继承 Ralph 原版)

- 读 prd.json → 挑最高 priority 的 pending story batch → fan-out → judge → continue / done
- 状态由 `GoalManager` 持久化(`state_meta` SQLite,key=`goal:<session_id>`)
- 终止由 `judge_goal()` LLM 软判定,**不是**字符串 grep

### 内层并行(原版没有,本项目的新增)

- `delegate_task(tasks=[…], parent_agent=self, background=False)` 走 batch mode
- `tools/delegate_tool.py:240` 硬编码 child `AIAgent(skip_memory=True)`,**进程内**起 `DaemonThreadPoolExecutor(max_workers=10)`
- 每个 child agent 在自己 thread 跑自己 story,共享 parent 的 prompt cache 边界

**为什么需要内层并行**:`mikeyobrien/ralph` 假设同一 priority 只有 1 个 story,所以不需要 fan-out。但实际工程里,priority 1 经常有 3-5 个独立 story(比如 priority 1 = "搭好 scaffold",priority 2 = "实现细节"各自 — 但 scaffold 内部也是多个独立子任务),串行等 30 秒/轮太慢。Hermes 的 `delegate_task` batch mode 天然支持,我们直接借。

**为什么 `delegate_task` 是同 turn 并发而非跨 turn**:`DaemonThreadPoolExecutor` 起 thread 后**不释放**主 agent turn,parent 进程阻塞 join 到所有 child 完成。语义上仍然是"1 turn 1 batch",但 batch 内多个 child 并发 = 同 turn 多 worker。

## 3. 本项目的设计决策

5 个关键设计决策,每个都是"考虑过其他选项后选了这个":

### 决策 1:借 Hermes `/goal` 作为 judge engine(而不是自己造一个 judge 循环)

**考虑过的替代**:自己写一个 LLM-as-judge 循环 + 简单 SQLite 持久化。

**为什么放弃**:`hermes_cli/goals.py` 已写完 1695 行 judge 引擎(state × judge × pause × resume × cost cap × transport/parse failure 兜底),`/goal` 自 v0.13.0 "Tenacity Release" 起就是 first-class primitive。重写这套 = 浪费 + 重复发明 + 失去 fail-OPEN 语义。

**最终方案**:`scripts/ralph.py` 只借 `GoalManager.set / pause / evaluate_after_turn` 三个入口,0 改 hermes-cli/goals.py。

### 决策 2:借 `delegate_task` batch 作为内层并行(而不是 subprocess + Claude Code CLI)

**考虑过的替代**:bash `claude --prompt-file prompt.md` 循环。

**为什么放弃**(per `references/two-layer-model.md` §4 B):fork 进程 → 破 prompt cache 边界 → 失去 Hermes cost 路径 → 无法联动 `/goal` state → 无法 fan-out children。Ralph 原版的优势(简单)在 Hermes 上变成劣势。

**最终方案**:`delegate_task(tasks=[N≥2], parent_agent=self)` 走 batch mode,内部 `DaemonThreadPoolExecutor(max_workers=10)` 自动 fan-out;`len(tasks)==1` 时自然 inline 不并发,避免无意义的 pool 开销。

### 决策 3:跳过 Idea → Contract 自动扩,老板自己写 prd.json

**考虑过的替代**:走 `draft_contract(objective)` 让 auxiliary LLM 自动把"实现 X"扩成完整 GoalContract,再 orchestrator 拆成 userStories。

**为什么放弃**:`draft_contract()` 在 `hermes_cli/goals.py:1008`,但**没有 prd.json 输出的 helper**。要套 Ralph schema 还得 orchestrator 自己再写拆解逻辑,自动扩出来的 prd.json 经常要么粒度不对(1 句话压成 1 个 story 没意义拆)要么粒度过细(每行代码 1 story)。

**最终方案**:**老板自己写 prd.json**(可以用 `references/` 下的 fixture 模板),orchestrator 启动时只校验 schema(`_check_file_overlap` 简陋 warn)。跳过"自动扩契约"这一步。

### 决策 4:中间 pause 节点(老板中间 review prd.json)

**考虑过的替代**:orchestrator 跑完不管,老板看 `prd.json` 文件再决定。

**为什么放弃**:Ralph 原版就是这样,问题在于 orchestrator 跑起来了停不下来,老板想改 prd.json 已经过了这一轮。

**最终方案**(per `SKILL.md §Launching the Loop`):2 个 review 节点:
1. **节点 1**:`draft_contract()` 输出 GoalContract 后,orchestrator 显示给老板 → 老板 `/goal /master show` 改 → `/goal resume`
2. **节点 2**:orchestrator 写完 prd.json 后,`goal_manager.pause("等待老板 review prd.json (story 拆分结果)")` → 老板 `cat prd.json` 看 → `/goal resume`

**任一节点老板不满意,直接 `/goal clear` 全盘放弃**。v0.1.0 自动 resume 保持脚本可跑通;interactive Hermes 会话可拦截 `REVIEW_NEEDED:` marker(per `scripts/ralph.py:61`)手动决定。

### 决策 5:judge model 默认走主对话,允许 `auxiliary.goal_judge` 路由覆盖

**考虑过的替代**:judge LLM 强制走 `config.yaml::auxiliary.goal_judge.*`,跟 worker model 隔离。

**为什么放弃**:`hermes_cli/goals.py:848` `_call_goal_judge_llm()` 默认就 fallthrough 到主对话模型 credentials(per `agent/auxiliary_client.py`),而且 v0.2 实战发现 `auxiliary.goal_judge.*` 配错会导致 judge POST 到 `/chat/completions` 但 MiniMax base_url 后缀是 `/anthropic` → 404(v0.3 已修,见 `SKILL.md §Pitfalls #10`)。

**最终方案**(per `references/judge-model.md`):
- **默认**:`scripts/ralph.py:234-265` `_init_parent_agent` 调 `hermes_cli.runtime_provider.resolve_runtime_provider()` 拿完整 5 元组(`provider/model/base_url/api_key/api_mode`)→ 构造 `AIAgent(...)` → judge 继承 worker 的完整 runtime
- **覆盖**:CLI `--judge-{provider,model,base-url,api-key-env}` 任一 flag 显式覆盖对应字段
- **反向**:CLI `--judge-keep-aux-config` 锁回老 `auxiliary.goal_judge.*` 路由

## 4. 架构图

```
                        ┌────────────────────────────────────────────┐
                        │          Hermes Agent 进程 (单进程)         │
                        │                                            │
   boss (human)         │  ┌──────────────────────────────────────┐  │
   ─────────────► /goal │  │  scripts/ralph.py orchestrator        │  │
        │  set / draft  │  │  ┌──────────────────────────────────┐ │  │
        │               │  │  │ outer loop (max_iter 轮)         │ │  │
        │               │  │  │  1. read prd.json               │ │  │
        │               │  │  │  2. pick top-priority batch     │ │  │
        │               │  │  │  3. delegate_task fan-out       │ │  │
        │               │  │  │  4. aggregate summaries         │ │  │
        │               │  │  │  5. goal_manager.evaluate       │ │  │
        │               │  │  │  6. check continue / cost cap   │ │  │
        │               │  │  └──────────────────────────────────┘ │  │
        │               │  └────────────┬─────────────────────────┘  │
        │               │               │                            │
        │               │               ▼                            │
        │               │  ┌────────────────────────────────────┐   │
        │               │  │  hermes_cli/goals.py (1695 行)      │   │
        │               │  │  GoalManager.set / pause / resume   │   │
        │               │  │  evaluate_after_turn ─► judge_goal  │   │
        │               │  │  state_meta SQLite 持久化            │   │
        │               │  └────────────┬───────────────────────┘   │
        │               │               │                            │
        │               │               ▼                            │
        │               │  ┌────────────────────────────────────┐   │
        │               │  │  tools/delegate_task.py            │   │
        │               │  │  delegate_task(tasks=[N≥2])        │   │
        │               │  │   ├─ DaemonThreadPoolExecutor(N)   │   │
        │               │  │   │   ├─ child AIAgent US-1       │   │
        │               │  │   │   ├─ child AIAgent US-2       │   │
        │               │  │   │   └─ child AIAgent US-3       │   │
        │               │  │   └─ aggregate results → json     │   │
        │               │  └────────────┬───────────────────────┘   │
        │               │               │                            │
        │               │               ▼                            │
        │               │       prd.json + progress.txt              │
        │               │       (workers 写 passes + progress 段)    │
        │               │                                            │
        │               │  judge LLM call (继承 worker endpoint)    │
        │               │   ├─ default: 主对话 model                  │
        │               │   ├─ CLI --judge-* override                │
        │               │   └─ --judge-keep-aux-config → 老路由       │
        └───────────────┴────────────────────────────────────────────┘
```

**数据流(单 round)**:

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
  │     每个 child: 读 prd.json → 读 progress.txt 顶部 Codebase Patterns
  │               → 实施自己 story → 标 passes=true
  │               → append progress.txt ## [Story ID]
  │               → echo "<promise>COMPLETE</promise>" if 全 passes
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

## 5. 决策时间线

| 时间 | 事件 | 决策 |
|---|---|---|
| 2026-09-17 早 | 老板决定 port Ralph 到 Hermes | 拒绝 bash 循环(fork 进程破 cache),决定走 `/goal` + `delegate_task` 双层 |
| 2026-09-17 中 | 可行性报告 `~/.hermes/cache/hermes-ralph-feasibility-2026-09-17.md` | 确认 `/goal` v0.13.0+ 是 first-class primitive,`delegate_task` batch mode 在 `tools/async_delegation.py:573` 有原生支持 |
| 2026-09-17 下午 | plan 文档 `~/.hermes/plans/hermes-ralph-skill-port-2026-09-17.md` | Phase 1-4 拆解:Phase 1 fixtures → Phase 2 orchestrator → Phase 3 e2e test → Phase 4 v1.0 docs |
| 2026-09-17 晚 | v0.1.0 skill 落地 | `scripts/ralph.py` 1 文件 + `SKILL.md` + 3 references,实跑 `hermes-ralph-goal-loop-test/` 3-story prd 通过 |
| 2026-09-17 深夜 | v0.2 实战发现 judge 路由 bug | `_call_goal_judge_llm()` 走 broken runtime → POST `/chat/completions` 404 |
| 2026-09-17 末 | v0.3 fix | `scripts/ralph.py:234-265` `_init_parent_agent` 调 `resolve_runtime_provider()`,judge 继承 parent 的 5 元组,fix 验证通过 |
| 2026-09-17 此刻 | main 分支文档 release | README.md(中文) + README.en.md(英文) + docs/ARCHITECTURE.md(本文件) |

## 6. 不变量

8 条硬不变量,任何一条被破 = skill 行为异常(per `SKILL.md §Memory & Cache Invariants`):

1. **子 agent 永远不写 parent memory** — `delegate_task` 在 `tools/delegate_tool.py:240` 硬编码 child `AIAgent(..., skip_memory=True, ...)`。orchestrator 入口**不传** `skip_memory`(传了 `False` 会被忽略,无此 kwarg)。
2. **主 skill 自身正常写 memory** — orchestrator 构造 parent agent 时不传 `skip_memory=True`(per `agent/agent_init.py:2211` `AIAgent.__init__(... skip_memory: bool = False, ...)`)。
3. **不破 prompt cache** — orchestrator 在同一进程反复调 `delegate_task`,system prompt prefix 不变(provider 端 prefix cache 自动复用);子 agent 是 fresh conversation,parent 完全看不到 child 内部 context(per `tools/delegate_tool.py:5-11`)。
4. **`DELEGATE_BLOCKED_TOOLS` 不动** — `terminal` / `file` / `edit` / `write` 全 OPEN(per `tools/delegate_tool_toolsets.py:14` 定义的 `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, cronjob_manage}`),子 agent 改 prd.json + 写 progress.txt 无阻碍。
5. **子 agent 不递归调 `delegate_task`** — `DELEGATE_BLOCKED_TOOLS` 包含 `delegate_task` 本身,nesting 边界 hard-capped(per `tools/delegate_tool.py:449-456` depth check)。
6. **`GoalManager` 不读 prd.json** — `GoalState`(per `hermes_cli/goals.py:390`)**没有** `priority` 字段,**没有** `userStories` 列表,**没有** `passes: bool` 概念。本 skill orchestrator 维护 priority 编排,`/goal` 只管单 goal 的 judge + state。
7. **`prd.json` 是 orchestrator 写的,不是 `/goal` 写的** — `draft_contract()` 只输出 `GoalContract`(per `hermes_cli/goals.py:1008`),`GoalContract → prd.json userStories` 是 orchestrator 自己做的拆分。`GoalManager` 从来不直接读写 prd.json。
8. **`judge_goal` 不看 prd.json** — judge 看的 `last_response` 是 orchestrator aggregate 出来的 worker summaries,judge 不关心 prd.json 里 priority 1/2/3 怎么拆。互补非重叠。

## 7. 替代方案考虑过但没用

### A. 自己造 judge loop

写一个 LLM-as-judge 函数 + 简单 SQLite 持久化 + 自定义 cost cap。**不用**因为 `/goal` 已写完 1695 行同类代码 + 实战验证过,重写 = 浪费 + 失去 fail-OPEN 兜底 + 失去 `pause/resume/clear` host session 联动。

### B. 走 bash + Claude Code CLI 跑

`claude --prompt-file prompt.md` 循环 + grep `<promise>`。**不用**因为 fork 进程破 prompt cache 边界 + 失去 Hermes cost 路径 + 无法 fan-out children。Ralph 原版的优势(简单)在 Hermes 上变成劣势(per `SKILL.md §Pitfalls #7`)。

### C. 单 goal 不拆 prd

让 `/goal` 直接对整个 `objective="实现 X"` 反复 judge,不维护 prd.json。**不用**因为:1) 失去 story 编排粒度,1 个 acceptance criteria fail 不会 pinpoint 到哪个 story;2) 失去 priority 串行(priority 2 等 priority 1 全完成的语义);3) 失去 acceptanceCriteria 自检能力(worker 不知道具体 criterion)。

### D. 用 Kanban card with --goal

把每 story 当 1 个 Kanban card,每个 card 走 `/goal` 跑。**不用**因为:1) Kanban card 之间无 priority 依赖协调;2) Kanban 不支持同 priority 内 fan-out(Kanban worker 串行);3) Kanban 是跨 profile 跨机器工具,本 skill 是单 profile in-process 工具,定位不同(per `SKILL.md §Out of scope`)。

## 8. 参考资料

- **Hermes `/goal` 文档**:`hermes_cli/goals.py:280-1695`(`GoalContract` / `GoalManager` / `judge_goal` / `draft_contract`)+ Hermes Agent 官方文档
- **Ralph 原版仓库**:[mikeyobrien/ralph](https://github.com/mikeyobrien/ralph) — `prd.json.example` + `CLAUDE.md` + `prompt.md` 的事实标准
- **OpenClaw 社区移植**:`~/.hermes/skills/openclaw-plugin-author-suite/references/ralph-integration.md` — Ralph 集成进 Hermes skill 的"金矿"判断(为什么用 `/goal` 而不是新写 ralph-loop)
- **kanban-codex-lane skill**:`~/.hermes/skills/kanban-codex-lane/SKILL.md` — 同思路("外部 CLI 当 implementation lane"对位,本 skill 是它的"自给自足版"——`delegate_task` 替代 Codex CLI)
- **openclaw-plugin-author-suite**:`~/.hermes/skills/openclaw-plugin-author-suite/SKILL.md` — 主 skill,本 skill 跟它共享 Ralph 集成层
- **可行性报告**:`~/.hermes/cache/hermes-ralph-feasibility-2026-09-17.md` — 本 skill 落地的依据 + 两层并行模型分析 + 三方案对比
- **实施 plan**:`~/.hermes/plans/hermes-ralph-skill-port-2026-09-17.md` — Phase 1-4 拆解 + 验证清单

---

*本文档写于 2026-09-17,ralph-goal-loop v0.3 / Hermes Agent v0.21.3 配套。后续 judge 路由 / fan-out 策略 / pause 节点等若有变化,以 [SKILL.md](../SKILL.md) + `references/` 为准。*