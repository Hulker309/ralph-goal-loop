---
name: ralph-goal-loop
description: "Use when you have a prd.json with multiple user stories and want a single Hermes process to implement them in priority order with parallel fan-out within each priority level. Wraps Hermes /goal (judge + state persistence) as the execution engine and delegate_task batch mode for parallel worker fan-out with a caller-chosen grouping strategy (--parallel-mode off|priority|auto|manual), plus a read-only **recon** pass that grounds each story's acceptance criteria against the real codebase before any worker runs. 0 external CLI, 100% Hermes in-process. Triggers on: 'run ralph', 'multi-story prd', 'goal + fan-out', 'prd.json 拆 story', 'per priority 跑'."
version: 0.3.0
author: Hermes Agent (希尔, 2026-09-17)
license: MIT
platforms: [linux, macos, windows]
changelog:
  - v0.3.0 — 三件事。① **并行真的能发生了**:批量分组从写死的 `min(priority)` 改成调用方可选的
    `--parallel-mode off|priority|auto|manual`,默认 `auto` 按 `dependsOn` + 文件重叠(用 recon 读到的
    文件)跨 priority 分组;文件未知的 story 单独跑,依赖不可满足时立刻返回 `NO_PROGRESS` 而不是空转。
    旧行为的问题:`min(priority)` 遇上上游 `ralph` skill 的编号习惯(每个 story 一个号)⇒ 批批只有 1 个
    story,并行从不触发。② **不再硬编码模型**:`_init_parent_agent` 去掉了写死的 provider/model 兜底,
    只认 `--worker-provider/--worker-model` 或 `config.yaml`,都没有就交给 Hermes 自己的 provider ladder。
    ③ **skill 里不留任何密钥**:仓库内无 secret(全库扫描),git remote URL 里内嵌的 token 已移除。
  - v0.2.0 — 补上 **recon(并行接地)** 与 **跨轮回写闭环**,并把文档与真实实现对齐。
    此前 SKILL.md 与 references/ 描述的是 v0.1.0 设计,其中有多个函数名(`expand_contract_to_prd` /
    `_step4_expand_to_prd` / `_check_file_overlap` 等)在 `scripts/ralph.py` 里并不存在;
    `hermes ralph` 这个入口从未实现(实现它需要改 hermes-agent 核心,与本 skill 的 0-modify 承诺矛盾);
    CLAUDE.md.tmpl 零引用(现标注为设计参考)。recon 起于真实事故:委派出去的 worker 拿不到既有接口
    信息,只能猜,猜错了在编译期爆掉再由 orchestrator 事后手修。
  - v0.1.0 — 初版:复刻 Ralph 的执行段(外层串行 + 同 priority 内 fan-out + judge + `<promise>` 协议)。
metadata:
  hermes:
    tags: [ralph, prd, goal, autonomous-agents, fan-out, delegate_task, multi-story]
    related_skills:
      - kanban-codex-lane
      - openclaw-plugin-author-suite
      - hermes-agent
---

# ralph-goal-loop

> **白话先**: 你有一个 `prd.json` 拆好的多 story 任务,想让它**在 Hermes 自己内部**跑完——按 priority 顺序、同 priority 内 fan-out 并行,直到全部 `passes: true` 才退。**不**调外部 Claude Code CLI,不走 bash 循环,直接用 Hermes 自己的 `/goal` 引擎(judge LLM + state 持久化)做"持续跑到目标完成",用 `delegate_task` batch mode 做"同 priority 内并行 fan-out"。**派活之前先做一步 recon(并行接地)**:派只读 worker 去真实代码库把每个 story 的验收标准摸成可判定的形式,免得执行者猜接口、跑完再返工(见 §Recon)。**这是 mikeyobrien/ralph 在 Hermes 内的复刻,外加一处补课;底层引擎是 Hermes 官方 `/goal` 而非自造**。

## Overview

`ralph-goal-loop` 是 `mikeyobrien/ralph` 的 Hermes-side 移植。它复用 4 个上游原语(都在 `hermes_cli/goals.py`):

| 原语 | file:line | 用途 |
|---|---|---|
| `GoalContract` | `hermes_cli/goals.py:280` | 想法 → 可验收契约的 Python 数据类 |
| `draft_contract(objective)` | `hermes_cli/goals.py:1008` | auxiliary LLM 自动把一句话扩成完整 GoalContract |
| `judge_goal(goal, last_response, ...)` | `hermes_cli/goals.py:864` | 单独调 judge,返回 `(verdict, reason, parse_failed, wait_directive, transport_failed)` |
| `GoalManager(session_id)` | `hermes_cli/goals.py:1053` | 状态机(set / pause / resume / clear / evaluate_after_turn / next_continuation_prompt)+ `state_meta` SQLite 持久化 |

`ralph-goal-loop` 在这 4 个原语之上做 **2 件事**:

1. 把"prd.json 多 story 编排 + 内层并行 fan-out"写成 orchestrator 脚本 `scripts/ralph.py`,用 `python scripts/ralph.py --prd <path>` 启动 —— **它不是 `hermes` 的子命令**(见 §Launching the Loop 的说明)。
2. 在派活之前插一步 **recon**:派只读 worker 去真实代码库,把每个 story 的 `acceptanceCriteria` 从"纸面期望句"重写成"可判定的验收 + 真实接口签名 + `path:line` 证据"。这一步是上游 Ralph 靠"迭代 agent 坐在项目里"天然获得的,一旦执行被委派出去就会消失,必须显式补回(见 §Recon)。

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
| **接地(recon)** | **隐式**:迭代 agent 坐在项目根目录,边做边读真实代码,接口不用猜 | **显式**:派活前 fan-out 只读 recon worker 读代码库,把真实签名 + `path:line` 证据写进 story 的验收标准(见 §Recon) |
| **外层串行** | bash `for i in $(seq 1 $MAX_ITERATIONS)`,每轮 fork Claude Code 进程 | **`GoalManager` 状态机**,`evaluate_after_turn` 跑 judge,judge 说 `done` → 退。**同进程** |
| **内层并行 fan-out** | Claude Code 进程内单 turn 多个 `Task` tool_use | **`delegate_task(tasks=[...])`** batch mode,内部 `DaemonThreadPoolExecutor(max_workers=10)`。**分组由调用方定**(`--parallel-mode`,默认 `auto` 按依赖+文件重叠判定),见 §Parallelism。recon 阶段复用同一套 fan-out |
| **完成协议** | grep `<promise>COMPLETE</promise>` in stdout | worker echo `<promise>` + judge LLM 双判定 |
| **状态持久化** | `prd.json` + `progress.txt` 磁盘 | `SessionDB.state_meta`(`goal:<session_id>`) + `prd.json` + `progress.txt` 磁盘 |
| **可暂停** | 重启脚本 | `/goal pause` / `/goal resume` / `/goal clear` 走 `GoalManager` |
| **跨轮记忆** | agent 每轮回写 `progress.txt` 的 `## Codebase Patterns` + 就近 `CLAUDE.md`,下一轮自动加载 | 每批结束后 orchestrator 汇总 worker 上报的 `Pattern:/Gotcha:/File:`,写进 `## Codebase Patterns`,并注入下一批的 worker prompt(见 §Recon 的"回写") |
| **想法→契约** | 人写 prd.json | `draft_contract()` **只喂 `/goal` 状态机,不产出 story**。prd.json 仍需先写好(自己写 / 抄模板 / 上游 `prd` + `ralph` 两个 skill 转换) |
| **用户中间 review** | 改 prd.json 文件 | `goal_manager.pause("等待老板 review prd.json")` + `/goal resume` |

**底层机制**:`delegate_task(tasks=[...])` 的 fan-out 跟 `delegate_task(goal=..., context=...)` 是同一个 tool 的不同 mode——传 `tasks=[N>=2]` 走 batch,`DaemonThreadPoolExecutor` 才开 pool;N=1 时**内部**退化成单 child 串行。

所以**并行是「分组」的函数**:怎么把 pending story 分进同一批,决定并发生不发生。见下节。

## Parallelism — 由调用方决定(`--parallel-mode`)

**背景**:上游 Ralph 是严格串行的(一轮一个 story),它的并发来自「那一轮内部 agent 自己开的多路」。移植到这里,执行本身就是一个 fan-out,**并行必须被决定**,而不是自动出现。

**谁决定**:跑这个 skill 的 agent(或人),用 `--parallel-mode` 决定;planner 只负责执行该策略。

| mode | 行为 | 什么时候用 |
|---|---|---|
| `off` | 一轮一个 story,严格串行 | 想要上游保真 / 排查问题时 |
| `priority` | `priority` 数值相同的进同一批 | 你自己按编号分组时(注意:上游 `ralph` skill 的编号习惯是每个 story 一个号 ⇒ 这个模式实际会退化成串行) |
| **`auto`(默认)** | 跨 priority 分组,依据 **`dependsOn` + 文件重叠** | 一般情况。**前提是 recon 跑过**(文件信息来自 recon) |
| `manual` | 按 story 上的 `parallelGroup` 标签分组 | 你要精确控制分组、不要任何推断时 |

**`auto` 的三条判定**:

1. **依赖优先** —— story 的 `dependsOn` 全部 `passes: true` 才算 eligible。`dependsOn` 可以写 camel 或 snake,值可以是数组或单个 id。
2. **文件不重叠才同批** —— 两个 story 的文件集合只要有交集,就不同批。这是唯一能防「两个 worker 同时写同一个文件 → 静默损坏」的机制。
3. **文件未知 ⇒ 单独跑** —— 没有 recon 文件信息时,**未知不等于无冲突**,所以那个 story 独占一批。

第 3 条有个直接后果:**关了 recon(`--no-recon`)又用 `auto`,就等于串行**。这不是退化 bug,是安全默认——要并行就得有「它们不会撞车」的证据。想在没有 recon 的情况下并行,用 `manual` 自己分组(`manual` 会照你的分组执行,文件重叠时只在 log 里警告,不拦)。

**`--max-parallel N`**(默认 10)对所有模式生效,也是并发上限。

**死锁不硬转**:如果某个 story 的 `dependsOn` 指向一个不存在的 id,它会永远不 eligible。这时 loop 不空转到 `max_iterations`,而是**立刻退出并返回 `NO_PROGRESS`(exit 10)**,`progress.txt` 里写明是哪个 story 在等哪个不存在的依赖。

**prd.json 可选字段**(全部向后兼容,不写就是旧行为):

```json
{
  "id": "US-008",
  "priority": 8,
  "dependsOn": ["US-001", "US-003"],   // 可选:等这些 story 过了才能开跑
  "files": ["tools/x.ts", "state/y.ts"], // 可选:显式声明会碰的文件(auto 用它判重叠)
  "parallelGroup": "g1"                  // 可选:manual 模式下按这个标签分组
}
```

`files` 不写时,`auto` 会去用 recon 读到的文件列表。

## Priority Grouping & Batch Strategy(核心伪代码)

> 下面这段是**结构示意**。函数名(`read_json` / `step_recon` / `build_story_task` / `parse_batch_result` …)是伪码名,**不代表真实符号**。真实实现见 `scripts/ralph.py`,方法名以 `_outer_loop` / `_run_batch` / `_evaluate` / `_step_recon` / `_render_worker_prompt` 为准。

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

    # Step 3: 读 prd.json
    # ⚠️ 澄清一处历史误导:这里**没有**"从想法/契约生成 story"的环节。
    #    真实实现 scripts/ralph.py::_step_normalize_prd 只做字段归一
    #    (setdefault priority/id/passes),不产出任何新 story。
    #    把 PRD 正文拆成 story,是上游 `prd` + `ralph` 两个 skill 在 Claude Code 里干的事;
    #    本 skill 把 prd.json 当作**输入前提**,不是它的产物。
    prd = read_json(prd_path)

    # ★ 拆完 prd.json 后,再 pause 一次让老板 review 拆解结果 ★
    goal_manager.pause("等待老板 review prd.json (story 拆分结果)")
    if not user_approves_prd():
        return "USER_REJECTED_PRD"

    # Step 3.5: ★ recon — 并行接地 ★(本 skill 对上游 Ralph 的补课,详见 §Recon)
    #   派 N 个**只读** recon worker 去真实代码库,把每个 story 的 acceptanceCriteria
    #   从"纸面期望句"重写成"可判定的验收 + 真实接口签名 + path:line 证据"。
    #   worker 只上报发现;**所有写入由 orchestrator 在整批返回后统一做**(并行安全)。
    #   失败不致命:recon 拿不到东西就照原样跑,只在 progress.txt 留痕。
    prd = step_recon(prd_path, prd, project_root, group_size=3)   # _step_recon()

    # Step 4: 外层循环 — 每轮按 --parallel-mode 挑一批 pending story 一起 fan-out
    #          (默认 auto:依赖 + 文件重叠判定,跨 priority 分组)
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

## Recon(并行接地 —— 本 skill 对上游的补课)

### 为什么需要它

上游 Ralph 从来不"查接口",但它**不需要查**:迭代 agent 起在项目根目录里,写 story JSON 时眼睛看得到真代码,实现时边做边读。**接地是它的载体属性,不是流程里的一步。**

一旦执行被委派给 fan-out worker,这个属性就没了——worker 只拿到 story 标题 + 验收标准 + 两个文件路径,关于"这个世界长什么样"的信息是**零**。于是它只能猜接口,猜错在编译时爆掉,再由 orchestrator 事后手修。

> **实证(peer-contract v0.4.1.0 US-008)**:worker 写了 391 行,6 个编译错**全部是接口级**的(import 路径、instance method vs 顶层函数、工厂类型),业务逻辑错 0 个。而且那 5 个错**是在 worker 超时死掉之后才第一次被看见的**——因为它连 `node_modules` 都还没装好,`tsc` 跑不了,自校验都做不到。

这不是 worker 水平问题,是**输入里缺了东西**。Hermes 侧必须在派活之前把它显式补回来。

### 怎么做的

```
prd.json(纸面验收标准)
      ↓
派 N 个只读 recon worker(按 story 分组,一组一个,同批并行)
      ↓
每个 worker 读真实代码库,逐 story 回答:
  1. 要改/建哪些文件?现在存在吗?
  2. 要调用的既有接口,真实签名是什么?给 `path:line` 证据
  3. 这个模块的既有约定(导出方式 / 命名 / 构建命令 / 测试命令)
  4. 陷阱(同名不同物 / 废弃 API / 隐式类型 / 静态检查抓不到的约束)
  5. 把 acceptanceCriteria 重写成可判定的形式
      ↓
worker 只**上报**(一个 <recon> JSON 块),不写任何文件
      ↓
orchestrator 在整批返回后统一合并,写入 prd.json + progress.txt
```

实现在 `scripts/ralph.py`:`_render_recon_prompt()` / `_parse_recon_block()` / `RalphGoalLoop._step_recon()`。

### 三条硬规则

1. **并行留,但写归 orchestrator。** recon worker 之间并行(N 个 worker 一批),但**没有任何 worker 碰 prd.json 或 progress.txt**。所有写入发生在整批返回之后,由 orchestrator 单独做——并行 worker 因此不可能互相踩。执行阶段同理。

2. **每条结论必须有证据。** 找不到的接口写 `NOT FOUND`,不许编一个看起来合理的签名。prompt 原话:"编造一个看起来合理的签名,比报告它不存在伤害大得多。"

3. **我们的描述不是权威,代码才是。** worker prompt 里明写:"你可能会被交到一个我们自己先前搞错的签名——**以代码为准,不以我们的描述为准**。"
   这条是针对真实事故加的:上一轮 orchestrator 派活时手写了一段「**真签名**(我 commit 的, 不能 mental-model 推)」,5 条错 4 条——用权威语气把猜测包装成事实,worker 反而不去怀疑了。**这是本次事故里最伤的一处。**

### 回写(跨轮记忆)

每批 worker 跑完,orchestrator 从它们的 summary 里收集三类行:

```
- Pattern: <这个代码库怎么做事的,带 file:line>
- Gotcha:  <踩到的非显然陷阱,带 file:line>
- File:    <X 定义/存在在哪,带 path:line>
```

去重后追加进 `progress.txt` 的 `## Codebase Patterns`。下一批 worker 的 prompt 里会被注入 `## Grounding` + `## Codebase Patterns` 两段。

**这是上游"知识滚雪球"的对应物**:上游靠迭代 agent 每轮回写 `CLAUDE.md`,这里靠 orchestrator 每批合并写 `progress.txt`。

### 开关

| flag | 默认 | 作用 |
|---|---|---|
| `--no-recon` | recon 开 | 跳过整个 recon 阶段(story 保持纸面验收标准) |
| `--recon-group N` | 3 | 一个 recon worker 领几个 story |
| `--project-root PATH` | cwd | worker 读代码的根目录 |
| `--worker-provider NAME` | `config.yaml::model.provider` | **换 provider**跑本 loop(不动全局 config)。provider 挂了 / 限流时用 |
| `--worker-model NAME` | `config.yaml::model.default` | **换模型**跑本 loop。例:`--worker-provider <p> --worker-model <m>`(用你环境里真有的值) |
| `--parallel-mode MODE` | `auto` | 并行分组策略:`off` / `priority` / `auto` / `manual`(见 §Parallelism) |
| `--max-parallel N` | 10 | 单批 story 数上限,也是并发上限 |

**recon 失败不致命**:拿不到东西就照原样跑,只在 `progress.txt` 留痕(`recon: ... proceeding ungrounded`)。刻意如此——补课环节不该变成新的故障点。

### 实测覆盖

`scripts/test_recon.py`(23 个测试)覆盖:prompt 生成 / `<recon>` 解析(含 ```json 围栏容错)/ grounding 渲染 / worker prompt 注入 / 多段 section 读取 / learnings 提取去重 / **`_step_recon` 端到端(用 mock delegate_task,验证是 orchestrator 而非 worker 写文件)** / worker 输出全是垃圾时不改 prd.json / delegate 抛异常不致命 / `--no-recon` 短路。

**已实测(2026-09-17,DeepSeek `deepseek-flash`)**:拿**本 skill 自己的代码库**当靶子跑了 2 个 story 的 recon,结果:

- 输出 **22 条真实签名 + 17 条既有约定 + 17 条陷阱**,每条都带 `path:line` 证据(例:`_outer_loop(self) -> str` [scripts/ralph.py:640];`delegate_task(...) 返回 JSON 字符串而非 dict` [tools/delegate_tool.py:417-423])
- **它揪出了本 skill 自己代码里的一个真 bug**:`_read_shared_facts` 的截断写作 `out[-limit_chars:]`,而 Python 里 `[-0:]` 就是 `[0:]` —— **传 0 会返回全部内容而不是空**,负数会从错误的一端切。已修 + 加回归测试(见 `test_recon.py::test_non_positive_budget_shares_nothing`)。
- 它读到了 `SKILL.md` 里"**不要**实现 `hermes ralph`"那段,并把它当成约束写进了 gotcha —— 说明 worker 确实读了上下文,不是闭眼写。
- 它推翻了 story 里的纸面假设:US-R2 的第一条验收标准"让读取函数接受调用方给的预算" **在代码里已经是真的了**(参数早就在),真正缺的只是调用点的贯通。**这正是 recon 该干的事——把纸面主张拿到代码里核对。**

**代价**:2 个 story 的 recon 花 **$1.78**(按模块内硬编码的 $/1k tokens 估算,非官方价目)。story 多时按 `--recon-group` 摊薄(默认 3 story/worker),但**这是一笔真实成本**,不是免费的。

**仍未验证**:真 delegate 的 fan-out worker 在真项目上的**长期**表现(多轮、跨 priority、learnings 逐步积累的效果)——需要一次真实的多 story 跑动。

## Launching the Loop

```bash
# 1. 准备 prd.json(自己写 / 抄模板 / 用上游 prd + ralph 两个 skill 从 PRD.md 转)
#    在**项目根目录**下跑(或显式 --project-root),recon 的 worker 按这个根去读代码。
cd /path/to/your/project
ls prd.json  # 确认有

# 2. 启动 loop
python ~/.hermes/skills/ralph-goal-loop/scripts/ralph.py --prd ./prd.json --project-root .

# 想用别的模型跑(不动全局 config.yaml),加这两个:
#   --worker-provider deepseek --worker-model deepseek-flash

# ⚠️ 不是 `hermes ralph` —— 本 skill **没有** hermes 子命令(原因见下)。
# 等价于: /goal draft → 老板 review → recon 接地 → /goal set + fan-out
```

### 为什么没有 `hermes ralph` 子命令

早期设计里写过 `hermes ralph --prd ...` 这个入口。它**从未实现**,而且**不该实现**:

- 注册 `hermes` 子命令,必须在 `hermes_cli/main.py` 里加 argparse 分支 —— 那就是**改 Hermes 核心**,跟本 skill 自己承诺的"0 modify hermes-agent"直接矛盾(该矛盾在早期 plan 里就存在,一直没被拎出来)。
- 实测 `hermes ralph --help` 返回 `hermes: 'ralph' is not a \`hermes\` command.`
- skill 的正常形态本来也不需要 CLI 入口 —— 跑编排器用 `python <skill>/scripts/ralph.py` 就够。

**老板 review 节点(实际是「半自动」的)**:

代码里只有一个真实停点:`_step_review_prd()` 往 `progress.txt` 写一行 `REVIEW_NEEDED:` marker,然后**立即 pause + resume** —— 也就是**不阻塞**。老板想看就看 `prd.json` / `progress.txt`,不看也不会卡住流程。

这符合本 skill 的设计意图:**质量由流程保证,人不是保险丝** —— 老板想看随时能看,不看也不塌。
- **节点 1**(GoalContract 拆完后):`draft_contract()` 的产出**只喂 `/goal` 状态机**,代码里**没有**把它显示给老板的环节 — 早期文档这一条与实现不符。
- **节点 2**(prd.json 拆完后):orchestrator 调 `goal_manager.pause("boss-review-prd")`,紧跟一行 `# v0.1.0: auto-resume` 立即 `resume()` — 所以它是**留痕**,不是阻塞门。要真阻塞,得自己把那行 resume 拿掉。

**两个节点都不是阻塞门**。想中途叫停,直接 `/goal clear` 全盘放弃即可(或 kill 掉 python 进程) — orchestrator 每轮会校验 `goal_manager.is_active()`,`clear` 之后下一轮就退。

## Model & Credentials(本 skill 的两条硬约束)

**1. 不硬编码模型。** 模块里没有任何写死的 provider / model 名。解析顺序只有两条:

```
--worker-provider / --worker-model     ← 显式覆盖,只对本次 run 生效
        ↓ 没有的话
config.yaml::model.{provider,default}  ← 跟 `hermes chat` 用同一份配置
        ↓ 还是没有
交给 Hermes 自己的 provider ladder      ← 传 None,由 Hermes 决定
```

这么设计的原因很直接:写死一个模型名 = 把 skill 绑死在一个厂商上,而且会在**那个模型正好挂掉的那天**变成唯一路径(先前版本写死了一组兜底值,结果配置里挂着已失效的 provider 时,loop 永远走那条死路)。想临时换模型跑,**不要改 `config.yaml`**,传 flag 就行:

```bash
python scripts/ralph.py --prd ./prd.json --project-root . \
  --worker-provider <provider> --worker-model <model>     # 用你环境里真有的值
```

`--judge-provider/--judge-model/--judge-base-url/--judge-api-key-env` 同理,单独覆盖 judge。

**2. 仓库里不留任何密钥。** 实测扫描(针对 `ghp_*` / `sk-*` / `api_key=` 形态):

- 仓库**工作树内**零密钥 —— 所有凭证都从环境变量(如 `OPENROUTER_API_KEY`)或 `config.yaml` 读,代码里只有变量名。
- `git remote` 的 URL **曾经内嵌过一个 token**,已移除,现在是不带凭证的 `https://github.com/Hulker309/ralph-goal-loop.git`。**推送时用环境变量给凭证**(例如 `GH_TOKEN`),别再写进 remote URL。
- 测试里出现的 `api_key = "***"` 是占位字符串,不是真值。

**换到新环境要做的**:确认 `config.yaml` 里的 provider 有可用凭证(或设好对应的环境变量),然后跑一次 `python scripts/ralph.py --prd <prd> --help` 确认 flag 齐了、再小范围跑一个 story 验证连通。

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
| judge LLM 选哪个 | **走 `resolve_runtime_provider()` ladder**(同 `hermes chat` 一条路径) | 软 | 老板可在 `config.yaml::auxiliary.goal_judge.{provider,model}` 改,或 CLI `--judge-{provider,model,base-url,api-key-env}` 覆盖 |

**judge model 设计意图**(走 `resolve_runtime_provider()` ladder + 允许覆盖):

> **Default (v0.3)**: judge 走 `hermes_cli.runtime_provider.resolve_runtime_provider()` ladder 拿完整 runtime(provider/model/base_url/api_key/api_mode),跟 `hermes chat` 同一条路径——避免 base_url 后缀推断 + api_mode 缺失导致的 endpoint 404。`scripts/ralph.py:234-265` `_init_parent_agent` 调它构造 parent agent,judge override 链路(`_resolve_judge_overrides` + `_judge_call_overrides_ctx`,`scripts/ralph.py:69-170`)继承 parent 的 runtime 后再覆盖。Rationale: 当 judge 跟 worker 是同一个模型时,它们共享同一个"worldview"——同套 domain knowledge,同套"什么算 done"直觉。不同模型 judge 可能在边界情况下不同意,不是 work 错,而是 default 不同。
>
> **User override**: set `auxiliary.goal_judge` in config.yaml to route the judge to a different model, **或** CLI 启动时传 `--judge-provider X --judge-model Y --judge-base-url Z --judge-api-key-env ENV_VAR`。CLI flag 优先级高于 config(`/goal` engine + `agent.auxiliary_client.call_llm` + 本 skill 的 `_judge_call_overrides_ctx` 一起支持,不动 hermes-agent)。
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
> 或命令行:
> ```bash
> python ~/.hermes/skills/ralph-goal-loop/scripts/ralph.py --prd ./prd.json \
>   --judge-provider openrouter --judge-model google/gemini-3-flash-preview \
>   --judge-keep-aux-config   # 反向:无视 CLI,继续用 auxiliary.goal_judge 配置
> ```

> **v0.3 fix (相对 v0.2 的 bug 修复,影响 judge 路由)**:
> - **症状**:v0.2 `AIAgent()` 空 kwargs 构造 parent,丢了 `model.provider/model`,judge 调用继承到 broken runtime(典型:`MiniMax-M3` + `/anthropic` 后缀但 api_mode 缺失,POST 到 `/chat/completions` 404)。
> - **修法**:v0.3 `scripts/ralph.py:234-265` `_init_parent_agent` 调 `resolve_runtime_provider(requested=provider, target_model=model)`,拿完整 5 元组(provider/model/base_url/api_key/api_mode)再构造 `AIAgent(...)`,跟 `hermes chat` ladder 2-8 同源。
> - **验证**:本机 `Desktop/hermes-ralph-goal-loop-test/` 3-story prd 实跑过,judge call 走 parent 同一 endpoint,无 404。**注意该次实跑是人工按 skill 协议手跑的**——当时 `hermes ralph` CLI 并不存在,orchestrator 的角色由人扮演(见 §Launching the Loop)。
> - **回退路径**:老 config `auxiliary.goal_judge.*` 仍然受 `_resolve_task_provider_model` 路由;用 CLI `--judge-keep-aux-config` 反向锁住老行为。
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
2. **并行要么不触发、要么撞文件** — 旧的 `min(priority)` 分组有个双向坑:story 各自独占 priority 时(上游 `ralph` skill 的默认编号习惯)**一批只有一个 story,并行一次都不发生**;而如果为了并行把不相关的 story 硬塞进一批,**两个 worker 可能同时写同一个文件 → 静默损坏**。**mitigation**:`--parallel-mode auto`(默认)用「依赖 + 文件重叠」判定,只把**已知不冲突**的 story 放同批,文件未知的单独跑;重叠判定依赖 recon 的 `files`。
3. **子 agent 期望 `skip_memory` 透传** — `delegate_task` 根本没有这个 kwarg,`skip_memory=True` 在 `tools/delegate_tool.py:240` 硬编码到 child AIAgent 构造;**mitigation**: orchestrator 入口**不传** `skip_memory`,等价于"已自动开启"
4. **`max_budget_usd` 没硬卡** — 单 loop 没界,cost spike 跑飞;**mitigation**: orchestrator 启动时校验,超出立即 abort 不 retry(不留中间状态)
5. **`delegate_task` 只有 1 个 task 时不并发** — batch mode 只在 `len(tasks) >= 2` 时启用(`tools/async_delegation.py` 的 `_executor`)。**这是预期行为,不是缺陷** —— 但要注意它的后果:**如果 prd.json 里每个 story 独占一个 priority,N 恒等于 1,整个 loop 完全串行,并行一次都不会发生**。并行不是免费的,它要求你在拆 story 时**把互不冲突的 story 放进同一个 priority**。
6. **prd.json 在 dirty 主 checkout 里改** — Ralph 实施文件改动 + prd.json 改动混在同一次 commit 里,后面 cherry-pick / revert 都麻烦;**mitigation**: orchestrator 启动时**强制要求 git worktree**(跟 `kanban-codex-lane` 同一套 worktree pattern)
7. **退化成"借外部 Claude Code 跑"** — fork 进程 30 秒 exit,失去 Hermes 自己的 cache 边界 + cost 路径;**mitigation**: 本 skill description 第一句写明 "0 external CLI, 100% Hermes in-process"
8. **`auxiliary.goal_judge` 配错** — 老板配了一个不存在的 provider/model,judge call 静默 fail,主循环退化成"一直 continue"直到 max_iterations;**mitigation**: orchestrator 启动时 sanity check `goal_judge_setting()` 返回有效值,无效就 warn 但不 abort
9. **paused 状态被外部改** — 老板中途 `/goal clear`,orchestrator 还不知道,继续跑下一轮;**mitigation**: orchestrator 每轮 `goal_manager.is_active()` 校验,not active → abort 报 `GOAL_CLEARED`
10. **v0.2→v0.3 进化: `api_mode` 缺失会让 child POST 到错 endpoint** — 旧版 `AIAgent()` 空 kwargs 构造 parent,丢了 `model.provider/model` + `api_mode`,child judge / delegate 走 `auxiliary_client.call_llm` 时 POST 到 `/chat/completions`,但 MiniMax `base_url` 后缀是 `/anthropic` 或 v1 路径 → 404 or 401。**mitigation (v0.3 已修)**: `scripts/ralph.py:240-265` `_init_parent_agent` 调 `hermes_cli.runtime_provider.resolve_runtime_provider(requested=provider, target_model=model)`,拿完整 5 元组(`provider/model/base_url/api_key/api_mode`)再构造 `AIAgent(...)`,跟 `hermes chat` ladder 2-8 同源。CLI 启动时仍可 `--judge-keep-aux-config` 锁回老 `auxiliary.goal_judge.*` 路由,见 Pitfall #8。

11. **recon worker 编造接口签名** — 这是 recon 反而**制造**故障的方式:一个看起来合理的假签名比 `NOT FOUND` 危险得多,因为它把错误伪装成事实,执行者就不去核实了;**mitigation**: prompt 硬规则要求每条结论带 `path:line` 证据、找不到写 `NOT FOUND`,并显式声明"我们的描述不是权威,代码才是"(见 §Recon 硬规则 2/3)。真实事故:派活时手写的「真签名」5 条错 4 条,worker 当场顶回 2 条却没怀疑整体框定。
12. **recon worker 直接写 prd.json / progress.txt** — 多个 recon worker 并行写同一个文件 = 静默损坏;**mitigation**: prompt 明令 READ ONLY、worker 只输出 `<recon>` JSON 块,**所有写入由 orchestrator 在整批返回后单独做**(`_step_recon`);执行阶段同理,**归写权始终在 orchestrator**。
13. **recon 开了但没人验它真跑过** — recon 失败是静默降级的(照原样跑,只在 progress.txt 留痕),所以"跑起来了"和"接上地了"从外面看不出区别;**mitigation**: 跑完查 `progress.txt` 里有没有 `recon: ... grounded=N story(ies)`,以及 `prd.json` 里有没有 `recon` 字段;查不到就是没接上,别 assume。
14. **给 `run()` 加一步就会打破所有离线测试** — `run()` 里现在有**两处**真 fan-out:`_step_recon` 和 `_run_batch`。只 stub `_run_batch` 的测试会**真调模型、真花钱**(本轮实际踩到:两个"离线"测试因为这个洞跑了真实 API 调用并挂住)。**mitigation**: 测试里用 `test_minidemo.py::_offline()` 这一个出口把所有对外 seam 一次 stub 掉;以后往 `run()` 加步骤,在 `_offline()` 里补一行,而不是去每个调用点补。

## Verification Checklist

完成本 skill 落地后,声明"完成"前逐项过:

- [ ] `python scripts/ralph.py --help` 显示正确帮助(含 `--recon` / `--recon-group` / `--project-root` / `--parallel-mode` / `--max-parallel`)
- [ ] **并行真的发生了** — 跑一个同 priority 或文件不重叠的多 story prd,`progress.txt` 里应出现 `round 1: mode=auto batch=N`(N>=2)。若一直 `batch=1`,查两件事:story 之间是否文件重叠,以及 recon 是否真的跑过(没有文件信息只能串行)
- [ ] **依赖被尊重** — 给一个 story 写 `dependsOn`,确认它在依赖 `passes: true` 之前不进任何一批
- [ ] **死锁不空转** — 把某个 `dependsOn` 写成不存在的 id,确认立刻返回 `NO_PROGRESS`(exit 10)而不是跑满 `max_iterations`
- [ ] 实跑本机 `C:/Users/Administrator/Desktop/hermes-ralph-goal-loop-test/` 的 3-story prd.json,验证:
  - [ ] 至少 3 轮迭代(每 priority 1 轮)
  - [ ] prd.json 全部 `passes: true` 后进程退出
- [ ] 内层并行真的发生 — 看 `~/.hermes/cache/delegation/live/<id>/task-*.log` 确认 `delegate_task(tasks=[N>=2])` 同 turn 触发,`DaemonThreadPoolExecutor` 实际并发
- [ ] **recon 阶段真的跑了** — `progress.txt` 有 `recon: N pending stories → M recon workers` 和 `## Grounding` 段;`prd.json` 里每个 pending story 带 `recon` 字段(查不到 = 没接上,见 Pitfall #13)
- [ ] **写隔离成立** — 通读一遍 `progress.txt` / `prd.json` 的改动历史:worker **从未**直接写过这两个文件,只有 orchestrator 写过
- [ ] **回写闭环成立** — 第一批跑完后 `progress.txt` 出现 `## Codebase Patterns` 段;且**第二批** worker 的 prompt 里带上了 `## Facts already established`(看 `~/.hermes/cache/delegation/live/<id>/task-*.log`)
- [ ] `--no-recon` 生效 — prd.json 里不出现 `recon` 字段
- [ ] `python scripts/test_recon.py` 23/23 通过;`python scripts/test_minidemo.py` 全过(回归)
- [ ] 终止协议生效 — worker 输出含 `<promise>COMPLETE</promise>` → orchestrator grep 命中 → 退
- [ ] 中间 review 节点生效 — `goal_manager.pause("等待老板 review prd.json")` 后 orchestrator 真的停在等 `/goal resume`,老板 `/goal resume` 后继续
- [ ] `git diff` 在 `hermes-agent/` 是 **0 改动**(纯新增 skill;**没有** CLI argparse 分支——见 §Launching the Loop)
- [ ] `agent/prompt_builder.py` / `tools/delegate_tool.py` / `DELEGATE_BLOCKED_TOOLS` 三件套 0 改动(不变量)
- [ ] `metadata.hermes.related_skills` 引用了 `kanban-codex-lane` + `openclaw-plugin-author-suite` + `hermes-agent`
- [ ] cost cap 真的生效 — 把 `max_budget_usd` 设成 $0.01,跑一个会超的 prd,验证立即 abort 不继续
- [ ] judge LLM 两条路径都没真调过: (a) `auxiliary.goal_judge.*` config 路由 + (b) CLI `--judge-{provider,model,base-url,api-key-env}` 覆盖 path——2 条 path 各自的 judge call model 字段未在 `~/.hermes/logs/` 里端到端验过(注:`resolve_runtime_provider()` ladder 本身已被 `hermes chat` 验证,见 §Not verified)
- [ ] negative test:故意写 1 个 story 的 acceptance criteria 错(比如 `assert 1==2`),验证子 agent 标 `passes=false` 不退出,主 skill 进入下一轮 retry 直到 max_iterations

## References

- `CLAUDE.md.tmpl`(本 skill 内)— worker prompt 的**设计参考**。运行时**不用它**(见文件头状态说明);真实 prompt 由 `scripts/ralph.py::_render_worker_prompt` 生成
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

- ✅ ~~step 2 的 `goal_manager.set(goal_text, contract=contract)`~~ 在 v0.21.3 实跑过 — `scripts/test_minidemo.py` 5/5 PASSED,`goal_manager.set(goal_text, contract=contract)` 路径工作正常
- 0 step 5 的 `delegate_task(tasks=[...])` 在**独立 python 进程**(不挂 `hermes chat`)里能否调通 —— `_init_parent_agent` 会自己 `AIAgent(...)` 构造 parent 再传,但**这条路径未在真环境验过**。`hermes ralph` CLI 模式本身**不存在**(见 §Launching the Loop)
- 0 sub-agent 报告说 `DELEGATE_BLOCKED_TOOLS = {delegate_task, clarify, memory, send_message, cronjob_manage}`,这是从 `tools/delegate_tool_toolsets.py:14` 读出来的;但**实际的阻塞是在 child agent 收到 task 时 filter tools,不是 child 的 toolset 里没有**,这条对 worker 写 prd.json 不构成阻碍,但 precision 仍待 Phase 3 实测验
- 0 judge LLM 的具体 token 消耗 + 主模型 judge vs Gemini Flash judge 实际质量差(per `/goal` 官方说 ~200 token,Phase 3 实测验)
- 0 跨 priority batch 间的"等上一组全 passes=true 才开下一组"——这个 orchestrator 逻辑本 skill 自己实现,`/goal` 不管 priority。Phase 3 实测验
- 0 `resolve_runtime_provider()` ladder 本身端到端验证 — v0.3 fix 信任 `hermes chat` ladder 2-8 的现成行为(老板 v0.21.3 实跑 `hermes chat` 通过),ralph 端单独不重测;若未来 ladder 上游变了,需重新跑 `scripts/test_minidemo.py` 验证 `_init_parent_agent` 仍能拿到完整 5 元组
