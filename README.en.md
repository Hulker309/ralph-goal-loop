# ralph-goal-loop

> Run a multi-story PRD inside Hermes: execute stories in priority + `dependsOn`
order until every story is `passes: true`. Ralph runs as a separate Python
process; the boss's main Hermes session is completely unaffected.

![Python ≥3.11](https://img.shields.io/badge/python-≥3.11-blue)
![MIT-ready](https://img.shields.io/badge/license-MIT--ready-yellow)
![Hermes v0.21.3+](https://img.shields.io/badge/hermes-v0.21.3%2B-purple)

## Overview

`ralph-goal-loop` is an in-process port of the
[Ralph Wiggum technique](https://github.com/mikeyobrien/ralph) for the Hermes
Agent runtime. It reads a `prd.json` with multiple user stories, runs them in
priority + `dependsOn` order via a persistent `AIAgent`, and exits only when
every story is `passes: true` — signaled by the literal token
`<promise>COMPLETE</promise>`.

Architecture is two-layer:

| Layer | Component | Responsibility |
|---|---|---|
| **Orchestration** | `RalphGoalLoop` (`scripts/core/orchestrator.py`) | Pure Python, no Hermes imports. Reads `prd.json`, picks the next batch by priority + `dependsOn`, calls `adapter.run_story()` |
| **Execution** | `HermesAdapter` (`scripts/adapters/hermes.py`) | Holds one `AIAgent` instance; `run_story()` builds a worker prompt and calls `agent.run_conversation()` |

**Difference from upstream Ralph**: upstream is a bash loop that forks a Claude Code subprocess each round; this skill uses the same `AIAgent` instance for all stories, accumulating history in one session.

**Difference from v0.4.0**: v0.4 used Hermes `/goal` + judge LLM + `delegate_task` fan-out; v1.0 removes these, keeping only `AIAgent.run_conversation()` — a more direct port of upstream Ralph.

## Prerequisites

- **Hermes Agent v0.21.3+**
- **Python 3.11+** (uses `tomllib` + `ExceptionGroup`)
- **LLM provider**: any provider configured in Hermes (from `config.yaml`) — this skill hardcodes no vendor

## Installation

**Option A — one-liner (recommended)**:

```bash
hermes skills tap add Hulker309/ralph-goal-loop
```

**Option B — manual copy**:

```bash
git clone https://github.com/Hulker309/ralph-goal-loop.git /tmp/rgl
cp -r /tmp/rgl/* ~/.hermes/skills/ralph-goal-loop/
hermes skills list | grep ralph-goal-loop
```

## Quick start

**Prereq:** You have a `prd.json` (hand-written or copied from the fixture below).

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

### Step 2: Prepare `progress.txt`

```bash
touch progress.txt
```

### Step 3: Launch the loop

```bash
cd /path/to/your/project
python scripts/ralph.py --prd ./prd.json --project-root .
```

Switch model:

```bash
python scripts/ralph.py --prd ./prd.json --project-root . \
  --worker-provider <provider> --worker-model <model>
```

## How it works

```
RalphGoalLoop.run()
  ├─ read prd.json
  ├─ pick next batch by priority + dependsOn
  ├─ for each story: HermesAdapter.run_story()
  │    └─ AIAgent.run_conversation(worker_prompt)
  │         └─ worker implements story, writes prd.json + progress.txt
  ├─ detect_promise() checks worker output for <promise>COMPLETE</promise>
  └─ repeat until all passes: true or max_iterations hit
```

Worker prompt tells the agent to:
1. Read its prd.json story
2. Read progress.txt's `## Codebase Patterns` section
3. Implement by acceptanceCriteria
4. Mark `passes: true`
5. Append `## [StoryID]` to progress.txt
6. If all stories done, echo `<promise>COMPLETE</promise>`

## CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--prd` | (required) | Path to `prd.json` |
| `--project-root` | cwd | Project root directory |
| `--max-iter` | `10` | Max rounds |
| `--cost-cap` | `5.0` | Hard cap on cumulative cost (USD) |
| `--worker-provider` | config.yaml | Override provider |
| `--worker-model` | config.yaml | Override model |
| `--parallel-mode` | `auto` | Grouping strategy: `off`/`priority`/`auto`/`manual` |
| `--max-parallel` | `10` | Max stories per batch |
| `--max-story-attempts` | `3` | Bench story after this many failures; `0` = never bench |

## Exit codes

| Status | Exit | Meaning |
|---|---|---|
| `ALL_PASSES` | 0 | All `passes: true` or `<promise>COMPLETE</promise>` received |
| `MAX_ITERATIONS` | 2 | Round cap hit with pending stories |
| `COST_CAP` | 3 | Cost cap hit |
| `DELEGATION_FAILED` | 6 | `adapter.run_story()` threw |
| `RUN_PAUSED` | 7 | Paused; re-run with same `--prd` to resume |
| `INTERNAL_ERROR` | 8 | Uncaught exception |
| `NO_PROGRESS` | 10 | Live deadlock (dependsOn cycle) |
| `STORIES_EXHAUSTED` | 11 | Dead stories (benched or broken dependency chain) |

## FAQ / Troubleshooting

**Q: How do I pause?**
A: `Ctrl-C` or `SIGTERM` for graceful stop. Re-run with the same `--prd` and the agent loads history from disk and resumes.

**Q: Where does progress go?**
A: `prd.json` (each story's `passes` field) + `progress.txt` (append-only log + `## Codebase Patterns` section).

**Q: Worker forgot to write `<promise>COMPLETE</promise>`?**
A: Main loop keeps retrying until `max_iterations`. The worker prompt template requires it.

**Q: One story blocks all the others forever?**
A: The v0.4 priority-starvation bug is fixed. Failed stories bench after `--max-story-attempts` failures; the rest continue.

**Q: What's the relationship with `/goal`?**
A: None. v1.0 does not use `/goal` or a judge LLM. It uses one `AIAgent` instance for everything.

## Testing

```bash
python scripts/tests/test_parallel.py
python scripts/tests/test_starvation.py
python -m unittest discover scripts/tests/ -v
```

## Links

- Architecture design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Repository: https://github.com/Hulker309/ralph-goal-loop
- mikeyobrien/ralph original: https://github.com/mikeyobrien/ralph
