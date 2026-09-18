# ralph-goal-loop

> 在 Hermes 内部跑多 story PRD:按 priority + `dependsOn` 顺序执行每个 story,直到全部 `passes: true` 才退。Ralph 运行在独立进程,老板的 Hermes 主 session 不受影响。

![Python ≥3.11](https://img.shields.io/badge/python-≥3.11-blue)
![MIT-ready](https://img.shields.io/badge/license-MIT--ready-yellow)
![Hermes v0.21.3+](https://img.shields.io/badge/hermes-v0.21.3%2B-purple)

## English summary

`ralph-goal-loop` is an in-process port of the
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) for the Hermes
Agent runtime. It reads a `prd.json` with multiple user stories, runs them in
priority + `dependsOn` order via a persistent `AIAgent`, and exits only when
every story is `passes: true` — signaled by the literal token
`<promise>COMPLETE</promise>`. Ralph runs as a separate Python process; the
boss's main Hermes session is completely unaffected.

→ [English version](README.en.md) · [Architecture design](docs/ARCHITECTURE.md)

## 它是什么

`ralph-goal-loop` 把 Geoffrey Huntley 的
[Ralph Wiggum 技术](https://github.com/mikeyobrien/ralph) 移植到 Hermes 内部。

架构分两层:

| 层 | 组件 | 职责 |
|---|---|---|
| **编排层** | `RalphGoalLoop` (`scripts/core/orchestrator.py`) | pure Python,无 Hermes 依赖。读 `prd.json`,按 priority + `dependsOn` 决定下一批跑哪些 story,调用 `adapter.run_story()` |
| **执行层** | `HermesAdapter` (`scripts/adapters/hermes.py`) | 持有一个 `AIAgent` 实例,`run_story()` 为每个 story 构建 worker prompt,调用 `agent.run_conversation()` 执行 |

**跟上游 Ralph 的区别**:上游是 bash 循环每轮 fork Claude Code 子进程;本 skill 是同一个 `AIAgent` 实例跑所有 story,历史在同 session 内累积。

**跟 v0.4.0 的区别**:v0.4 借用了 Hermes `/goal` + judge LLM + `delegate_task` fan-out;v1.0 去掉这些,只保留 `AIAgent.run_conversation()`,是上游 Ralph 的更直接的移植。

## 前置要求

- **Hermes Agent v0.21.3+**
- **Python 3.11+** (用了 `tomllib` + `ExceptionGroup`)
- **LLM provider**:任何你在 Hermes 里配置过的 provider(取 `config.yaml` 里的值)—— 本 skill **不硬编码任何厂商**

## 安装

**方式 A — 一行命令(推荐)**:

```bash
hermes skills tap add Hulker309/ralph-goal-loop
```

**方式 B — 手动复制**:

```bash
# 克隆到临时目录
git clone https://github.com/Hulker309/ralph-goal-loop.git /tmp/rgl
# 拷 skill 主体进本地 skills 目录
cp -r /tmp/rgl/* ~/.hermes/skills/ralph-goal-loop/
# 验证
hermes skills list | grep ralph-goal-loop
```

## Quick start

**前置:** 已有一份 prd.json(可手写,可拷模板)。

### 步骤 1:写 prd.json

最小可跑 schema(对齐 mikeyobrien/ralph::prd.json.example):

```json
{
  "branchName": "ralph-goal-loop/hello-world",
  "title": "Hello-world 3-story demo",
  "description": "Three tiny Python tasks to validate ralph-goal-loop end-to-end.",
  "userStories": [
    {
      "id": "US-1",
      "title": "Create greet.py with greet(name) function",
      "priority": 1,
      "passes": false,
      "acceptanceCriteria": [
        "File greet.py exists at the fixture root",
        "greet.py defines greet(name) returning 'Hello, {name}!'",
        "Running `python greet.py` exits 0 and prints 'Hello, Hermes!'",
        "test_greet.py exists and `python -m unittest test_greet` reports OK"
      ]
    }
  ]
}
```

### 步骤 2:准备 progress.txt

```bash
touch progress.txt
```

### 步骤 3:启动 loop

```bash
cd /path/to/your/project
python scripts/ralph.py --prd ./prd.json --project-root .
```

换模型:

```bash
python scripts/ralph.py --prd ./prd.json --project-root . \
  --worker-provider <provider> --worker-model <model>
```

## 工作原理

```
RalphGoalLoop.run()
  ├─ 读 prd.json
  ├─ 按 priority + dependsOn 排序,选下一批 story
  ├─ 对每个 story: HermesAdapter.run_story()
  │    └─ AIAgent.run_conversation(worker_prompt)
  │         └─ worker 实施 story,写 prd.json + progress.txt
  ├─ detect_promise() 检查 worker 输出是否含 <promise>COMPLETE</promise>
  └─ 重复,直到全部 passes: true 或 max_iterations 到
```

worker prompt 告知 agent:
1. 读自己的 prd.json story
2. 读 progress.txt 的 `## Codebase Patterns` 节
3. 按 acceptanceCriteria 实施
4. 标记 `passes: true`
5. 追加 `## [StoryID]` 到 progress.txt
6. 如果全部 story 过了,echo `<promise>COMPLETE</promise>`

## CLI 参数

| Flag | Default | Purpose |
|---|---|---|
| `--prd` | (必填) | `prd.json` 路径 |
| `--project-root` | cwd | 项目根目录 |
| `--max-iter` | `10` | 最大轮数 |
| `--cost-cap` | `5.0` | 累计 cost 硬卡(USD) |
| `--worker-provider` | config.yaml | 换 provider |
| `--worker-model` | config.yaml | 换模型 |
| `--parallel-mode` | `auto` | 分组策略:`off`/`priority`/`auto`/`manual` |
| `--max-parallel` | `10` | 单批 story 上限 |
| `--max-story-attempts` | `3` | 失败几次后 bench;`0` = 不 bench |

## 退出码

| Status | Exit | 含义 |
|---|---|---|
| `ALL_PASSES` | 0 | 全部 `passes: true` 或收到 `<promise>COMPLETE</promise>` |
| `MAX_ITERATIONS` | 2 | 轮数到上限,仍有 pending story |
| `COST_CAP` | 3 | cost 到上限 |
| `DELEGATION_FAILED` | 6 | `adapter.run_story()` 抛异常 |
| `RUN_PAUSED` | 7 | 暂停后重跑(同 `--prd`) |
| `INTERNAL_ERROR` | 8 | 未捕获异常 |
| `NO_PROGRESS` | 10 | 活死锁(依赖成环) |
| `STORIES_EXHAUSTED` | 11 | 死故事(被 bench 或依赖链有问题) |

## FAQ / Troubleshooting

**Q:怎么暂停?**
A:`Ctrl-C` 或 `SIGTERM` 优雅停止。下次同 `--prd` 重跑,agent 从磁盘加载历史记录继续。

**Q:进度存在哪?**
A:`prd.json`(各 story 的 `passes` 字段) + `progress.txt`(append-only 日志 + `## Codebase Patterns` 节)。

**Q:worker 漏写 `<promise>COMPLETE</promise>` 怎么办?**
A:主循环继续 retry,直到 `max_iterations` 卡死。worker prompt 模板要求必须写。

**Q:一个 story 永远跑不通,卡住全部?**
A:v0.4 的 priority 饥饿 bug 已修。现在失败满 `--max-story-attempts` 次后 bench,其余 story 继续。

**Q:跟 `/goal` 什么关系?**
A:没关系。v1.0 不借 `/goal` judge LLM,只用一个 `AIAgent` 实例跑所有 story。

## 测试

```bash
python scripts/tests/test_parallel.py
python scripts/tests/test_starvation.py
python -m unittest discover scripts/tests/ -v
```

## 扩展到其他平台

Ralph 的编排逻辑(RalphCore)与平台执行解耦。要接新平台:

1. 在 `scripts/adapters/` 下新建 `your_platform.py`,实现 `PlatformAdapter` 抽象接口:
   - `run_story(story, context)` — 用平台 API 执行 story,返回 `{summary, tokens, tool_calls}`
   - `run_orchestrator_turn(prompt, history)` — 同上但用于 orchestrator turn
   - `get_history()` / `save_history(history)` — 持久化,支持重启恢复
2. 在 `scripts/ralph.py` 的 argparse 加 `--platform your_platform` 选项
3. 构造 adapter 时做 platform 分支
4. 记得检测 `<promise>COMPLETE</promise>` — 这是唯一退出信号

现有 stub:`scripts/adapters/openclaw.py` — 接 OpenCLAW 时照此模板填即可。

## Links

- 架构设计:[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 仓库:https://github.com/Hulker309/ralph-goal-loop
- mikeyobrien/ralph 原版:https://github.com/mikeyobrien/ralph
