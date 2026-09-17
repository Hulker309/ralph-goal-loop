# ralph-goal-loop

> Run a multi-story PRD inside Hermes: **recon grounds each story's acceptance criteria against the real codebase first**, then a caller-chosen grouping strategy (`--parallel-mode`) runs them in parallel until every story is `passes: true` — **no external CLI, 100% in-process**.

![Python ≥3.11](https://img.shields.io/badge/python-≥3.11-blue)
![MIT-ready](https://img.shields.io/badge/license-MIT--ready-yellow)
![Hermes v0.21.3+](https://img.shields.io/badge/hermes-v0.21.3%2B-purple)

## 概述 / Overview

`ralph-goal-loop` is an in-process port of the
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) for the Hermes
Agent runtime. It takes a `prd.json` with multiple user stories, **grounds each
story's acceptance criteria against the real codebase first** (recon), then runs
them through a caller-chosen parallel grouping strategy until every story is
`passes: true`. The execution engine is Hermes' own `/goal` slash command (judge
LLM + state persistence) and the worker fan-out is `delegate_task(tasks=[…])`
batch mode. Zero external Claude Code CLI, zero prompt-cache boundary breakage,
zero `hermes-agent/` core modifications.

## 它是什么 / What it is

A 1:1 port of Geoffrey Huntley's
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) into Hermes.
Underlying primitives are **only two**:

| Primitive | Role |
|---|---|
| **Hermes `/goal`** | Judge engine (`GoalManager` + `judge_goal()` + `state_meta` SQLite persistence + `pause/resume/clear`) |
| **`delegate_task(tasks=[…])`** | Parallel worker fan-out (batch mode, `DaemonThreadPoolExecutor(max_workers=10)`). **The grouping strategy is the caller's choice** — see `--parallel-mode` |

The shell `scripts/ralph.py` does **4 things** in-process:

1. **recon (grounding)**: read-only workers read the real codebase and rewrite each story's `acceptanceCriteria` into verifiable form with real signatures + `path:line` evidence
2. **Batch planning**: decide which stories run together this round, per `--parallel-mode` (default `auto`: `dependsOn` + file overlap)
3. Calls `delegate_task(tasks=[…], parent_agent=self, background=False)` to fan-out the batch's stories to child agents in parallel
4. Calls `goal_manager.evaluate_after_turn(last_response)` for the judge LLM's soft verdict, and merges what the workers reported back into the shared context

**0 modifications to `hermes-agent/`**. **0 forked external CLI processes**. **0 prompt-cache boundary breakage**.

## Differences from mikeyobrien/ralph

| Dimension | mikeyobrien/ralph (original) | ralph-goal-loop |
|---|---|---|
| **Outer loop** | bash `for i in $(seq 1 $MAX_ITERATIONS)`, each round forks a Claude Code subprocess | `GoalManager` state machine inside Hermes process + judge LLM soft verdict |
| **Inner fan-out** | Multiple `Task` tool_use within one Claude Code subprocess | `delegate_task(tasks=[N≥2])` via `DaemonThreadPoolExecutor` automatic fan-out |
| **Inner parallelism** | Multiple `Task` tool_use inside the Claude Code child process | `delegate_task(tasks=[N≥2])` through `DaemonThreadPoolExecutor`; grouping chosen by `--parallel-mode` (`off`/`priority`/`auto`/`manual`) |
| **State persistence** | `prd.json` + `progress.txt` files only | `prd.json` + `progress.txt` + `SessionDB.state_meta(goal:<session_id>)` SQLite |
| **Pausing** | Restart the script | `goal_manager.pause(...)` + Hermes `/goal resume` |
| **Mid-flight review** | Edit `prd.json` directly | Orchestrator pauses after prd expansion: `pause("等待老板 review")`, boss `/goal resume` to continue |
| **Mid-flight review** | Edit the `prd.json` file | Orchestrator writes a `REVIEW_NEEDED:` marker into `progress.txt` then **immediately auto-resumes (non-blocking)**; remove that resume line to make it actually stop |
## Prerequisites

- **Hermes Agent v0.21.3+** (this skill uses `/goal` v0.13.0+ "Tenacity Release" first-class primitives)
- **Python 3.11+** (uses `tomllib` + `Task` group + `ExceptionGroup`)
- **LLM provider**: any provider you have configured in Hermes (whatever `config.yaml` names) — this skill hardcodes no vendor
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
cd /path/to/your/project   # the directory holding prd.json
python ~/.hermes/skills/ralph-goal-loop/scripts/ralph.py --prd ./prd.json --project-root .
# --project-root is where recon's workers read code; defaults to the cwd
```

Switch model / parallel strategy (neither touches your global config):

```bash
python ~/.hermes/skills/ralph-goal-loop/scripts/ralph.py --prd ./prd.json --project-root . \
  --worker-provider <provider> --worker-model <model>   # use values your env really has
```

> ⚠️ **There is no `hermes ralph` subcommand.** It was never implemented and should not be: registering it means editing `hermes_cli/main.py`, which contradicts this skill's "0 core modifications" promise. You run the orchestrator with `python <skill>/scripts/ralph.py`.

## Full CLI flags

`scripts/ralph.py` argparse full parameter list:

| Flag | Default | Purpose |
| Flag | Default | Purpose |
|---|---|---|
| `--prd` | (required) | Path to `prd.json`, orchestrator validates existence on launch |
| `--project-root` | cwd | Root the workers read code from (**recon depends on it**) |
| `--max-iter` | `10` | Outer-loop max rounds; on hit → `MAX_ITERATIONS` exit |
| `--cost-cap` | `5.0` | Hard cap on worker + judge cumulative cost (USD); on hit → `COST_CAP` exit |
| `--parallel-mode` | `auto` | Parallel grouping strategy: `off` / `priority` / `auto` / `manual` |
| `--max-parallel` | `10` | Cap on stories per batch, and the concurrency ceiling |
| `--max-story-attempts` | `3` | Bench a story after this many failed attempts so it stops blocking the rest; `0` = never bench |
| `--no-recon` | `False` | Skip the recon pass — stories keep their paper acceptance criteria |
| `--recon-group` | `3` | Stories handed to one recon worker |
| `--session-id` | `None` | Custom `goal:<session_id>` `state_meta` key; omit → Hermes default session |
| `--no-draft` | `False` | Skip `draft_contract()` phase, assume `prd.json` is already written |
| `--worker-provider` | config.yaml | Run this loop on a different provider (global config untouched) |
| `--worker-model` | config.yaml | Run this loop on a different model |
| `--judge-provider` | `None` (inherited from `parent_agent`) | Override judge LLM provider |
| `--judge-model` | `None` (inherited from `parent_agent`) | Override judge LLM model |
| `--judge-base-url` | `None` (inherited from `parent_agent`) | Override judge base_url |
| `--judge-api-key-env` | `None` (inherited from `parent_agent`) | Env var name holding the judge API key |
| `--judge-keep-aux-config` | `False` | **Reverse**: ignore CLI flags, lock back to `config.yaml::auxiliary.goal_judge.*` routing |
Full `--help` output:

```text
usage: ralph [-h] --prd PRD [--max-iter MAX_ITER] [--cost-cap COST_CAP]
             [--session-id SESSION_ID] [--no-draft] [--no-recon]
             [--recon-group RECON_GROUP] [--project-root PROJECT_ROOT]
             [--worker-provider WORKER_PROVIDER] [--worker-model WORKER_MODEL]
             [--parallel-mode {off,priority,auto,manual}]
             [--max-parallel MAX_PARALLEL]
             [--max-story-attempts MAX_STORY_ATTEMPTS]
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
| `NO_GOAL` | 9 | `goal_manager` state abnormal, no active goal to evaluate |
| `NO_PROGRESS` | 10 | **Live deadlock**: stories exist but none can run now (typically a `dependsOn` cycle). Exits immediately instead of spinning |
| `STORIES_EXHAUSTED` | 11 | **Dead stories**: the remainder can never finish (benched, or the dependency chain names a non-existent / benched story). Names what died and why |

## Known caveats

1. **Judge LLM defaults to the main conversation model** (same worker world-view) — via `scripts/ralph.py:234-265` `_init_parent_agent` calling `resolve_runtime_provider()` to inherit the parent agent's full 5-tuple. If your worker and judge must use different models, you **must** explicitly pass `--judge-{provider,model,base-url,api-key-env}` or configure `config.yaml::auxiliary.goal_judge.*`.
2. **`<promise>COMPLETE</promise>` is a worker hard protocol** — must be enforced by the `CLAUDE.md` template; otherwise the orchestrator's grep will never hit and you'll deadlock on `MAX_ITERATIONS`.
3. **`delegate_task(tasks=[1])` does NOT parallelise** — batch mode engages only at `len(tasks) ≥ 2` (`tools/async_delegation.py`). That is expected, but the consequence is real: **if every story in `prd.json` has its own priority, `priority` mode yields batches of exactly 1 and the fan-out never fires**. To get parallelism, group non-conflicting stories together when you split the prd, or use the default `--parallel-mode auto` (groups by file overlap, which needs recon's file info).
4. **`skip_memory` cannot be passed** — `delegate_task` hard-codes child `AIAgent(skip_memory=True)` in `tools/delegate_tool.py:240`; passing `skip_memory=False` will be silently ignored (the kwarg doesn't exist).
5. **Don't edit `prd.json` inside a dirty main checkout** — Ralph's file changes + prd.json changes get mixed into the same commit, making cherry-pick/revert painful. **Use a git worktree**; this skill warns when launched outside one.

## Links

- Architecture design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Repository: https://github.com/Hulker309/ralph-goal-loop
- Hermes `/goal` primitives (`hermes_cli/goals.py:280-1695`): see Hermes Agent docs
- mikeyobrien/ralph original: https://github.com/mikeyobrien/ralph
- Related skills: `kanban-codex-lane` / `openclaw-plugin-author-suite` / `hermes-agent`