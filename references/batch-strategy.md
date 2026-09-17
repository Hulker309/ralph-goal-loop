> ⚠️ **状态说明(2026-09-17 核对)**:本文是 **v0.1.0 设计文档,不是代码现状**。
> 文中带 `_stepN_` 前缀的方法名(`_step6_outer_loop` / `_step7_run_batch` / `_step8_evaluate` /
> `_step4_expand_to_prd` / `_build_story_task` / `_check_file_overlap` / `run_single_story_inline`)
> 在 `scripts/ralph.py` 里**全部不存在** —— 脚本已改道到另一套命名(`_outer_loop` / `_run_batch` /
> `_evaluate` / `_step_recon` / `_render_worker_prompt`)。本文保留,作为**设计意图**参考。
> 要核对真实行为,以 `scripts/ralph.py` 和 `SKILL.md` 为准。

# Batch Strategy — Priority Grouping & delegate_task Fan-out

> 配 `SKILL.md §Priority Grouping & Batch Strategy` 段。本文件是它的事实展开:为什么按 priority 分组、伪代码、1-story 退化、result 解析、文件冲突回避、跨 priority 同步。

## 1. 为什么按 priority 分组(不是一次性 fan-out 所有 pending)

**核心动机**(per SKILL.md §Pitfalls 第 2 条):

> 同 priority story 没分组 fan-out — 直接把所有 pending story 一起跑,priority 2 撞 priority 1 产出 = 文件冲突 / 上下文错乱;**mitigation**: orchestrator 强制按 `min(priority)` 分组,跨 priority 等上一组全 `passes=true` 才开下一组

**两个独立原因**:

### 1a. 文件/上下文冲突

`prd.json` 是单文件。priority 1 story 跟 priority 2 story 同时 fan-out,**两个 child agent 同时 `read_json(prd_path)` + `write_json(prd_path, modified)` = last-write-wins 静默丢数据**。同样 priority 1 的 worker 写到 `progress.txt` 时,priority 2 worker 也在 append,会拼成混乱的故事叙述。

按 priority 分组强制**串行的两层结构**:round 1 只跑 priority 1,priority 1 全 `passes=true` 后 round 2 才跑 priority 2。每轮只动 prd.json 一次,append progress.txt 也只在同一 priority 内多个 worker 并发(下面 §4 给出冲突回避)。

### 1b. 调试可观察性

混跑时,出错 story 的 verdict 跟正常 story 的 verdict 混在一起,debug 时分不清"哪个 priority 出错"。分组后,每轮 batch = 1 个 priority = 1 个清晰的成败单元,跟 SKILL.md §Verification Checklist "至少 3 轮迭代(每 priority 1 轮)" 对齐。

## 2. 伪代码 — 完整 orchestrator 主循环

```python
def _step6_outer_loop(self) -> str:
    """每轮: 读 prd → 挑 batch → _step7_run_batch → _step8_evaluate."""
    for round_idx in range(self.max_iter):
        # 读最新 prd.json(worker 可能在子 agent 里改过 passes 字段)
        prd = read_json(self.prd_path)
        pending = [s for s in prd["userStories"] if not s.get("passes", False)]

        # 终止条件 1: 全 passes
        if not pending:
            return "ALL_PASSES"

        # 终止条件 2: 老板中途 /goal clear
        if not self.goal_manager.is_active():
            return "GOAL_CLEARED"

        # 挑当前 priority 最低(=最先跑)的所有 pending story
        top_priority = min(s["priority"] for s in pending)
        batch = [s for s in pending if s["priority"] == top_priority]

        # 跨 priority 同步点: 本轮只跑 batch 这一组,等结果才进下一轮
        self._log_progress(f"round {round_idx + 1}: priority={top_priority}, batch_size={len(batch)}, "
                          f"stories={[s['id'] for s in batch]}")

        # 内层 fan-out(走 delegate_task batch mode 或 inline)
        batch_result = self._step7_run_batch(batch)
        if batch_result["status"] == "DELEGATION_FAILED":
            return "DELEGATION_FAILED"

        # judge 软判定
        decision = self._step8_evaluate(batch_result)
        if decision == "GOAL_DONE":
            return "GOAL_DONE"

        # cost cap 兜底
        if self.cost_so_far_usd >= self.cost_cap_usd:
            return "COST_CAP"

    return "MAX_ITERATIONS"
```

## 3. `_step7_run_batch` — 1-story 退化 vs N>=2 fan-out

```python
def _step7_run_batch(self, batch_stories: list[dict]) -> dict:
    """调 delegate_task(tasks=[...], background=False) 或 1-story 退化路径."""
    if len(batch_stories) == 0:
        return {"status": "EMPTY", "results": []}

    # 构造 tasks[] — 每个 story 1 个 task
    tasks = [self._build_story_task(s) for s in batch_stories]

    try:
        # 阻塞等所有 children 完成,返回 json dict
        result_json = delegate_task(
            tasks=tasks,
            parent_agent=self.parent_agent,   # ★ 必传 per delegate_tool.py:428
            background=False,                 # 阻塞 join
            # skip_memory 不传 — 子 agent 默认 skip_memory=True 已硬编码
            # max_iterations 不传 — config.yaml::delegation.max_iterations 是权威
        )
        result = json.loads(result_json)
    except Exception as exc:
        self._log_progress(f"DELEGATION_FAILED: {exc!r}")
        return {"status": "DELEGATION_FAILED", "error": repr(exc), "results": []}

    return {"status": "OK", "results": result.get("results", [])}
```

### 3a. `len(tasks)==1` 退化(per `tools/async_delegation.py:32`)

`_executor` 是 `Optional[ThreadPoolExecutor]`,只在 `len(tasks) >= max_async_children` 或 `len(tasks) >= 2` 时启用(per `async_delegation.py:573` `_get_executor(max(...))`)。当 `tasks=[s1]` 时:

- `_executor` 不会被 `_get_executor` 调起
- 实际走 `_run_child_lifecycle` 单 child 串行
- 等价于 inline 跑 1 个 child,**不并发**,但**正确性 100% 等价**

**本 skill 接受这个 trade-off**:1-story batch 不强行制造并发(没意义,1 个 task 没法跟自己并发),N>=2 batch 自动 fan-out。

**真正坑**(per SKILL.md §Pitfalls 第 5 条直接引用):

> 走 `delegate_task` 但只有 1 个 task — 不会走 batch mode(`tools/async_delegation.py` 的 `_executor` 只在 `len(tasks) >= 2` 时启用),等于退化成串行;**mitigation**: orchestrator 主动判定,1 个 story 时直接 inline 跑不走 batch

本 skill v0.1.0 简化:**1-story 也走 `delegate_task(tasks=[s1])`**,自然 inline,省掉分支。**不**用 `subprocess` 调外部 Claude Code(per T3 设计点 8,明确不调外部 CLI)。

### 3b. `len(tasks)>=2` 走 batch mode

`delegate_task` 内部走 `_run_batch(batch, background=False)`(`delegate_tool_dispatch.py:441`),`background=False` → `_execute_and_aggregate` 同步 join。每个 child 跑在 `DaemonThreadPoolExecutor(max_workers=batch.max_children)`(per `delegate_tool_dispatch.py:135`)。`max_children` 从 `config.yaml::delegation.max_concurrent_children` 读,默认 10。

**关键 invariant**(per `delegate_tool.py:449-456`):
- `parent_agent._delegate_depth` < `max_spawn_depth` 才允许 spawn
- orchestrator 自己深度 0,children 深度 1,grandchildren 深度 2(per `delegation.max_spawn_depth`,默认 2-3)
- 超过深度 → `delegate_task` 返回 `tool_error` JSON

## 4. Result 解析 — 字段对位

`delegate_task(tasks=[...], background=False)` 返回 JSON 字符串(`delegate_tool.py:506` `return _run_batch(...)`,`_run_batch` 在 `delegate_tool_dispatch.py:441` `return json.dumps(_execute_and_aggregate(batch), ensure_ascii=False)`)。

`_execute_and_aggregate` 的输出结构(per `delegate_tool_dispatch.py:217` 跟 `_build_result_entry`):

```json
{
  "results": [
    {
      "task_index": 0,
      "status": "completed",          // "completed" | "failed" | "timeout" | ...
      "summary": "...worker text...", // worker 最终返回给 parent 的文本
      "exit_reason": "max_iterations", // "completed" | "max_iterations" | "tool_error" | ...
      "tokens": {"input": 1234, "output": 567},
      "duration_seconds": 12.3
    },
    ...
  ]
}
```

**orchestrator 判定规则**:

| 判定 | 字段 | 值 |
|---|---|---|
| 成功 | `entry["status"] == "completed"` AND `entry["exit_reason"] == "completed"` | worker 真做完所有 acceptance criteria |
| 需重试 | `entry["exit_reason"] == "max_iterations"` | worker 跑到 config 设的迭代上限还没完,本轮算 partial,等下一轮 judge 决定 |
| 失败 | `entry["status"] in {"failed", "timeout"}` | worker 直接挂,记 progress.txt,继续下一轮 retry |
| schema 错 | `entry.get("schema_valid") is False AND entry["status"] == "completed"` | output_schema 校验失败(per `delegate_tool_dispatch.py:94`),本 skill v0.1.0 不传 output_schema,这条永远不命中 |

**summary 文本扫 `<promise>COMPLETE</promise>`**:

```python
def _has_promise(self, summary: str) -> bool:
    return "<promise>COMPLETE</promise>" in (summary or "")
```

`<promise>COMPLETE</promise>` 是 worker 自己 echo 的 literal token(per `CLAUDE.md` 模板强制要求)。orchestrator 拿到 summary → grep 这字符串 → 双保险:有 + judge done → 退 GOAL_DONE;有 + judge continue → 仍退 GOAL_DONE(以 worker 报告为准,因为 worker 真完成了);无 + judge done → 仍退 GOAL_DONE(以 judge 为准);无 + judge continue → 下一轮 retry。

## 5. 文件冲突回避 — 1 story = 1 新文件路径

**核心规则**:同 priority 内 fan-out,每个 worker 写**不同的**新文件。**禁止**多个 worker 同时改同一个文件(per `CLAUDE.md` 模板 "Do not write to files outside your own story's acceptance-criteria scope")。

**prd.json 跟 progress.txt 的冲突**:这两个是"公共文件",所有 worker 都要写。回避办法:

- **prd.json**: orchestrator 在 `delegate_task` 返回**之后**才写 `passes=true` 字段,worker 自己**只**标 `passes=true` 不写其它字段,且只在 `passes=true` 这一行写,**不**读改其它字段
- **progress.txt**: 每个 worker append 自己 `## [Story ID]` 段,**append-only**,不同 worker 的段不重叠(以 `## US-` 起始作为 anchor),append 长度 ~10 行,Linux `open(O_APPEND)` atomic(per Python `open(path, "a")` 在 POSIX 上用 `O_APPEND`,多 writer append 不会 interleaved 字节)

**新文件**(`greet.py` / `test_greet.py` / `count_vowels.py` 等):每个 story 的 acceptance criteria 显式指定文件路径,worker 在自己 story 内只 `create_files` 这些路径。**冲突**出现在"两个 priority 1 story 都写 `utils.py`"时——这种情况 orchestrator 启动时 sanity check `prd.json` 每个 story 的文件路径不重叠(`set(story_files) ∩ set(other_story_files) == ∅`)。

```python
def _check_file_overlap(self, prd: dict) -> list[str]:
    files_per_story = {}
    for s in prd["userStories"]:
        for ac in s["acceptanceCriteria"]:
            # 简陋提取: 找 acceptance criteria 里出现的 .py / .md / .json 文件名
            for token in re.findall(r"\b\w+\.(py|md|json|txt)\b", ac):
                files_per_story.setdefault(token, []).append(s["id"])
    overlaps = {f: ids for f, ids in files_per_story.items() if len(ids) > 1}
    return overlaps
```

v0.1.0 实装:**只在 orchestrator 启动时 warn**,不阻断。老板自己保证 acceptance criteria 拆得开。

## 6. 跨 priority 同步点

```python
# 伪代码 §2 主循环里:
batch = [s for s in pending if s["priority"] == top_priority]
#                                       ^^^^^^^^^^^^^^^^^
# 这一行就是跨 priority 同步点: 每轮只挑 1 个 priority 的 batch
```

`min(priority)` 保证:**优先级最高的(数值最小的)那一组 story 一定先跑完才开下一 priority**。即使 priority 1 有 5 个 story,priority 2 也要等这 5 个全 `passes=true`。

**容错(⚠️ 本节原描述与实现相反,2026-09-17 核对后改写)**:原设计设想「priority 1 局部失败不阻塞 priority 2」。但 `_outer_loop` 的实际逻辑是每轮取 `min(priority)` 的 pending stories 组一批 —— **只要 priority 1 还有 `passes: false` 的 story,它永远是最小 priority,priority 2 永远不会开始**。

后果是**优先级饥饿**:一个跑不通的 story 会把整个 loop 卡死在那一层,一直空转到 `max_iterations`,后面的优先级再也没机会。这是真实行为,不是本文档原来说的「继续跑下一层」。

要避免饥饿,拆 story 时别让某个 priority 只有「一个难过」的 story 且它的阻塞面很大 —— 或者接受这是设计取舍(阻塞是保守的:宁可卡住也不要带着未完成的依赖往下跑)。

## 7. 不在范围内

- ❌ worker 跟 orchestrator 跨进程的 priority 协商(全 in-process,无需协议)
- ❌ 动态 batch size(根据 cost 或 token 实时调)—— v0.1.0 写死 `max_children=10`
- ❌ priority 中间插队(老板说 "US-3 跳过 US-1 直接跑")—— v0.1.0 不支持,要插队手动改 prd.json
- ❌ file overlap 的硬阻断—— v0.1.0 只 warn 不 abort(per §5)
- ❌ `delegate_task` 的 `output_schema` 参数—— v0.1.0 不传,靠 worker 自己 echo `<promise>` + judge 软判定
