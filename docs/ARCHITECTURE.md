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

ralph-goal-loop 不是简单地"用 Hermes 实现 bash 循环",而是把 Ralph 原版的**单一循环**拆成**两层 + 一步补课**:

### 外层循环(继承 Ralph 原版)

- 读 prd.json → 挑这一轮的 batch → fan-out → judge → continue / done
- 状态由 `GoalManager` 持久化(`state_meta` SQLite,key=`goal:<session_id>`)
- 终止由 `judge_goal()` LLM 软判定,**不是**字符串 grep

### 内层并行(本项目新增,且**分组由调用方决定**)

- `delegate_task(tasks=[…], parent_agent=self, background=False)` 走 batch mode
- `tools/delegate_tool.py:240` 硬编码 child `AIAgent(skip_memory=True)`,**进程内**起 `DaemonThreadPoolExecutor(max_workers=10)`
- 每个 child agent 在自己 thread 跑自己 story,共享 parent 的 prompt cache 边界
- **分组策略**由 `--parallel-mode` 决定(`off` / `priority` / `auto` / `manual`),默认 `auto`

**为什么"分组"是个需要单独解决的问题**(v0.3/v0.4 才想清楚):`delegate_task` 只在 `len(tasks) ≥ 2` 时并发,所以**并行是分组的函数** —— 怎么把 pending story 划进同一批,直接决定并发生不发生。而 Ralph 原版**没有这个问题**:它严格串行、一轮一个 story,并发来自"那一轮内部 agent 自己开的多路"。移植过来后执行本身就是 fan-out,分组就必须被显式决定,否则默认行为会静默退化。

**踩过的坑**:最初写死 `min(priority)` 分组。上层 `ralph` skill 的编号规则是「每个 story 一个号(1,2,3…)」,所以每批恒等于 1 —— **并行一次都没触发**(peer-contract 那次 23 个 story 跑了 23 轮全串行)。更糟的是 `min(priority)` 同时是**硬闸**:低数值那层没过,后面永远轮不到,一个跑不通的 story 就能把整个 prd 空转到 `max_iterations`,预算全烧在重试它上。

**v0.4 的修正**:三条职责分开 —— `dependsOn` 管资格、`priority` 只管顺序(**不拦路**)、失败满 `--max-story-attempts` 次就 bench。见 §3 决策 6 / 决策 7。
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

**最终方案**(per `SKILL.md §Launching the Loop`):orchestrator 写一行 `REVIEW_NEEDED:` marker 进 `progress.txt`,然后**立即 pause + resume** —— 也就是**不阻塞**。老板想看就看 `prd.json` / `progress.txt`,不看也不会卡住流程。

**这符合本 skill 的设计意图**:质量由流程(recon + 可判定验收 + judge)保证,**人不是保险丝** —— 想看随时能看,不看也不塌。要真停下,把 `_step_review_prd` 里那行 `resume()` 去掉即可。

**任一时刻老板不满意,直接 `/goal clear` 全盘放弃**;orchestrator 每轮校验 `is_active()`,检测到即退(`GOAL_CLEARED`,exit 7)。

### 决策 5:judge model 默认走主对话,允许 `auxiliary.goal_judge` 路由覆盖

**考虑过的替代**:judge LLM 强制走 `config.yaml::auxiliary.goal_judge.*`,跟 worker model 隔离。

**为什么放弃**:`hermes_cli/goals.py:848` `_call_goal_judge_llm()` 默认就 fallthrough 到主对话模型 credentials(per `agent/auxiliary_client.py`),而且 v0.2 实战发现 `auxiliary.goal_judge.*` 配错会导致 judge POST 到 `/chat/completions` 但 MiniMax base_url 后缀是 `/anthropic` → 404(v0.3 已修,见 `SKILL.md §Pitfalls #10`)。

**最终方案**(per `references/judge-model.md`):
- **默认**:`scripts/ralph.py:234-265` `_init_parent_agent` 调 `hermes_cli.runtime_provider.resolve_runtime_provider()` 拿完整 5 元组(`provider/model/base_url/api_key/api_mode`)→ 构造 `AIAgent(...)` → judge 继承 worker 的完整 runtime
- **覆盖**:CLI `--judge-{provider,model,base-url,api-key-env}` 任一 flag 显式覆盖对应字段
- **反向**:CLI `--judge-keep-aux-config` 锁回老 `auxiliary.goal_judge.*` 路由

### 决策 6:recon —— 派活前先接地(而不是让 worker 自己猜接口)

**背景**:原版 Ralph 从来不"查接口",但它**不需要查** —— 迭代 agent 起在项目根目录里,写 story 时眼睛看得到真代码,实现时边做边读。**接地是它的载体属性,不是流程里的一步。** 一旦执行被委派给 fan-out worker,这个属性就消失:worker 只拿到 story 标题 + 验收标准 + 两个文件路径,关于"这个世界长什么样"的信息是**零**。

**考虑过的替代**:(a) 什么都不做 —— 让 worker 自己去读代码;(b) 把接口信息塞进 `CLAUDE.md` / worker prompt 模板。

**为什么放弃**:
- (a) 实证失败(peer-contract v0.4.1.0 US-008):worker 写了 391 行,**6 个编译错全部是接口级**(import 路径、instance method vs 顶层函数、工厂类型),业务逻辑错 0 个。而且那 5 个错**是在 worker 超时死掉之后才第一次被看见的** —— 它连 `node_modules` 都没装好,`tsc` 跑不了,自校验都做不到。这不是 worker 水平问题,是**输入里缺了东西**。
- (b) 更危险:那次 orchestrator 派活时手写了一段「**真签名**(我 commit 的, 不能 mental-model 推)」,5 条错 4 条 —— **用权威语气把猜测包装成事实,worker 反而不去怀疑了**。这是整个事故里最伤的一处。

**最终方案**:派活前插一步 **recon** —— 派只读 worker 去真实代码库,把每个 story 的 `acceptanceCriteria` 从"纸面期望句"重写成"可判定的验收 + 真实接口签名 + `path:line` 证据"。三条硬规则:(1) 每条结论必须带 `path:line` 证据,找不到写 `NOT FOUND`,不许编;(2) **我们的描述不是权威,代码才是**;(3) worker **只上报**,所有写入由 orchestrator 在整批返回后统一做 —— 并行 worker 因此不可能互相踩。

**失败不致命**:recon 拿不到东西就照原样跑,只在 `progress.txt` 留痕。补课环节不该变成新的故障点。

### 决策 7:失败下场 + 死故事识别(而不是无限重试)

**背景**:见 §2 的"踩过的坑"。`min(priority)` 硬闸让一个跑不通的 story 变成一堵墙 —— 它永远 `passes: false`,所以永远是最小 priority,所以后面每一层永远轮不到,run 一路空转到 `max_iterations`。

**考虑过的替代**:(a) 保持硬闸,失败就无限重试(原行为);(b) 失败即中止整个 run;(c) 失败 story 下场,其余继续。

**为什么放弃**:
- (a) 预算全烧在重试同一个 story 上,别的 story 一次都没试过 —— 这是实测到的真实代价。
- (b) 太粗暴。Ralph 的设计哲学是"别让局部失败阻塞好工作";一个 story 卡住不代表其它 story 不能推进。

**最终方案**(c),拆成三条互不重叠的职责:

| 维度 | 谁负责 |
|---|---|
| **能不能跑** | `dependsOn` —— 唯一真正的约束 |
| **先跑哪个** | `priority` —— 只是排序偏好,**不拦路** |
| **跑不通怎么办** | `--max-story-attempts`(默认 3)—— 计数,满则 bench |

**死故事识别**:`_dead_stories()` 算出"永远跑不了"的集合,按**传递闭包**传播 —— 被 bench 的、`dependsOn` 指向不存在 id 的、以及**等在死故事后面的**(第三种最容易被漏掉:不识别它,它就会以自己的方式永远重试下去)。

**终止状态拆成两个**(不再有光秃秃的 `MAX_ITERATIONS`,见 §6 不变量):
- `NO_PROGRESS`(exit 10)—— **活死锁**:story 都在但当下没一个可跑(典型是依赖成环)
- `STORIES_EXHAUSTED`(exit 11)—— **死故事**:剩下的已不可能跑完,**点名谁死了、为什么**

`--max-story-attempts 0` = 关闭下场机制、永远重试(旧行为,留给想要的人)。

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
```
Run start
  │
  ├─ Step 1-2: draft GoalContract + GoalManager.set
  │
  ├─ Step 3: read prd.json (orchestrator only normalises it; see 不变量 #7)
  │
  ├─ Step 4: ★ RECON ★  (orchestrator owns every write)
  │     tasks = [_render_recon_prompt(root, group) for group in groups(pending, size)]
  │     delegate_task(tasks=[…], parent_agent=self, background=False)
  │       each read-only recon worker: 读真代码 → 输出 <recon>{…}</recon> JSON
  │                                      （不写任何文件）
  │     orchestrator (整批返回后统一):
  │       parse <recon> → 重写 story.acceptanceCriteria + 挂 story.recon
  │       → 写回 prd.json + append progress.txt `## Grounding`
  │
  └─ Round N
       │
       ├─ orchestrator: read prd.json
       │     pending   = filter(userStories where !passes)
       │     dead      = _dead_stories(...)     # bench 过的 + dependsOn 坏掉的（传递闭包）
       │     runnable  = pending - dead
       │     if !runnable → return "STORIES_EXHAUSTED"   (exit 11)
       │     batch, reason = _plan_batch(runnable, passed_ids)   # ← 分组由 --parallel-mode 决定
       │         off      : 1 个（最低 priority）
       │         priority : priority 数值相同的一批
       │         auto     : dependsOn 满足 + 文件不重叠（跨 priority）
       │         manual   : 按 story.parallelGroup 标签
       │     if !batch → return "NO_PROGRESS"            (exit 10，活死锁)
       │
       ├─ orchestrator: delegate_task(tasks=[worker_prompt(s) for s in batch],
       │                              parent_agent=self, background=False)
       │     worker prompt 含: story 标题 + 验收标准 + `## Grounding`(该 story 的 recon 结论)
       │                      + `## Facts already established`(本 run 已积累的事实)
       │     每个 child: 读真代码 → 实施自己 story → 跑验收命令 → 标 passes=true
       │               → append progress.txt `## [Story ID]` + Pattern/Gotcha/File 三行
       │               → echo "<promise>COMPLETE</promise>" if 全 passes
       │
       ├─ delegate_task 返回: json {"results":[{task_index, status, summary,
       │                                          exit_reason, tokens, ...}, ...]}
       │
       ├─ orchestrator: _record_attempts(batch)   # 没变绿的记一次，满 cap 则 bench
       │
       ├─ orchestrator: _merge_learnings(results) # 收集 Pattern/Gotcha/File → `## Codebase Patterns`
       │
       ├─ orchestrator: aggregate summaries → last_response
       │     goal_manager.evaluate_after_turn(last_response)
       │       → judge_goal(state.goal, last_response, contract=state.contract)
       │       → {"status", "should_continue", "continuation_prompt",
       │          "verdict", "reason", "message"}
       │
       ├─ orchestrator: 检查 should_continue / cost_cap / max_iter
       │
       └─ next round 或 return "GOAL_DONE" / "ALL_PASSES" / "COST_CAP"
                                       / "MAX_ITERATIONS" / "NO_PROGRESS"
                                       / "STORIES_EXHAUSTED"
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
| 2026-09-17 晚(续) | **诊断:实跑效果不符**(peer-contract 跑出"报错→兜底→反复修") | 定位为**接地丢失**:原版靠"agent 坐在项目里"天然获得的接口知识,委派出去后没补回来。同时查出 `hermes ralph` 从未实现、`min(priority)` 让并行静默失效、多处文档描述与实现不符 |
| v0.3.0 | 补 recon + 回写闭环;文档与实现对齐 | 模型不再硬编码(去掉写死的 provider/model 兜底);仓库内清掉密钥;`--parallel-mode` 让分组可配;`.gitignore` 修好(原来换行是字面量,`.pyc` 因此被跟踪) |
| v0.4.0 | 修优先级饥饿 | `priority` 从硬闸改为排序偏好;`--max-story-attempts` 失败下场;`_dead_stories` 传递闭包;终止状态拆成 `NO_PROGRESS`(10) / `STORIES_EXHAUSTED`(11) |

## 6. 不变量

12 条硬不变量,任何一条被破 = skill 行为异常(1-8 见 `SKILL.md §Memory & Cache Invariants`,9-12 是本 skill 自己的编排不变量):

1. **子 agent 永远不写 parent memory** — `delegate_task` 在 `tools/delegate_tool.py:240` 硬编码 child `AIAgent(..., skip_memory=True, ...)`。orchestrator 入口**不传** `skip_memory`(传了 `False` 会被忽略,无此 kwarg)。
2. **主 skill 自身正常写 memory** — orchestrator 构造 parent agent 时不传 `skip_memory=True`(per `agent/agent_init.py:2211` `AIAgent.__init__(... skip_memory: bool = False, ...)`)。
3. **不破 prompt cache** — orchestrator 在同一进程反复调 `delegate_task`,system prompt prefix 不变(provider 端 prefix cache 自动复用);子 agent 是 fresh conversation,parent 完全看不到 child 内部 context(per `tools/delegate_tool.py:5-11`)。
4. **`DELEGATE_BLOCKED_TOOLS` 不动** — `terminal` / `file` / `edit` / `write` 全 OPEN(per `tools/delegate_tool_toolsets.py:14` 定义的 `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, cronjob_manage}`),子 agent 改 prd.json + 写 progress.txt 无阻碍。
5. **子 agent 不递归调 `delegate_task`** — `DELEGATE_BLOCKED_TOOLS` 包含 `delegate_task` 本身,nesting 边界 hard-capped(per `tools/delegate_tool.py:449-456` depth check)。
6. **`GoalManager` 不读 prd.json** — `GoalState`(per `hermes_cli/goals.py:390`)**没有** `priority` 字段,**没有** `userStories` 列表,**没有** `passes: bool` 概念。本 skill orchestrator 维护 priority 编排,`/goal` 只管单 goal 的 judge + state。
7. **`prd.json` 是输入,不是本 skill 的产物** — orchestrator 只做字段归一(`setdefault priority/id/passes`),**没有**「GoalContract → userStories」的拆解逻辑(早期文档里的 `expand_contract_to_prd` 从不存在)。`draft_contract()` 的产出只喂 `/goal` 状态机。把 PRD 正文拆成 story 是上游 `prd` + `ralph` 两个 skill 在 Claude Code 里干的事。`GoalManager` 从来不直接读写 prd.json。
8. **`judge_goal` 不看 prd.json** — judge 看的 `last_response` 是 orchestrator aggregate 出来的 worker summaries,judge 不关心 prd.json 里 priority 1/2/3 怎么拆。互补非重叠。
9. **写权只在 orchestrator** — 并行的 worker(recon 与执行)一律**只上报**,碰都不碰 `prd.json` / `progress.txt` 的共享段。所有写入发生在整批返回之后、由 orchestrator 单独做。这是并行安全的前提:两个 worker 同时写同一文件 = 静默损坏,没有报错。
10. **recon 失败不致命** — 拿不到结论就照原样跑(story 保持纸面验收标准),只在 `progress.txt` 留痕(`recon: ... proceeding ungrounded`)。补课环节不该变成新的故障点。
11. **priority 不是闸** — 它只决定先够哪个,不拦路;能不能跑只由 `dependsOn` 决定。把它当闸用就会重现优先级饥饿(见 §3 决策 7)。
12. **每个有终态的路径都留痕** — `NO_PROGRESS` / `STORIES_EXHAUSTED` / `COST_CAP` / `MAX_ITERATIONS` 都往 `progress.txt` 写明原因(谁死了、在等谁)。不允许出现跑完了但不知道为什么的终止。

## 7. 替代方案考虑过但没用

### A. 自己造 judge loop

写一个 LLM-as-judge 函数 + 简单 SQLite 持久化 + 自定义 cost cap。**不用**因为 `/goal` 已写完 1695 行同类代码 + 实战验证过,重写 = 浪费 + 失去 fail-OPEN 兜底 + 失去 `pause/resume/clear` host session 联动。

### B. 走 bash + Claude Code CLI 跑

`claude --prompt-file prompt.md` 循环 + grep `<promise>`。**不用**因为 fork 进程破 prompt cache 边界 + 失去 Hermes cost 路径 + 无法 fan-out children。Ralph 原版的优势(简单)在 Hermes 上变成劣势(per `SKILL.md §Pitfalls #7`)。

### C. 单 goal 不拆 prd

让 `/goal` 直接对整个 `objective="实现 X"` 反复 judge,不维护 prd.json。**不用**因为:1) 失去 story 编排粒度,1 个 acceptance criteria fail 不会 pinpoint 到哪个 story;2) 失去 priority 串行(priority 2 等 priority 1 全完成的语义);3) 失去 acceptanceCriteria 自检能力(worker 不知道具体 criterion)。

### D. 用 Kanban card with --goal

把每 story 当 1 个 Kanban card,每个 card 走 `/goal` 跑。**不用**因为:1) Kanban card 之间无 priority 依赖协调(也不认 `dependsOn`);2) Kanban worker 串行,没有本项目这层「按文件重叠/依赖自动分组」的并行 fan-out;3) Kanban 是跨 profile 跨机器工具,本 skill 是单 profile in-process 工具,定位不同(per `SKILL.md §Out of scope`)。

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