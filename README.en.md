# ralph-goal-loop

> Run a multi-story PRD inside Hermes: serial across `priority` levels, parallel fan-out within each priority, until every story is `passes: true` — **no external CLI, 100% in-process**.

![Python ≥3.11](https://img.shields.io/badge/python-≥3.11-blue)
![MIT-ready](https://img.shields.io/badge/license-MIT--ready-yellow)
![Hermes v0.21.3+](https://img.shields.io/badge/hermes-v0.21.3%2B-purple)

## 概述 / Overview

`ralph-goal-loop` is a 1:1 in-process port of the
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) for the Hermes
Agent runtime. It takes a `prd.json` with multiple user stories and runs them
priority-by-priority: serial across priorities, parallel fan-out within each
priority level, until every story is `passes: true`. The execution engine is
Hermes' own `/goal` slash command (judge LLM + state persistence) and the
per-priority worker fan-out is `delegate_task(tasks=[…])` batch mode. Zero
external Claude Code CLI, zero prompt-cache boundary breakage, zero
`hermes-agent/` core modifications.

## 它是什么 / What it is

A 1:1 port of Geoffrey Huntley's
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) into Hermes.
Underlying primitives are **only two**:

| Primitive | Role |
|---|---|
| **Hermes `/goal`** | Judge engine (`GoalManager` + `judge_goal()` + `state_meta` SQLite persistence + `pause/resume/clear`) |
| **`delegate_task(tasks=[…])`** | Per-priority parallel fan-out (batch mode, `DaemonThreadPoolExecutor(max_workers=10)`) |

The shell `scripts/ralph.py` does **3 things** in-process:

1. Reads `prd.json`, groups by `min(priority)` to pick the current batch
2. Calls `delegate_task(tasks=[…], parent_agent=self, background=False)` to fan-out the batch's stories to child agents in parallel
3. Calls `goal_manager.evaluate_after_turn(last_response)` for the judge LLM's soft verdict ("continue" / "done")

**0 modifications to `hermes-agent/`**. **0 forked external CLI processes**. **0 prompt-cache boundary breakage**.

## Differences from mikeyobrien/ralph

| Dimension | mikeyobrien/ralph (original) | ralph-goal-loop |
|---|---|---|
| **Outer loop** | bash `for i in $(seq 1 $MAX_ITERATIONS)`, each round forks a Claude Code subprocess | `GoalManager` state machine inside Hermes process + judge LLM soft verdict |
| **Inner fan-out** | Multiple `Task` tool_use within one Claude Code subprocess | `delegate_task(tasks=[N≥2])` via `DaemonThreadPoolExecutor` automatic fan-out |
| **Completion check** | `grep '<promise>COMPLETE</promise>'` against stdout string | Worker echoes `<promise>` + `judge_goal()` LLM double-verdict, either triggers exit |
| **State persistence** | `prd.json` + `progress.txt` files only | `prd.json` + `progress.txt` + `SessionDB.state_meta(goal:<session_id>)` SQLite |
| **Pausing** | Restart the script | `goal_manager.pause(...)` + Hermes `/goal resume` |
| **Mid-flight review** | Edit `prd.json` directly | Orchestrator pauses after prd expansion: `pause("等待老板 review")`, boss `/goal resume` to continue |

## Prerequisites

- **Hermes Agent v0.21.3+** (this skill uses `/goal` v0.13.0+ "Tenacity Release" first-class primitives)
- **Python 3.11+** (uses `tomllib` + `Task` group + `ExceptionGroup`)
- **LLM provider** configured in Hermes (MiniMax-M3 / OpenAI / Anthropic / OpenRouter / Gemini / local Ollama)
- Optional: external Claude Code CLI or other worker LLM — **not** called by default, all workers run as child agents inside Hermes

## Installation

**Option A — one-liner (recommended)**:

```bash
hermes skills tap add Hulker309/ralph-goal-loop
```

**Option B — manual copy**:

```bash
# Clone to a temp dir
git clone https://github.com/Hulker309/ralph-goal-loop.git /tmp/rgl
# Copy skill body + references into local skills dir
cp -r /tmp/rgl/* ~/.hermes/skills/ralph-goal-loop/
# Verify
hermes skills list | grep ralph-goal-loop
```

## Quick start

**Prereq:** You already have a `prd.json` (hand-written, copied from the fixture below, or auto-expanded by `/goal draft`).

### Step 1: Write `prd.json`

Minimal runnable schema (aligned with mikeyobrien/ralph::prd.json.example):

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

Full 3-story template: `Desktop/hermes-ralph-goal-loop-test/prd.json` (or `references/` demo fixture in this repo).

### Step 2: Prepare `CLAUDE.md` (worker prompt)

Worker child agents must receive a `CLAUDE.md` to do work. See `CLAUDE.md.tmpl` in this skill, or copy `Desktop/ralph-goal-loop-e2e/CLAUDE.md` (a runnable end-to-end instance). Minimal skeleton:

```markdown
# Worker instructions for ralph-goal-loop story

You are 1 worker running 1 user story. Read prd.json, find your story by id,
implement ONLY that story's acceptanceCriteria (no scope creep), create the
required files, run the self-tests, mark `passes: true` in prd.json,
append `## [YourStoryID]` to progress.txt, then **MUST** echo
`<promise>COMPLETE</promise>` on the last line of your response.
```

### Step 3: Prepare `progress.txt`

```bash
touch progress.txt
```

Workers will append `## [Story ID]` sections as they finish, serving as a cross-story context log.

### Step 4: Launch the loop

```bash
cd /path/to/your/prd-folder
python ~/.hermes/skills/ralph-goal-loop/scripts/ralph.py --prd ./prd.json
# Or inside Hermes: hermes ralph --prd ./prd.json
```

Equivalent one-liner:

```bash
hermes ralph --prd ./prd.json --max-iter 10 --cost-cap 5.0
```

## Full CLI flags

`scripts/ralph.py` argparse full parameter list:

| Flag | Default | Purpose |
|---|---|---|
| `--prd` | (required) | Path to `prd.json`, orchestrator validates existence on launch |
| `--max-iter` | `10` | Outer-loop max rounds; on hit → `MAX_ITERATIONS` exit |
| `--cost-cap` | `5.0` | Hard cap on worker + judge cumulative cost (USD); on hit → `COST_CAP` exit |
| `--session-id` | `None` | Custom `goal:<session_id>` `state_meta` key; omit → Hermes default session |
| `--no-draft` | `False` | Skip `draft_contract()` phase, assume `prd.json` is already written |
| `--judge-provider` | `None` (inherited from `parent_agent`) | Override judge LLM provider, e.g. `openrouter` |
| `--judge-model` | `None` (inherited from `parent_agent`) | Override judge LLM model, e.g. `google/gemini-3-flash-preview` |
| `--judge-base-url` | `None` (inherited from `parent_agent`) | Override judge base_url |
| `--judge-api-key-env` | `None` (inherited from `parent_agent`) | Env var name holding the judge API key (orchestrator reads env then assigns) |
| `--judge-keep-aux-config` | `False` | **Reverse**: ignore CLI flags, lock back to `config.yaml::auxiliary.goal_judge.*` routing |

Full `--help` output:

```text
usage: ralph [-h] --prd PRD [--max-iter MAX_ITER] [--cost-cap COST_CAP]
             [--session-id SESSION_ID] [--no-draft]
             [--judge-provider JUDGE_PROVIDER] [--judge-model JUDGE_MODEL]
             [--judge-base-url JUDGE_BASE_URL]
             [--judge-api-key-env JUDGE_API_KEY_ENV]
             [--judge-keep-aux-config]

ralph-goal-loop orchestrator (Phase 2)
```

## Exit codes

| Status | Exit code | Meaning |
|---|---|---|
| `GOAL_DONE` | 0 | Judge LLM returns `done`, or worker echoes `<promise>COMPLETE</promise>` |
| `ALL_PASSES` | 0 | All stories in `prd.json` are `passes: true`, orchestrator self-check passes |
| `GOAL_BLOCKED` | 4 | Judge reports `blocked`, or `USER_REJECTED_CONTRACT` (boss rejected the GoalContract mid-flight) |
| `MAX_ITERATIONS` | 2 | Outer loop hit `--max-iter` cap with pending stories remaining |
| `COST_CAP` | 3 | Cumulative cost hit `--cost-cap`, immediate abort |
| `USER_REJECTED_PRD` | 5 | Orchestrator paused after prd.json expansion, boss `/goal clear` to abandon |
| `DELEGATION_FAILED` | 6 | `delegate_task(tasks=[…])` threw an internal exception (depth exceeded / pool creation failed) |
| `GOAL_CLEARED` | 7 | Boss `/goal clear` mid-flight; orchestrator detected `not is_active()` |
| `INTERNAL_ERROR` | 8 | Orchestrator itself threw an uncaught exception (prd file missing / parse failure / etc.) |
| `NO_GOAL` | 9 | `goal_manager` state abnormal, no active goal to evaluate |
| `CONTINUE` | (internal) | Judge returns continue, orchestrator enters next retry round |

## Known caveats

1. **Judge LLM defaults to the main conversation model** (same worker world-view) — via `scripts/ralph.py:234-265` `_init_parent_agent` calling `resolve_runtime_provider()` to inherit the parent agent's full 5-tuple. If your worker and judge must use different models, you **must** explicitly pass `--judge-{provider,model,base-url,api-key-env}` or configure `config.yaml::auxiliary.goal_judge.*`.
2. **`<promise>COMPLETE</promise>` is a worker hard protocol** — must be enforced by the `CLAUDE.md` template; otherwise the orchestrator's grep will never hit and you'll deadlock on `MAX_ITERATIONS`.
3. **`delegate_task(tasks=[1])` does NOT fan-out** — `_executor` is only enabled when `len(tasks) ≥ 2` (per `tools/async_delegation.py:573`); single story runs serially inline. This skill accepts that trade-off and returns normally for single-story cases.
4. **`skip_memory` cannot be passed** — `delegate_task` hard-codes child `AIAgent(skip_memory=True)` in `tools/delegate_tool.py:240`; passing `skip_memory=False` will be silently ignored (the kwarg doesn't exist).
5. **Don't edit `prd.json` inside a dirty main checkout** — Ralph's file changes + prd.json changes get mixed into the same commit, making cherry-pick/revert painful. **Use a git worktree**; this skill warns when launched outside one.

## Links

- Architecture design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Repository: https://github.com/Hulker309/ralph-goal-loop
- Hermes `/goal` primitives (`hermes_cli/goals.py:280-1695`): see Hermes Agent docs
- mikeyobrien/ralph original: https://github.com/mikeyobrien/ralph
- Related skills: `kanban-codex-lane` / `openclaw-plugin-author-suite` / `hermes-agent`