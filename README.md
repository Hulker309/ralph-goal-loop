# ralph-goal-loop

> 在 Hermes 内部跑多 story PRD:按 `priority` 顺序串行、同 priority 内 fan-out 并行,直至 `passes: true` 全员到齐 — **不调外部 CLI,100% Hermes 进程内**。

![Python ≥3.11](https://img.shields.io/badge/python-≥3.11-blue)
![MIT-ready](https://img.shields.io/badge/license-MIT--ready-yellow)
![Hermes v0.21.3+](https://img.shields.io/badge/hermes-v0.21.3%2B-purple)

## English summary

`ralph-goal-loop` is a 1:1 in-process port of the
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) for the Hermes
Agent runtime. It takes a `prd.json` with multiple user stories and runs them
priority-by-priority: serial across priorities, parallel fan-out within each
priority level, until every story is `passes: true`. The execution engine is
Hermes' own `/goal` slash command (judge LLM + state persistence) and the
per-priority worker fan-out is `delegate_task(tasks=[…])` batch mode. Zero
external Claude Code CLI, zero prompt-cache boundary breakage, zero
`hermes-agent/` core modifications.

→ [English version](README.en.md) · [Architecture design](docs/ARCHITECTURE.md)

## 它是什么

`ralph-goal-loop` 把 Geoffrey Huntley 的
[Ralph Wiggum 技术](https://github.com/mikeyobrien/ralph) 移植到 Hermes 内部。
底层**只借两个上游原语**:

| 原语 | 作用 |
|---|---|
| **Hermes `/goal`** | judge 引擎(`GoalManager` + `judge_goal()` + state_meta SQLite 持久化 + `pause/resume/clear`) |
| **`delegate_task(tasks=[…])`** | 同 priority 内并行 fan-out(走 batch mode,`DaemonThreadPoolExecutor(max_workers=10)`) |

外壳的 `scripts/ralph.py` 在 Hermes 进程内做**3 件事**:

1. 读 `prd.json`,按 `min(priority)` 分组挑出当前 batch
2. 调 `delegate_task(tasks=[…], parent_agent=self, background=False)` 把 batch 内 story 并行甩给子 agent
3. 调 `goal_manager.evaluate_after_turn(last_response)` 让 judge LLM 软判定「继续 / 完成」

**0 改 `hermes-agent/`**。**0 fork 外部 CLI 进程**。**0 破 prompt caching 边界**。

## 跟 mikeyobrien/ralph 区别

| 维度 | mikeyobrien/ralph 原版 | ralph-goal-loop |
|---|---|---|
| **外层循环** | bash `for i in $(seq 1 $MAX_ITERATIONS)`,每轮 fork Claude Code 子进程 | Hermes 进程内 `GoalManager` 状态机 + judge LLM 软判定 `done` |
| **内层并行** | Claude Code 子进程内多个 `Task` tool_use | `delegate_task(tasks=[N≥2])` 走 `DaemonThreadPoolExecutor` 自动 fan-out |
| **完成判定** | `grep '<promise>COMPLETE</promise>'` stdout 字符串 | worker echo `<promise>` + `judge_goal()` LLM 双判定,任一触发即退 |
| **状态持久化** | `prd.json` + `progress.txt` 文件 | `prd.json` + `progress.txt` + `SessionDB.state_meta(goal:<session_id>)` SQLite |
| **可暂停** | 重启脚本 | `goal_manager.pause(...)` + Hermes `/goal resume` |
| **可中途 review** | 改 `prd.json` 文件 | orchestrator 在拆完 prd 后 `pause("等待老板 review")`,老板 `/goal resume` 继续 |

## 前置要求

- **Hermes Agent v0.21.3+**(本 skill 借 `/goal` v0.13.0+「Tenacity Release」起的 first-class 原语)
- **Python 3.11+**(用了 `tomllib` + `Task` group + `ExceptionGroup`)
- **LLM provider**:任何 Hermes 配置过的 provider(MiniMax-M3 / OpenAI / Anthropic / OpenRouter / Gemini / 本地 Ollama)
- 可选:**外部 Claude Code CLI 或其他 worker LLM** —— 默认**不调**,所有 worker 都在 Hermes 进程内起

## 安装

**方式 A — 一行命令(推荐)**:

```bash
hermes skills tap add Hulker309/ralph-goal-loop
```

**方式 B — 手动复制**:

```bash
# 克隆到临时目录
git clone https://github.com/Hulker309/ralph-goal-loop.git /tmp/rgl
# 拷 skill 主体 + reference 资料进本地 skills 目录
cp -r /tmp/rgl/* ~/.hermes/skills/ralph-goal-loop/
# 验证
hermes skills list | grep ralph-goal-loop
```

## Quick start

**前置:** 已有一份 prd.json(可手写,可拷下面的 fixture,可让 `/goal draft` 自动扩)。

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

完整 3-story 模板见 `Desktop/hermes-ralph-goal-loop-test/prd.json`(或本仓库 `references/` 下的 demo fixture)。

### 步骤 2:准备 CLAUDE.md(worker prompt)

worker 子 agent 必须收到 `CLAUDE.md` 才能干活。模板见本 skill 的 `CLAUDE.md.tmpl`,或拷 `Desktop/ralph-goal-loop-e2e/CLAUDE.md`(端到端跑通的实例)。最小骨架:

```markdown
# Worker instructions for ralph-goal-loop story

You are 1 worker running 1 user story. Read prd.json, find your story by id,
implement ONLY that story's acceptanceCriteria (no scope creep), create the
required files, run the self-tests, mark `passes: true` in prd.json,
append `## [YourStoryID]` to progress.txt, then **MUST** echo
`<promise>COMPLETE</promise>` on the last line of your response.
```

### 步骤 3:准备 progress.txt

```bash
touch progress.txt
```

worker 跑通后会按 `## [Story ID]` 段追加,作为跨 story 的上下文日志。

### 步骤 4:启动 loop

```bash
cd /path/to/your/prd-folder
python ~/.hermes/skills/ralph-goal-loop/scripts/ralph.py --prd ./prd.json
# 或在 Hermes 内用: hermes ralph --prd ./prd.json
```

等价的 quick start 一行:

```bash
hermes ralph --prd ./prd.json --max-iter 10 --cost-cap 5.0
```

## CLI 完整参数表

`scripts/ralph.py` 的 argparse 全参数:

| Flag | Default | Purpose |
|---|---|---|
| `--prd` | (必填) | `prd.json` 路径,orchestrator 启动时校验存在 |
| `--max-iter` | `10` | 外层循环最大轮数,到即 `MAX_ITERATIONS` 退出 |
| `--cost-cap` | `5.0` | worker + judge 累计 cost(USD)硬卡,到即 `COST_CAP` 退出 |
| `--session-id` | `None` | 自定义 `goal:<session_id>` state_meta key;不传走 Hermes 默认 session |
| `--no-draft` | `False` | 跳过 `draft_contract()` 阶段,假定 prd.json 已经手写好 |
| `--judge-provider` | `None`(继承 parent_agent) | 覆盖 judge LLM provider,如 `openrouter` |
| `--judge-model` | `None`(继承 parent_agent) | 覆盖 judge LLM model,如 `google/gemini-3-flash-preview` |
| `--judge-base-url` | `None`(继承 parent_agent) | 覆盖 judge base_url |
| `--judge-api-key-env` | `None`(继承 parent_agent) | judge api key 所在 env 变量名(orchestrator 读 env 后再赋) |
| `--judge-keep-aux-config` | `False` | **反向**:无视 CLI flags,锁回 `config.yaml::auxiliary.goal_judge.*` 路由 |

完整 `--help` 输出:

```text
usage: ralph [-h] --prd PRD [--max-iter MAX_ITER] [--cost-cap COST_CAP]
             [--session-id SESSION_ID] [--no-draft]
             [--judge-provider JUDGE_PROVIDER] [--judge-model JUDGE_MODEL]
             [--judge-base-url JUDGE_BASE_URL]
             [--judge-api-key-env JUDGE_API_KEY_ENV]
             [--judge-keep-aux-config]

ralph-goal-loop orchestrator (Phase 2)
```

## 退出码表

| Status | Exit code | 含义 |
|---|---|---|
| `GOAL_DONE` | 0 | judge LLM 判 `done`,或 worker echo `<promise>COMPLETE</promise>` |
| `ALL_PASSES` | 0 | `prd.json` 所有 story `passes: true`,orchestrator 读 prd 自检通过 |
| `GOAL_BLOCKED` | 4 | judge 报 `blocked`,或 `USER_REJECTED_CONTRACT`(老板中途拒掉 GoalContract) |
| `MAX_ITERATIONS` | 2 | 外层循环到 `--max-iter` 上限,仍有 pending story |
| `COST_CAP` | 3 | 累计 cost 到 `--cost-cap`,立即 abort |
| `USER_REJECTED_PRD` | 5 | orchestrator 拆完 prd.json 后 pause,老板 `/goal clear` 放弃 |
| `DELEGATION_FAILED` | 6 | `delegate_task(tasks=[…])` 内部抛异常(depth 超限 / pool 创建失败) |
| `GOAL_CLEARED` | 7 | 老板中途 `/goal clear`,orchestrator 检测到 `not is_active()` |
| `INTERNAL_ERROR` | 8 | orchestrator 自己抛未捕获异常(prd 文件不存在 / 解析失败等) |
| `NO_GOAL` | 9 | `goal_manager` 状态异常,无 active goal 可 evaluate |
| `CONTINUE` | (内部) | judge 判 continue,orchestrator 进入下一轮 retry |

## 已知 caveat

1. **judge LLM 默认走主对话模型**(同 worker world-view)—— 通过 `scripts/ralph.py:234-265` `_init_parent_agent` 调 `resolve_runtime_provider()` 继承父 agent 的完整 5 元组。如果你的 worker 跟 judge 必须用不同 model,**必须**显式传 `--judge-{provider,model,base-url,api-key-env}` 或在 `config.yaml::auxiliary.goal_judge.*` 配。
2. **`<promise>COMPLETE</promise>` 是 worker 硬协议** —— 必须由 `CLAUDE.md` 模板强制要求,否则 orchestrator 永远 grep 不命中 → `MAX_ITERATIONS` 卡死。
3. **`delegate_task(tasks=[1])` 不会 fan-out** —— `_executor` 只在 `len(tasks) ≥ 2` 时启用(per `tools/async_delegation.py:573`),单 story 走串行 inline。本 skill 接受这个 trade-off,1 story 时正常返回。
4. **`skip_memory` 不能传** —— `delegate_task` 在 `tools/delegate_tool.py:240` 硬编码 child `AIAgent(skip_memory=True)`,传 `skip_memory=False` 会被忽略(无此 kwarg)。
5. **prd.json 不在 dirty 主 checkout 里改** —— Ralph 实施文件改动 + prd.json 改动混在同次 commit 里,cherry-pick/revert 麻烦。**请用 git worktree**,本 skill 启动时检测到不在 worktree 内会 warn。

## Links

- 架构设计:[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 仓库:https://github.com/Hulker309/ralph-goal-loop
- Hermes `/goal` 原语(`hermes_cli/goals.py:280-1695`):见 Hermes Agent 文档
- mikeyobrien/ralph 原版:https://github.com/mikeyobrien/ralph
- 相关 skill:`kanban-codex-lane` / `openclaw-plugin-author-suite` / `hermes-agent`