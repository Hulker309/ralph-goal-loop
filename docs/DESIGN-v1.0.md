# Design: ralph-goal-loop v1.0 — RalphCore + PlatformAdapter

**Date**: 2026-09-18
**Status**: Draft (companion to `tasks/prd-ralph-goal-loop-v1.md`)
**Audience**: developers and the boss

---

## 1. What we are building

A two-layer port of `mikeyobrien/ralph`:

```
┌───────────────────────────────────────────────────────────────┐
│                       RalphCore (pure Python)                  │
│                                                                │
│   scripts/core/prd.py          — prd.json read/write/normalize│
│   scripts/core/progress.py     — progress.txt + Patterns merge│
│   scripts/core/promise.py      — <promise> detection           │
│   scripts/core/orchestrator.py — RalphGoalLoop (outer loop)    │
│                                                                │
│   ZERO hermes imports.                                          │
│   ZERO Claude / OpenAI / model SDK imports.                    │
│   Can be unit-tested in a clean venv.                          │
└───────────────────────────┬────────────────────────────────────┘
                            │
                  ┌─────────▼─────────┐
                  │  PlatformAdapter  │  abstract base class
                  └─────────┬─────────┘
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
       ┌──────────────────┐   ┌──────────────────────┐
       │ HermesAdapter    │   │ OpenCLAWAdapter      │
       │ scripts/adapters/│   │ scripts/adapters/    │
       │ hermes.py        │   │ openclaw.py (TODO)   │
       └──────────────────┘   └──────────────────────┘
```

The PRD (`tasks/prd-ralph-goal-loop-v1.md`) defines the user stories and functional requirements. This document explains **why**.

---

## 2. Why we drop /goal, judge, and recon

The boss's 2026-09-17 diagnostic report (`c:\Users\Administrator\Desktop\ralph-goal-loop-诊断-2026-09-17\RALPH_GOAL_LOOP_诊断报告.md`) established that v0.4.0 is not a port — it is a re-design that drifted from Ralph's architecture in three compounding ways.

### 2.1 The /goal engine is structurally incompatible with Ralph

`/goal` was designed for "one objective, judge whether the agent has done it." Its primitive is `evaluate_after_turn(last_response) -> verdict` where `verdict ∈ {done, continue, wait, blocked}`. The single verdict describes the state of **one goal**.

Ralph has N stories, each with its own `passes: bool`. There is no "verdict" that subsumes "story 1 passed, story 2 passed, story 3 failed because of X, story 4 is waiting on story 1" — that is a **multi-passes state vector**, not a single goal-state.

v0.4.0 tried to make `/goal` fit by feeding it an aggregated `last_response` and trusting the judge LLM to interpret "all stories done" from worker summaries. This works when summaries are clean; it silently mis-judges when summaries are contradictory, partial, or hallucinated.

### 2.2 Recon exists only because we broke the invariant

The boss's report identified the central Ralph invariant:

> "Ralph's agent sits in the project root and reads real code each turn. The interface inventory is **implicit** in the agent's environment, not in any precomputed file."

v0.4.0 broke this by dispatching per-story execution to `delegate_task`, which spawns **fresh** child agents (`skip_memory=True`, hardcoded in `tools/delegate_tool.py:240`). A fresh agent has no inventory of the project's interfaces. v0.4.0 compensated by introducing `recon`: a fan-out of read-only workers that read the code and write `prd.json.recon` and `progress.txt.## Grounding`.

Recon is not a Ralph concept. It is a **patch** for the gap that `delegate_task` opened. Remove `delegate_task` and recon becomes redundant.

### 2.3 Judge LLM exists only because we broke the completion protocol

Upstream Ralph's completion signal is the literal substring `<promise>COMPLETE</promise>` in worker output. The orchestrator's bash loop greps for it. That is **the entire** completion check.

v0.4.0 added a judge LLM as a "double-check" — the worker says `<promise>`, but a separate LLM reads the summary and decides if `<promise>` was warranted. This introduces a second authority on completion, which is fine if you don't trust workers, but it is **not Ralph**. Ralph trusts the worker's `<promise>` declaration.

### 2.4 What we keep, what we lose

| Mechanism | v0.4.0 | v1.0 | Rationale |
|---|---|---|---|
| Outer iteration loop | ✅ | ✅ | Core Ralph algorithm |
| prd.json + progress.txt | ✅ | ✅ | Ralph state files |
| `<promise>` completion | ✅ | ✅ | Ralph completion signal |
| `dependsOn` gating | ✅ | ✅ | Boss-approved v0.4.0 fix |
| Priority ordering | ✅ | ✅ | Boss-approved v0.4.0 fix |
| Benching failed stories | ✅ | ✅ | Boss-approved v0.4.0 fix |
| `--parallel-mode` | ✅ | ✅ (semantics) | v0.3.0 boss-approved |
| `/goal` engine | ✅ | ❌ | Incompatible — see §2.1 |
| Judge LLM | ✅ | ❌ | Not Ralph — see §2.3 |
| Recon worker | ✅ | ❌ | Patch — see §2.2 |
| `delegate_task` fan-out | ✅ | ❌ | Patch — see §2.2 |
| Draft contract | ✅ | ❌ | No contract in Ralph |
| Boss review pause/resume | ✅ | ❌ | Boss pauses process directly |

We drop four mechanisms. We keep six. The four we drop are all "patches for breaking the invariant."

---

## 3. The PlatformAdapter interface

```python
class PlatformAdapter(ABC):
    @abstractmethod
    def run_orchestrator_turn(self, prompt: str, history: list) -> AssistantResponse:
        """Run one orchestrator-level turn.

        The orchestrator is a thinking entity. Each turn it:
        1. Reads prd.json (its own action, via file tools).
        2. Picks the highest-priority pending story (its own decision).
        3. Decides what context to give the worker (its own decision).
        4. Calls adapter.run_story() to dispatch.
        5. Reads progress.txt (its own action).
        6. Decides next action.
        """
        ...

    @abstractmethod
    def run_story(self, story: dict, context: str) -> AssistantResponse:
        """Run one story to completion.

        The story agent:
        1. Reads project files (its CWD is the project root).
        2. Implements the story.
        3. Verifies acceptance criteria.
        4. Sets `passes: true` in prd.json (its own action).
        5. Appends to progress.txt.
        6. Echoes <promise>COMPLETE</promise> if ALL stories are now passed.
        """
        ...

    @abstractmethod
    def get_history(self) -> list: ...

    @abstractmethod
    def save_history(self, history: list) -> None: ...
```

This interface is **minimal** on purpose. A new platform implements four methods. Everything else — iteration loop, batch planning, dependency gating, benching, `<promise>` detection, progress.txt merging — is in `RalphCore` and runs identically on every platform.

---

## 4. How HermesAdapter implements this

```
HermesAdapter.__init__():
    session_id = "ralph-<unix-ts>"
    self.agent = AIAgent(session_id=session_id, quiet_mode=True, model=..., provider=...)
    self.history = []  # orchestrator's conversation history

HermesAdapter.run_orchestrator_turn(prompt, history):
    self.history = history
    result = self.agent.run_conversation(
        user_message=prompt,
        conversation_history=self.history,
    )
    self.history.append({"role": "user", "content": prompt})
    self.history.append({"role": "assistant", "content": result["message"]})
    self.save_history(self.history)
    return {"message": result["message"], ...}

HermesAdapter.run_story(story, context):
    # Build a worker prompt and dispatch on the SAME agent
    prompt = build_worker_prompt(story, context)
    result = self.agent.run_conversation(
        user_message=prompt,
        conversation_history=self.history,
    )
    self.history.append(...)
    self.save_history(self.history)
    return {"message": result["message"], ...}

HermesAdapter.get_history() -> self.history

HermesAdapter.save_history(history) ->
    write to ~/.hermes/ralph-sessions/<session_id>/conversation_history.json
```

**Critical: same agent for orchestrator and story worker.** This is the invariant restoration. The orchestrator's history grows over the run. The story worker's context is whatever the orchestrator decided to inject. The agent reads `prd.json` and `progress.txt` from its CWD (which is the project root). The agent writes `passes: true` itself.

This is `claude --prompt-file prompt.md` from a directory inside the project, except the process persists across iterations instead of being forked per iteration.

---

## 5. The background-process execution model

```
Terminal 1 (user's main hermes session):
  $ hermes chat
  > How's ralph doing?
  (boss checks prd.json / progress.txt directly, no hermes integration)

Terminal 2 (background ralph process):
  $ cd /path/to/project
  $ python scripts/ralph.py --prd ./prd.json --project-root . &
  $ jobs
  [1]+ Running    python scripts/ralph.py --prd ./prd.json

  # boss can SIGTERM to pause:
  $ kill %1
  # boss edits prd.json to skip a story:
  $ vim prd.json
  # boss resumes:
  $ python scripts/ralph.py --prd ./prd.json
  # HermesAdapter detects existing session_id, re-loads history
```

This replaces v0.4.0's `/goal pause/resume` mechanism, which depended on `GoalManager` plumbing into Hermes' SQLite state DB. The boss now controls the process lifecycle directly via shell signals — much simpler, much more Ralph-like (upstream Ralph is a bash loop, you Ctrl-C it, edit prd.json, restart).

---

## 6. Why pure Python in RalphCore

Three reasons:

1. **Testability.** `scripts/core/orchestrator.py` can be unit-tested with stub adapters in a clean Python venv. No Hermes dependency, no API keys, no network.
2. **Portability.** When we eventually write `OpenCLAWAdapter`, the `RalphCore` tests still pass. The architecture's promise is "drop in a new adapter, everything else works."
3. **Auditability.** A reviewer can read `core/orchestrator.py` and verify the iteration algorithm without learning any framework. The logic is Ralph's bash loop, translated to Python.

---

## 7. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `AIAgent.run_conversation` semantics differ from `claude --prompt-file` | Medium | High | US-015 verifies equivalence on a real project. If divergent, document gap. |
| Orchestrator agent gets stuck in conversation loop | Medium | Medium | The orchestrator prompt tells it "you can only echo `<promise>` when prd.json shows all stories passed." If it loops, `--max-iter` saves us. |
| `conversation_history` grows unbounded over a long run | Medium | Low | Hermes compresses history automatically (compression facade). If not, add a manual truncation step before save_history. |
| Hermes import fails in user's environment | Low | Medium | `HermesAdapter.__init__` raises `RuntimeError` with import error chain. Boss sees clear error, fixes env. |
| Boss loses patience waiting for `--max-iter` × story wall-clock | Medium | Medium | Show progress in `progress.txt`. Add `--time-cap` flag in v1.1 if needed. |
| OpenCLAW adapter design doesn't fit OpenCLAW's actual API | High | Low | OpenCLAW adapter is out of scope for this PRD. When implemented, may require interface revision — but RalphCore tests should still pass. |

---

## 8. What this design explicitly is NOT

- Not a "Ralph rewrite in Hermes." It's a port. Ralph's algorithm is preserved.
- Not a "framework for multi-agent orchestration." It's Ralph plus a platform abstraction.
- Not a "judge LLM with extra steps." The judge LLM is gone.
- Not a "general PRD executor." It executes Ralph-format prd.json files. Other PRD formats are out of scope.
- Not a "production-grade autonomous agent system." It's a Ralph port. Same failure modes as upstream Ralph — a wrong story can spin until `max_iter`, an unsatisfiable story blocks dependents forever (now benched after 3 attempts).

---

## 9. References

- **PRD**: `tasks/prd-ralph-goal-loop-v1.md` (15 user stories, 18 functional requirements, 8 success metrics)
- **Diagnostic report**: `c:\Users\Administrator\Desktop\ralph-goal-loop-诊断-2026-09-17\RALPH_GOAL_LOOP_诊断报告.md`
- **Current code**: `scripts/ralph.py` (v0.4.0, 1154 lines)
- **Hermes interfaces used**: `run_agent.AIAgent.run_conversation`, `hermes_cli.config.load_config`, `hermes_cli.runtime_provider.resolve_runtime_provider`
- **Hermes interfaces NOT used (vs v0.4.0)**: `GoalManager`, `evaluate_after_turn`, `judge_goal`, `_call_goal_judge_llm`, `delegate_task`, `draft_contract`, `auxiliary.goal_judge.*`
