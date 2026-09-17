# PRD: ralph-goal-loop v1.0 — True-Fidelity Port of Ralph to Hermes

**Date**: 2026-09-18
**Author**: Hermes Agent (希尔)
**Status**: Draft
**Supersedes**: v0.4.0 (scripts/ralph.py 1154 lines, /goal-driven)

---

## 1. Introduction / Overview

`ralph-goal-loop` is a Hermes-side port of `mikeyobrien/ralph`. v0.4.0 drifted from Ralph's architecture by adopting the Hermes `/goal` engine (judge LLM + state persistence), a recon worker, and `delegate_task` subagents as separate mechanisms.

The boss's 2026-09-17 diagnostic report established that this drift is not incidental — `/goal` is incompatible with Ralph because `/goal` returns one verdict about one goal, but Ralph has many stories each with its own `passes` state and cross-iteration knowledge accumulation. Recon exists only to compensate for "fresh child agent loses context," which is a problem we created by using subagents.

**This PRD rewrites the skill to its core architecture**: orchestrator becomes a single persistent AIAgent that reads the real project each turn, uses `<promise>COMPLETE</promise>` as the only completion signal, and treats Hermes as the in-process execution engine — not as the goal-tracking engine.

**Two-platform support** is preserved via `PlatformAdapter`: Hermes is implemented in this PRD, OpenCLAW is a TODO stub.

---

## 2. Goals

- **G-1**: Behavior on Hermes is equivalent to upstream Ralph (`mikeyobrien/ralph`) — same completion protocol, same state files, same iteration loop, same failure semantics.
- **G-2**: Ralph's core algorithm is preserved verbatim in `RalphCore` (pure Python, no Hermes imports). It compiles + tests + runs identically regardless of platform.
- **G-3**: Adding a new platform requires implementing one class (`PlatformAdapter`), not rewriting orchestration logic.
- **G-4**: The orchestrator (`AIAgent`) reads the real project each turn via its CWD — no prompt-cached interface inventory injected upfront, no recon pass, no judge LLM.
- **G-5**: All v0.3.0 parallelism planner logic and v0.4.0 starvation-fix logic remains in `RalphCore` (dependsOn gating, priority ordering, benching, dead-story detection).
- **G-6**: Background-process execution model: `python scripts/ralph.py --prd ...` runs to completion as a separate process. User's main `hermes` session is unaffected; progress is visible via `prd.json` + `progress.txt`.
- **G-7**: Zero modifications to Hermes core (`hermes_cli/`, `run_agent.py`, `tools/delegate_tool.py`, `agent/`).

---

## 3. Non-Goals (Out of Scope)

- ❌ **OpenCLAW adapter implementation** — TODO stub only. (Architecture supports it; no behavior.)
- ❌ **Concurrent story execution** — single AIAgent session, sequential per-priority. Ralph's original is also sequential per iteration; concurrency was the v0.3.0 deviation we are removing.
- ❌ **Backward compatibility with `/goal`-specific flags** — `--judge-*`, `--no-draft`, `--session-id` are dropped. v0.4.0 callers update their invocations.
- ❌ **Real-cost token estimation** — keep v0.4.0's hardcoded `_WORKER_IN_USD_PER_1K = 0.003` / `_WORKER_OUT_USD_PER_1K = 0.015` constants.
- ❌ **Multi-machine or cross-profile parallelism**.
- ❌ **Recon / pre-pass interface grounding** — orchestrator reads project files itself each turn.
- ❌ **Judge LLM** — orchestrator's own `<promise>` detection is the only completion signal. No `_call_goal_judge_llm`, no `GoalManager.evaluate_after_turn`, no `auxiliary.goal_judge` config.
- ❌ **Auto-resume boss review nodes** — v0.1.0's auto-resume hack goes away; if the boss wants to review mid-run, they pause the background process and resume it manually.

---

## 4. Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                       RalphCore (pure Python)                  │
│                                                                │
│   ┌─────────────┐  ┌──────────────┐  ┌──────────────┐         │
│   │ prd.py      │  │ progress.py  │  │ promise.py   │         │
│   │ (read/write │  │ (append-only │  │ (regex match │         │
│   │  prd.json + │  │  log + ##    │  │  for literal │         │
│   │  normalize) │  │  Codebase    │  │  <promise>   │         │
│   │             │  │  Patterns    │  │  COMPLETE    │         │
│   │             │  │  merge)      │  │  </promise>) │         │
│   └─────────────┘  └──────────────┘  └──────────────┘         │
│                                                                │
│   ┌──────────────────────────────────────────────────────┐    │
│   │ orchestrator.py (RalphGoalLoop)                      │    │
│   │   - outer iteration loop (max_iter)                  │    │
│   │   - per-iteration: pick next story → call            │    │
│   │     adapter.run_story(story, context) → parse result │    │
│   │   - <promise> detection → exit                       │    │
│   │   - cost cap check → exit                            │    │
│   │   - starvation handling: benching, dead-story detect │    │
│   │   - parallelism planner (--parallel-mode)            │    │
│   └──────────────────────────────────────────────────────┘    │
│                                                                │
└───────────────────────────┬────────────────────────────────────┘
                            │
                  ┌─────────▼─────────┐
                  │  PlatformAdapter  │  (abstract base class)
                  │  interface:       │
                  │   run_story()     │
                  │   run_orchestrator_turn()  ← returns assistant message
                  │   get_history()   │
                  │   save_history()  │
                  └─────────┬─────────┘
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
       ┌──────────────────┐   ┌──────────────────────┐
       │ HermesAdapter    │   │ OpenCLAWAdapter      │
       │ (THIS PRD)       │   │ (TODO)               │
       │                  │   │ class OpenCLAWAdapter│
       │ AIAgent +        │   │   ...                │
       │ run_conversation │   │   raise              │
       │   (...)          │   │   NotImplementedError│
       └──────────────────┘   └──────────────────────┘
```

### Why this architecture

**`RalphCore` is Ralph, period.** When the orchestrator runs, it does exactly what `mikeyobrien/ralph`'s bash loop does:

1. Read `prd.json` → identify highest-priority pending story whose `dependsOn` are all `passes: true`.
2. Hand the story to the adapter.
3. Adapter returns the assistant's response text (and any tool calls it made).
4. Orchestrator checks for `<promise>COMPLETE</promise>` → exit if found.
5. Append to `progress.txt`.
6. Loop.

**Adapter interface is intentionally minimal.** Three methods only:
- `run_story(story, context) -> AssistantResponse` — execute one story.
- `run_orchestrator_turn(prompt, history) -> AssistantResponse` — for orchestrator-level decisions (story selection, batch planning) that the orchestrator agent does itself.
- `get_history() / save_history()` — persistence of orchestrator's conversation history.

**Background-process model**: `python scripts/ralph.py --prd prd.json` runs to completion or `max_iter`. User's main `hermes` session is unaffected. They can `cat prd.json | jq` or `tail progress.txt` to see progress.

---

## 5. User Stories

### US-001: Refactor scripts/ralph.py into RalphCore + HermesAdapter split

**Description**: As a developer, I need the existing 1154-line `scripts/ralph.py` decomposed into a pure-Python core and a Hermes-specific adapter so the platform boundary is explicit.

**Acceptance Criteria**:
- [ ] Create `scripts/core/` directory with `__init__.py`, `prd.py`, `progress.py`, `promise.py`, `orchestrator.py`.
- [ ] Create `scripts/adapters/` directory with `__init__.py`, `base.py`, `hermes.py`.
- [ ] Move prd.json read/write/normalize logic from `ralph.py` into `core/prd.py`.
- [ ] Move progress.txt append + `## Codebase Patterns` merge from `ralph.py` into `core/progress.py`.
- [ ] Move `<promise>COMPLETE</promise>` detection from `ralph.py` into `core/promise.py`.
- [ ] Move `_outer_loop`, `_plan_batch`, `_plan_auto`, `_plan_manual`, `_dead_stories`, `_record_attempts`, `_exhausted_report` into `core/orchestrator.py` (class `RalphGoalLoop`).
- [ ] Move `_resolve_judge_overrides`, `_judge_call_overrides_ctx`, `_render_recon_prompt`, `_parse_recon_block`, `_step_recon`, `_write_grounding_section`, `_read_shared_facts`, `_merge_learnings`, `_render_worker_prompt` — **all deleted** (none of these concepts survive).
- [ ] Create `scripts/adapters/base.py` with abstract class `PlatformAdapter` declaring: `run_story(story, context) -> AssistantResponse`, `run_orchestrator_turn(prompt, history) -> AssistantResponse`, `get_history() -> list`, `save_history(history) -> None`.
- [ ] Create `scripts/adapters/hermes.py` with `HermesAdapter` implementing the four methods (skeleton — fleshed out in US-002).
- [ ] New `scripts/ralph.py` becomes a thin entry point: parse args, instantiate `HermesAdapter`, instantiate `RalphGoalLoop(adapter)`, call `loop.run()`, map return status to exit code.
- [ ] All public API of `RalphGoalLoop` documented with docstrings (no behavior change from v0.4.0 except: no `_step_recon`, no `_step_draft_and_set`, no `_step_review_prd`).
- [ ] `python scripts/ralph.py --help` shows new CLI (see US-005).
- [ ] Typecheck passes (`python -m py_compile scripts/core/*.py scripts/adapters/*.py scripts/ralph.py`).

---

### US-002: Implement HermesAdapter using AIAgent.run_conversation

**Description**: As a developer, I need the HermesAdapter to drive a single persistent `AIAgent` instance via `run_conversation(user_message, conversation_history=...)`, persisting history across iterations.

**Acceptance Criteria**:
- [ ] `HermesAdapter.__init__()` constructs one `AIAgent(session_id="ralph-<int(time.time())>", quiet_mode=True, model=<from config or flag>, provider=<from config or flag>)`.
- [ ] `run_orchestrator_turn(prompt, history)` calls `agent.run_conversation(user_message=prompt, conversation_history=history)` and returns a dict `{"message": <assistant text>, "tool_calls": [...], "tokens": {input, output}}`.
- [ ] `run_story(story, context)` builds a worker prompt (story id + acceptance criteria + relevant `## Codebase Patterns` from progress.txt), then calls `agent.run_conversation(...)` again on the same agent (so the orchestrator's history + the worker's tool calls share one session).
- [ ] `get_history()` returns the agent's `conversation_history` attribute.
- [ ] `save_history(history)` writes to `~/.hermes/sessions/<session_id>/conversation_history.json` so the process can be restarted (best-effort; failure logs to progress.txt but doesn't abort).
- [ ] On Hermes import failure, `HermesAdapter` raises `RuntimeError` with the exact import error in its message.
- [ ] Adapter never calls `delegate_task` (no fan-out — see §Architecture rationale).
- [ ] Adapter never references `GoalManager`, `judge_goal`, `auxiliary.goal_judge`, or `draft_contract`.
- [ ] Typecheck passes.

---

### US-003: Drop /goal integration from orchestrator

**Description**: As a developer, I need the orchestrator to drop all Hermes-`/goal` plumbing (GoalManager set/pause/evaluate, judge LLM routing, draft_contract) so it depends only on the adapter interface.

**Acceptance Criteria**:
- [ ] `RalphGoalLoop.__init__` no longer accepts `auto_draft`, `judge_overrides`, or constructs `GoalManager`.
- [ ] `_step_draft_and_set` deleted.
- [ ] `_step_review_prd` deleted (the v0.1.0 auto-resume hack is gone — boss now pauses the background process manually if they want to review).
- [ ] `_evaluate` is replaced by `core/promise.py::detect_promise(assistant_message) -> bool`. No judge LLM call.
- [ ] `_outer_loop` calls `adapter.run_story(story, context)` instead of `delegate_task(tasks=[...])`.
- [ ] `_outer_loop` no longer constructs `last_response` aggregates; it checks each `run_story` result for `<promise>` token directly.
- [ ] `--no-draft`, `--judge-provider`, `--judge-model`, `--judge-base-url`, `--judge-api-key-env`, `--judge-keep-aux-config`, `--session-id` flags **removed** from argparse.
- [ ] `_LOG_FALLBACK` and the `_LOG_FALLBACK = ...` patterns related to judge-routing removed.
- [ ] Typecheck passes.

---

### US-004: Drop recon pass

**Description**: As a developer, I need the recon mechanism removed because the orchestrator is now a persistent agent that reads project files itself each turn — recon's compensating-for-fresh-subagent-context purpose no longer exists.

**Acceptance Criteria**:
- [ ] `_step_recon`, `_render_recon_prompt`, `_parse_recon_block`, `_write_grounding_section`, `_RECON_BLOCK_RE`, `_RECON_GROUP_DEFAULT` all deleted from `ralph.py`.
- [ ] `--no-recon`, `--recon-group` flags removed from argparse.
- [ ] `scripts/test_recon.py` deleted (or archived to `.archive/` with a note).
- [ ] `progress.txt` no longer contains a `## Grounding` section (orchestrator's own reads via file tools suffice).
- [ ] `prd.json` no longer has a `recon` field on stories (orchestrator doesn't need pre-grounded acceptance criteria — it re-reads real code each turn).
- [ ] SKILL.md §Recon section deleted entirely.
- [ ] docs/ARCHITECTURE.md §Recon discussion deleted.
- [ ] Typecheck passes.

---

### US-005: Update CLI surface to match new architecture

**Description**: As a developer / boss, I want the CLI to reflect what the orchestrator actually does now.

**Acceptance Criteria**:
- [ ] `python scripts/ralph.py --help` shows:
  - `--prd PATH` (required)
  - `--max-iter N` (default 10)
  - `--cost-cap USD` (default 5.0)
  - `--project-root PATH` (default cwd)
  - `--worker-provider NAME` (default: config.yaml `model.provider`)
  - `--worker-model NAME` (default: config.yaml `model.default`)
  - `--parallel-mode {off,priority,auto,manual}` (default `auto`)
  - `--max-parallel N` (default 10)
  - `--max-story-attempts N` (default 3; 0 = never bench)
- [ ] Removed flags produce `argparse` error: `--no-draft`, `--no-recon`, `--recon-group`, `--session-id`, `--judge-provider`, `--judge-model`, `--judge-base-url`, `--judge-api-key-env`, `--judge-keep-aux-config`.
- [ ] `--max-iter` default renamed to `--max-iter` (was `--max-iter` in v0.4.0 — keep name, drop `--no-draft` confusion).
- [ ] Exit codes unchanged from v0.4.0: 0 (ALL_PASSES/GOAL_DONE), 2 (MAX_ITERATIONS), 3 (COST_CAP), 6 (DELEGATION_FAILED), 7 (GOAL_CLEARED), 8 (INTERNAL_ERROR), 10 (NO_PROGRESS), 11 (STORIES_EXHAUSTED). Note: `GOAL_DONE` and `GOAL_CLEARED` rename to `ALL_PASSES` and `RUN_PAUSED` in US-008.
- [ ] Typecheck passes.

---

### US-006: Preserve v0.4.0 starvation fix (dependsOn, priority, benching)

**Description**: As a developer, I want the v0.4.0 starvation fixes preserved verbatim in `RalphCore`.

**Acceptance Criteria**:
- [ ] `_plan_auto`, `_plan_manual`, `_plan_priority`, `_plan_off` keep their v0.4.0 semantics.
- [ ] `dependsOn` (camel) and `depends_on` (snake) both accepted via `_story_deps`.
- [ ] `_dead_stories` does the transitive closure over `dependsOn` chains (a story waiting on a benched story is itself dead).
- [ ] `_record_attempts` charges one attempt to every dispatched story still not `passes: true`; benches at `max_story_attempts`.
- [ ] `STORIES_EXHAUSTED` exit code (11) returned when only dead stories remain.
- [ ] `NO_PROGRESS` exit code (10) returned when no story is runnable (e.g. `dependsOn` cycle) but nothing is dead yet.
- [ ] Unit tests in `scripts/test_starvation.py` continue to pass with the new file layout (move test file to `scripts/tests/test_starvation.py` if we reorganize; see US-009).
- [ ] Typecheck passes.

---

### US-007: Preserve v0.3.0 parallelism planner

**Description**: As a developer, I want the v0.3.0 parallelism-mode logic preserved verbatim.

**Acceptance Criteria**:
- [ ] `--parallel-mode auto` groups by `dependsOn` + file overlap; unknown file set ⇒ story runs alone.
- [ ] `--parallel-mode manual` respects `parallelGroup` field on stories.
- [ ] `--parallel-mode priority` groups by equal priority value.
- [ ] `--parallel-mode off` runs one story at a time.
- [ ] `--max-parallel` caps batch size.
- [ ] With no `delegate_task` fan-out, "batch" is still computed but executed sequentially via repeated `adapter.run_story()` calls.
- [ ] Unit tests in `scripts/test_parallel.py` continue to pass (move to `scripts/tests/` per US-009).
- [ ] Typecheck passes.

---

### US-008: Rename statuses; update exit-code mapping

**Description**: As a developer, I want statuses renamed to reflect that there's no `/goal` judge involved anymore.

**Acceptance Criteria**:
- [ ] `GOAL_DONE` renamed to `ALL_PASSES` (already existed, but is now the only completion signal besides `<promise>`-driven exit).
- [ ] `GOAL_CLEARED` renamed to `RUN_PAUSED` (boss paused the background process; boss resumes by re-running with the same `--prd`).
- [ ] `GOAL_BLOCKED` removed (no judge LLM to be blocked).
- [ ] `USER_REJECTED_CONTRACT` removed (no contract draft step).
- [ ] `USER_REJECTED_PRD` removed (no prd review gate; boss edits `prd.json` directly while process is paused).
- [ ] `NO_GOAL` removed.
- [ ] Remaining statuses: `ALL_PASSES`, `MAX_ITERATIONS`, `COST_CAP`, `DELEGATION_FAILED`, `RUN_PAUSED`, `INTERNAL_ERROR`, `NO_PROGRESS`, `STORIES_EXHAUSTED`.
- [ ] `_STATUS_TO_EXIT` dict updated to match.
- [ ] Exit codes: 0 (ALL_PASSES), 2 (MAX_ITERATIONS), 3 (COST_CAP), 6 (DELEGATION_FAILED), 7 (RUN_PAUSED), 8 (INTERNAL_ERROR), 10 (NO_PROGRESS), 11 (STORIES_EXHAUSTED).
- [ ] SKILL.md §Monitoring & Kill Behavior updated.
- [ ] Typecheck passes.

---

### US-009: Reorganize tests under scripts/tests/

**Description**: As a developer, I want test files moved into `scripts/tests/` so they don't get confused with the production scripts in `scripts/`.

**Acceptance Criteria**:
- [ ] Create `scripts/tests/` directory.
- [ ] Move `scripts/test_minidemo.py` → `scripts/tests/test_minidemo.py`.
- [ ] Move `scripts/test_parallel.py` → `scripts/tests/test_parallel.py`.
- [ ] Move `scripts/test_starvation.py` → `scripts/tests/test_starvation.py`.
- [ ] Move `scripts/test_recon.py` → `scripts/tests/.archive/test_recon.py` (deprecated; docstring explains "recon removed in v1.0 — see US-004").
- [ ] Each test file imports updated paths to `scripts/core/` and `scripts/adapters/`.
- [ ] `python scripts/tests/test_starvation.py` runs and all assertions pass.
- [ ] `python scripts/tests/test_parallel.py` runs and all assertions pass.
- [ ] `python scripts/tests/test_minidemo.py` runs and all assertions pass.
- [ ] README.md test instructions updated.

---

### US-010: Update SKILL.md to reflect new architecture

**Description**: As a developer / future reader, I want SKILL.md to describe what the code actually does, not what v0.1.0 design documents imagined.

**Acceptance Criteria**:
- [ ] §Overview rewritten: orchestrator is "persistent AIAgent + PlatformAdapter" not "/goal engine".
- [ ] §Two-Layer Model rewritten: "outer iteration loop (RalphCore, pure Python) + inner per-story agent (HermesAdapter)". Drop the comparison table that mentions `/goal` judge.
- [ ] §Priority Grouping & Batch Strategy updated: pseudocode uses real method names (`RalphGoalLoop.run()`, `HermesAdapter.run_story()`, `detect_promise()`).
- [ ] §Recon section **deleted**.
- [ ] §Launching the Loop updated: `python scripts/ralph.py --prd ... --project-root .` is the entry; clarify that ralph runs as a separate process (boss's main `hermes` session unaffected).
- [ ] §Model & Credentials updated: drop auxiliary.goal_judge.* discussion; keep `--worker-provider` / `--worker-model` flags.
- [ ] §Monitoring & Kill Behavior updated: show how to pause (`Ctrl-C` / SIGTERM), check progress (`cat progress.txt`, `jq .userStories[].passes prd.json`), resume (re-run `python scripts/ralph.py --prd <same>`).
- [ ] §Cost & Iteration Caps: drop `goal.max_turns` row.
- [ ] §Memory & Cache Invariants rewritten: orchestrator agent's prompt-cache prefix is stable across iterations (same agent instance); subagent invariant no longer applies.
- [ ] §<promise>COMPLETE</promise> Protocol: kept verbatim (single sentence completion signal).
- [ ] §Pitfalls: rewrite list — drop #1 (prd.json + progress.txt race is gone because no subagents), #3 (skip_memory no longer relevant), #8 (auxiliary.goal_judge), #10 (api_mode 404 — judge override gone), #11 (recon hallucination — recon gone), #12 (recon write race — gone), #13 (recon silent failure — gone), #14 (`_offline()` seam — never existed in v1.0). Keep #2, #5, #7, #9 (renumbered if needed).
- [ ] §Verification Checklist updated to reflect new architecture.
- [ ] §References: drop CLAUDE.md.tmpl reference (or keep as historical — call out that it's not used at runtime).

---

### US-011: Update docs/ARCHITECTURE.md

**Description**: As a developer / future reader, I want the architecture doc to describe the RalphCore / PlatformAdapter split and the rationale for dropping /goal, judge, and recon.

**Acceptance Criteria**:
- [ ] §Background: keep Ralph Wiggum original design description.
- [ ] §Why we needed 2 layers (existing): rewrite — we now need 1 layer (RalphCore) + 1 platform-specific adapter, not "outer Ralph + inner /goal".
- [ ] §Design decisions: keep decisions 2, 3, 4 (parallelism, no hardcoded model, no secrets); delete decision 1 ("borrow /goal as judge engine"); add new decision 6 ("RalphCore is pure Python; PlatformAdapter is the only Hermes-aware code").
- [ ] Add §Dropped mechanisms section: explains why /goal, judge LLM, recon are no longer needed.
- [ ] Add §Adapter interface section: describes PlatformAdapter contract.

---

### US-012: Update README.md and README.en.md

**Description**: As a developer / boss, I want the user-facing README to match the new architecture.

**Acceptance Criteria**:
- [ ] §Quickstart updated: example invocation shows only `--prd`, `--project-root`, `--max-iter`.
- [ ] §CLI Reference: only the v1.0 flags listed in US-005.
- [ ] §How it works: 3-step diagram (RalphCore orchestrates → HermesAdapter runs story → detect <promise> → loop).
- [ ] §FAQ / Troubleshooting: drop Q&A about /goal judge, recon, judge LLM. Add: "How do I pause mid-run?" (Ctrl-C / SIGTERM, then re-run with same --prd to resume), "Where does progress go?" (progress.txt + prd.json).
- [ ] Drop any reference to v0.4.0 architecture decisions.

---

### US-013: Add OpenCLAWAdapter stub

**Description**: As a developer, I want a clear TODO marker for OpenCLAW adapter so the architecture's extensibility is visible.

**Acceptance Criteria**:
- [ ] Create `scripts/adapters/openclaw.py` with `OpenCLAWAdapter(PlatformAdapter)` whose four methods all raise `NotImplementedError("OpenCLAW adapter TODO — see PRD US-013")`.
- [ ] Class docstring describes what the adapter would need to implement: an OpenCLAW agent run loop, a way to detect `<promise>` in its output, a way to persist conversation history across OpenCLAW sessions.
- [ ] `scripts/ralph.py` argparse accepts `--platform {hermes,openclaw}` (default `hermes`); selecting `openclaw` constructs `OpenCLAWAdapter` and immediately raises `NotImplementedError` with a useful message.
- [ ] README.md §Extending to other platforms section briefly explains how to add a new adapter.

---

### US-014: End-to-end smoke test with a 3-story prd

**Description**: As a developer, I want to verify the new architecture runs end-to-end on a minimal prd before declaring US-001 through US-013 done.

**Acceptance Criteria**:
- [ ] Create `examples/three-stories/prd.json` with 3 stories: each story creates one trivial file and echoes a marker.
- [ ] Run `python scripts/ralph.py --prd examples/three-stories/prd.json --project-root examples/three-stories` to completion.
- [ ] `prd.json` shows all 3 stories `passes: true`.
- [ ] `progress.txt` exists and contains `[ralph-orchestrator]` log lines + `## Codebase Patterns` section if any worker emitted one.
- [ ] Exit code 0.
- [ ] Run again from a paused state: start a run, SIGTERM after round 1, re-run with same `--prd`, verify the second run resumes from where the first stopped (worker agent re-loads history from disk).
- [ ] Total wall-clock time logged in `progress.txt`.

---

### US-015: Verify equivalence with upstream Ralph on a real project

**Description**: As a developer / boss, I want to verify that the new architecture produces equivalent outputs to upstream Ralph on a non-trivial project.

**Acceptance Criteria**:
- [ ] Pick a project (suggest: `desktop/hermes-ralph-goal-loop-test/` fixture if still present, or another small project).
- [ ] Run upstream `mikeyobrien/ralph` on that project's prd.json; capture `prd.json` end state + `progress.txt` final contents.
- [ ] Run `python scripts/ralph.py --prd <same prd.json>` on the same project; capture end state.
- [ ] Both runs: all stories `passes: true`, exit code 0, comparable wall-clock time (within 2x — Hermes in-process is expected faster).
- [ ] `progress.txt` shape comparable: both have append-only log lines + `## Codebase Patterns` section; v1.0 entries use `[ralph-orchestrator]` prefix; upstream uses whatever its bash loop logs.
- [ ] If divergence observed: document in `docs/v1.0-vs-upstream.md` with specific examples.

---

## 6. Functional Requirements

- **FR-1**: `RalphCore` MUST be importable without any Hermes imports. (`import scripts.core.orchestrator` works in a clean Python venv.)
- **FR-2**: `PlatformAdapter` MUST be an abstract base class; instantiating it directly MUST raise `TypeError`.
- **FR-3**: `HermesAdapter.run_story()` MUST return within `--max-iter`-bounded time per story (default `--max-iter` for orchestrator; per-story iteration cap is `AIAgent`'s own `max_iterations` parameter, default `sys.maxsize`).
- **FR-4**: Orchestrator MUST persist `conversation_history` after every iteration so that the process can be SIGTERM'd and resumed.
- **FR-5**: `<promise>COMPLETE</promise>` MUST be detected as a literal substring of any worker response (case-sensitive, exact match). Detection in `core/promise.py`.
- **FR-6**: Orchestrator MUST treat one `<promise>` hit in any worker response as immediate exit with `ALL_PASSES`.
- **FR-7**: Orchestrator MUST exit with `MAX_ITERATIONS` after `--max-iter` outer iterations without seeing `<promise>`.
- **FR-8**: Orchestrator MUST exit with `COST_CAP` when cumulative token cost exceeds `--cost-cap`. Cost computed as `(input_tokens / 1000) * 0.003 + (output_tokens / 1000) * 0.015`.
- **FR-9**: Orchestrator MUST skip stories whose `dependsOn` chain contains any non-`passes` story. (Dependency gating from v0.4.0 preserved.)
- **FR-10**: Orchestrator MUST bench a story after `--max-story-attempts` failed dispatches. (Starvation fix from v0.4.0 preserved.)
- **FR-11**: Orchestrator MUST exit with `STORIES_EXHAUSTED` (exit 11) when only dead-or-benched stories remain. (Transitive closure from v0.4.0 preserved.)
- **FR-12**: Orchestrator MUST exit with `NO_PROGRESS` (exit 10) when no story is runnable AND no story is dead (e.g., circular `dependsOn` chain where each cycle member waits on another).
- **FR-13**: Orchestrator MUST NOT use `delegate_task` in v1.0. (Concurrency is removed; see §Architecture rationale.)
- **FR-14**: Orchestrator MUST NOT call `GoalManager.set / pause / evaluate_after_turn` or `_call_goal_judge_llm`. (No /goal dependency.)
- **FR-15**: Orchestrator MUST NOT perform recon pass. (No `_step_recon`, no `_render_recon_prompt`, no `_parse_recon_block`.)
- **FR-16**: `prd.json` schema MUST be backward-compatible with v0.4.0. New optional fields (`recon`, `_branched_from`) ignored if present.
- **FR-17**: `progress.txt` format MUST remain append-only. Existing `[timestamp] ralph-orchestrator: <msg>` lines parseable; `## Codebase Patterns` section still extractable.
- **FR-18**: `HermesAdapter` MUST handle Hermes import failure gracefully — raise `RuntimeError` with the exact import error chain.

---

## 7. Technical Considerations

### 7.1 Adapter Interface (precise contract)

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List, TypedDict


class AssistantResponse(TypedDict):
    message: str                    # Assistant's text response (may contain <promise>)
    tool_calls: List[Dict[str, Any]]  # Tool calls made during this turn (for logging/debug)
    tokens: Dict[str, int]          # {"input": int, "output": int}


class PlatformAdapter(ABC):
    @abstractmethod
    def run_orchestrator_turn(self, prompt: str, history: List[Dict[str, Any]]) -> AssistantResponse:
        """Run one orchestrator-level turn (e.g., 'pick the next story and plan the batch').
        History is the orchestrator's accumulating conversation history."""
        ...

    @abstractmethod
    def run_story(self, story: Dict[str, Any], context: str) -> AssistantResponse:
        """Run one story to completion (or until the agent hits its iteration limit).
        `context` is the orchestrator-provided grounding (## Codebase Patterns, recent progress)."""
        ...

    @abstractmethod
    def get_history(self) -> List[Dict[str, Any]]:
        """Return the orchestrator's current conversation history."""
        ...

    @abstractmethod
    def save_history(self, history: List[Dict[str, Any]]) -> None:
        """Persist the orchestrator's conversation history to durable storage."""
        ...
```

### 7.2 Why single agent (no delegate_task)?

The boss's diagnostic report identified three mechanisms in v0.4.0 that exist only to compensate for breaking Ralph's "agent sits in project" invariant:
1. **Recon worker** — fan out fresh agents to read code; recon's output is the fresh agent's interface inventory.
2. **Per-priority `delegate_task` batch** — fan out fresh agents to execute stories; each gets a recon-injected grounding.
3. **Judge LLM** — external LLM rates whether the fresh agent's summary indicates completion.

If we restore the invariant (one persistent agent in the project), all three mechanisms lose their purpose. The persistent agent reads the project files itself each turn (which is what `cd $PROJECT_ROOT && claude` gives upstream Ralph). Stories are dispatched sequentially because the same agent does them, with full cross-story history.

### 7.3 What we keep from v0.4.0

- **Outer iteration loop** — same `for round_idx in range(max_iter)` shape.
- **Batch planner** (`_plan_auto`, `_plan_manual`, etc.) — same logic, just executed sequentially now.
- **Starvation handling** (dependsOn gating, priority ordering, benching, dead-story transitive closure) — preserved verbatim.
- **CLI flags except the /goal-specific ones** — `--parallel-mode`, `--max-parallel`, `--max-story-attempts`, `--project-root`, `--worker-provider`, `--worker-model`, `--max-iter`, `--cost-cap`, `--prd`.
- **prd.json schema** — backward-compatible.
- **progress.txt format** — backward-compatible.

### 7.4 What we drop

- `/goal` engine integration (`GoalManager`, `evaluate_after_turn`, `_call_goal_judge_llm`, `auxiliary.goal_judge.*`).
- Judge LLM routing (`_resolve_judge_overrides`, `_judge_call_overrides_ctx`, `api_mode` ladder).
- Recon pass (`_step_recon`, `_render_recon_prompt`, `_parse_recon_block`, `_write_grounding_section`).
- `delegate_task` (per-story execution becomes `HermesAdapter.run_story()` on the same agent).
- `--no-draft`, `--no-recon`, `--recon-group`, `--session-id`, all `--judge-*` flags.
- `GoalContract` / `draft_contract` (no contract drafting).
- Boss review nodes (`_step_review_prd`) — boss pauses the process and edits `prd.json` directly.
- `USER_REJECTED_*`, `GOAL_BLOCKED`, `NO_GOAL`, `GOAL_DONE`, `GOAL_CLEARED` statuses — replaced by `ALL_PASSES`, `RUN_PAUSED`, `STORIES_EXHAUSTED`, `NO_PROGRESS`.

### 7.5 Hermes imports used (and only these)

- `from run_agent import AIAgent` — to construct the persistent agent.
- `from hermes_cli.config import load_config` — to resolve `--worker-provider` / `--worker-model` defaults.
- `from hermes_cli.runtime_provider import resolve_runtime_provider` — to get a fully-resolved (provider, model, base_url, api_key, api_mode) tuple.

That's it. No `GoalManager`, no `delegate_task`, no `judge_goal`, no `draft_contract`, no `auxiliary_client`, no `auxiliary.goal_judge`.

### 7.6 Persistence

The orchestrator's `conversation_history` is persisted to disk after every iteration. Two failure modes are accepted:
1. **Process killed (SIGTERM / Ctrl-C)** — boss re-runs `python scripts/ralph.py --prd <same>`; `HermesAdapter.__init__` checks for an existing `ralph-<session_id>` directory under `~/.hermes/sessions/...`; if present, re-attach; if not, start fresh.
2. **Crash** — same as SIGTERM; the next run re-loads from the last persisted snapshot.

This replaces v0.4.0's `GoalManager`-based persistence (which depended on Hermes' SQLite state DB).

---

## 8. Success Metrics

- **SM-1**: A 3-story prd runs end-to-end with exit code 0 in under 60 seconds on `examples/three-stories/`.
- **SM-2**: Re-running after SIGTERM resumes from the last persisted history (no story is re-executed; no story is missed).
- **SM-3**: All v0.3.0 / v0.4.0 test files (in `scripts/tests/`) pass after the refactor.
- **SM-4**: `python scripts/ralph.py --help` produces no usage errors; all listed flags match US-005.
- **SM-5**: `git grep "GoalManager\|judge_goal\|delegate_task\|recon\|_call_goal_judge_llm\|auxiliary.goal_judge"` returns 0 matches in `scripts/`.
- **SM-6**: `git grep "_step_recon\|_render_recon_prompt\|_parse_recon_block"` returns 0 matches in `scripts/`.
- **SM-7**: A non-trivial prd (10+ stories, mix of priorities and dependencies) runs to completion in a comparable wall-clock time to upstream Ralph (within 2x).
- **SM-8**: Total `scripts/` LOC ≤ 1000 (down from 1154 + test files).

---

## 9. Open Questions

1. **Q-1**: Should `HermesAdapter` allow `--platform hermes --use-delegate-task-for-stories` as an escape hatch (opt back into v0.3.0 / v0.4.0 fan-out behavior)? My recommendation: **no** — it adds complexity and contradicts the architecture's premise. v0.3.0 / v0.4.0 are kept in git history; if you want that behavior, check out the old tag.
2. **Q-2**: Should `--parallel-mode` still exist if stories are always sequential? My recommendation: **yes, keep it** — the batch planner logic still determines which stories can run in parallel and which are blocked. The flag stays meaningful even though execution is serial. (Auto/manual/priority/off all still describe valid grouping decisions; the executor just walks the batch one story at a time.)
3. **Q-3**: Should the orchestrator agent's `system_prompt` be configurable (e.g., `--system-prompt PATH`)? My recommendation: **no for v1.0** — hardcode a minimal orchestrator system prompt that explains "you are Ralph orchestrator, read prd.json, pick highest-priority pending story, dispatch via adapter, return <promise>COMPLETE</promise> when all done". Make it configurable in v1.1 if needed.
4. **Q-4**: How does the orchestrator's agent know when **all** stories are done (so it can echo `<promise>` itself)? My recommendation: orchestrator agent runs the outer loop **internally** as part of its conversation — its prompt each turn is "current state of prd.json: here are the pending stories. Pick the highest priority, dispatch it via adapter, append to progress.txt, then read progress.txt and tell me what's next." When the orchestrator reads `prd.json` and sees all `passes: true`, it echoes `<promise>COMPLETE</promise>` and exits. This puts the iteration loop inside the agent's head, which is more Ralph-faithful.

---

## 10. Out of Scope (Reiterated)

- OpenCLAW adapter implementation.
- Concurrent story execution.
- Backward compatibility with `/goal`-specific flags.
- Real-cost token estimation.
- Multi-machine or cross-profile parallelism.
- Recon / pre-pass interface grounding.
- Judge LLM.
- Auto-resume boss review nodes.
- Memory/cross-session persistence beyond `conversation_history.json`.
- Network-attached `prd.json` (e.g., S3-backed).
- Slack/Discord/Telegram integration for progress notifications (out of scope for skill; can be added by a separate skill that watches `progress.txt`).

---

## 11. References

- **Diagnostic report**: `c:\Users\Administrator\Desktop\ralph-goal-loop-诊断-2026-09-17\RALPH_GOAL_LOOP_诊断报告.md` — establishes why v0.4.0's `/goal` + recon + subagent architecture is a deviation, not a port.
- **Current code**: `scripts/ralph.py` (1154 lines, v0.4.0) — to be decomposed into core/ + adapters/.
- **Current docs**: `SKILL.md` (586 lines), `docs/ARCHITECTURE.md` (~300 lines), `README.md` / `README.en.md` — to be rewritten.
- **Hermes interfaces used**: `AIAgent.run_conversation()`, `hermes_cli.config.load_config`, `hermes_cli.runtime_provider.resolve_runtime_provider`.
- **Hermes interfaces NOT used (vs v0.4.0)**: `GoalManager`, `evaluate_after_turn`, `judge_goal`, `_call_goal_judge_llm`, `delegate_task`, `draft_contract`, `auxiliary.goal_judge.*`.
