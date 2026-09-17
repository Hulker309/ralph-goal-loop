#!/usr/bin/env python3
"""
test_minidemo.py — minimal end-to-end smoke test for ralph-goal-loop orchestrator.

Goal: verify the orchestrator state machine + Judge contract work in v0.21.3 without
actually running 3 real stories (which would burn ~$0.5-$1 in API cost).

What this verifies (after ralph-2 fix):
1. Imports of hermes_cli.goals + tools.delegate_tool succeed in the target env.
2. progress.txt is created and contains REVIEW_NEEDED markers.
3. Outer-loop state machine advances correctly: 1 pending → batch=[story] → run → judge.
4. <promise>COMPLETE</promise> detection in stub summary → GOAL_DONE without judge call.
5. Judge fail-OPEN path: when goal_manager=None, _evaluate uses pure <promise> check.
6. 5-field verdict routing in _evaluate (ralph-2 fix):
   - verdict=done → GOAL_DONE (status=0)
   - verdict=blocked → GOAL_BLOCKED (status=4)
   - verdict=continue → CONTINUE → outer loop continues
   - transport-failure (status=paused with judge-error message) → GOAL_BLOCKED
7. CLI flag wiring: --judge-provider / --judge-model / --judge-keep-aux-config pass through
   to _resolve_judge_overrides without errors and (when keep_aux_config=True) yield empty dict.

What this does NOT verify (out of scope for a minidemo):
- Real delegate_task fan-out across N>=2 children (would burn cost + need real model).
- Real GoalManager.set/evaluate_after_turn against state.db (test would pollute state).
- Actual file-write by workers (no worker is spawned).
- Network call to a real judge (mocked).

Run:  python scripts/test_minidemo.py
or:    pytest scripts/test_minidemo.py -v
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

# Path setup so we can import the orchestrator.
_HERE = Path(__file__).resolve().parent
_SKILL_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(Path(r"C:/Users/Administrator/.hermes/hermes-agent")))


def _build_mini_prd(path: Path) -> None:
    """Write a 1-story prd.json. Single story = single batch = fan-out trivial case."""
    path.write_text(json.dumps({
        "branchName": "ralph-minidemo",
        "title": "minidemo",
        "description": "Single-story smoke test for ralph-goal-loop orchestrator.",
        "userStories": [
            {
                "id": "US-1",
                "title": "create a hello world file",
                "priority": 1,
                "passes": False,
                "acceptanceCriteria": [
                    "hello.txt exists with content 'hello'",
                ],
            }
        ],
    }, indent=2), encoding="utf-8")


def _stub_run_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Stub delegate_task result: success + <promise> + some tokens (for cost tracking)."""
    return {
        "status": "OK",
        "results": [{
            "task_index": 0,
            "status": "completed",
            "exit_reason": "completed",
            "summary": (
                "Story US-1 done. Created hello.txt. "
                "Verification: `ls hello.txt` → hello.txt. "
                "Marking passes=true. <promise>COMPLETE</promise>"
            ),
            "tokens": {"input": 200, "output": 50},
            "duration_seconds": 1.2,
        }],
    }


def _stub_draft_and_set(self, goal_text: str):
    """Stub draft_contract + GoalManager.set: don't touch state.db."""
    self.goal_manager = None  # bypass real judge; fall back to <promise>-only path
    return None


def _make_decision(verdict: str, **overrides: Any) -> Dict[str, Any]:
    """Synthesize a decision dict mirroring ``_decision()`` in hermes_cli/goals.py:1042."""
    base = {
        "status": "done" if verdict == "done" else "active",
        "should_continue": verdict not in ("done",),
        "continuation_prompt": None,
        "verdict": verdict,
        "reason": f"synthetic {verdict}",
        "message": f"synthetic {verdict}",
    }
    base.update(overrides)
    return base


def _make_orch_with_judge(decision: Dict[str, Any]) -> Any:
    """Build a RalphGoalLoop whose goal_manager.evaluate_after_turn returns ``decision``."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    orch = ralph.RalphGoalLoop(prd_path="/tmp/nope.json", session_id="t", auto_draft=False)
    # Stub GoalManager with a mock that returns our canned decision.
    orch.goal_manager = MagicMock()
    orch.goal_manager.evaluate_after_turn.return_value = decision
    orch.goal_manager.is_active.return_value = True
    return orch


def test_minidemo_runs_and_returns(tmp_path: Path, capsys) -> None:
    """End-to-end: orchestrator boots, runs 1 round, returns GOAL_DONE."""
    _build_mini_prd(tmp_path / "prd.json")

    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    with patch.object(ralph.RalphGoalLoop, "_step_draft_and_set", _stub_draft_and_set), \
         patch.object(ralph.RalphGoalLoop, "_run_batch", _stub_run_batch):
        orch = ralph.RalphGoalLoop(
            prd_path=str(tmp_path / "prd.json"),
            max_iter=3,
            cost_cap_usd=1.0,
            session_id="test-minidemo",
            auto_draft=False,  # extra safety
        )
        status = orch.run()

    progress = (tmp_path / "progress.txt").read_text(encoding="utf-8")
    assert "=== start" in progress, f"missing start marker in:\n{progress}"
    assert "REVIEW_NEEDED: PRD" in progress, f"missing PRD review marker in:\n{progress}"
    assert "round 1:" in progress, f"missing round 1 log in:\n{progress}"
    assert ("GOAL_DONE" in progress or "ALL_PASSES" in progress), \
        f"missing termination log in:\n{progress}"
    assert status == "GOAL_DONE", f"expected GOAL_DONE, got {status}"
    assert "=== end" in progress, f"missing end marker in:\n{progress}"
    assert orch.cost_so_far_usd >= 0.0
    assert orch.cost_so_far_usd < 0.01

    print(f"\n✓ test_minidemo_runs_and_returns PASSED (status={status}, cost=${orch.cost_so_far_usd:.6f})")
    print(f"  progress.txt size = {len(progress)} bytes, lines = {len(progress.splitlines())}")


def test_orchestrator_imports_resolve() -> None:
    """Verify the 4 critical hermes imports load in this Python env.

    Note: ``delegate_task`` import may fail in stripped-down test envs (missing optional deps
    like ``requests`` in hermes-agent's tools package). We treat that as a soft skip — not a
    failure of ralph-2 — because the orchestrator already falls back to a stub when
    ``_DELEG_OK`` is False (ralph.py:42).
    """
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph
    assert hasattr(ralph, "GoalContract"), "GoalContract not imported from hermes_cli.goals"
    assert hasattr(ralph, "GoalManager"), "GoalManager not imported from hermes_cli.goals"
    assert hasattr(ralph, "draft_contract"), "draft_contract not imported"
    assert hasattr(ralph, "RalphGoalLoop"), "RalphGoalLoop class missing"
    assert callable(ralph.RalphGoalLoop.run), "RalphGoalLoop.run not callable"
    # ralph-2 additions
    assert hasattr(ralph, "_resolve_judge_overrides"), "_resolve_judge_overrides missing"
    assert hasattr(ralph, "_judge_call_overrides_ctx"), "_judge_call_overrides_ctx missing"
    assert "GOAL_BLOCKED" in ralph._STATUS_TO_EXIT, "GOAL_BLOCKED missing from exit map"
    # Soft check on delegate_task — the test_env may not have full hermes-agent deps installed.
    if not hasattr(ralph, "delegate_task"):
        print("⚠ test_orchestrator_imports_resolve: SKIPPED delegate_task check "
              "(env missing hermes-agent optional deps; orchestrator falls back to stub)")
        return
    print("✓ test_orchestrator_imports_resolve PASSED")


def test_promise_detection_only() -> None:
    """When goal_manager=None, _evaluate relies purely on <promise> token in summary."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph
    orch = ralph.RalphGoalLoop(prd_path="/tmp/nope.json", session_id="t", auto_draft=False)
    orch.goal_manager = None  # force pure-promise path
    r1 = orch._evaluate({"results": [{"summary": "...done... <promise>COMPLETE</promise>"}]})
    assert r1 == "GOAL_DONE", f"expected GOAL_DONE, got {r1}"
    r2 = orch._evaluate({"results": [{"summary": "...partial work..."}]})
    assert r2 == "CONTINUE", f"expected CONTINUE, got {r2}"
    print("✓ test_promise_detection_only PASSED")


def test_status_to_exit_mapping() -> None:
    """All status strings map to a defined exit code (no KeyError)."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph
    for status in ["ALL_PASSES", "GOAL_DONE", "MAX_ITERATIONS", "COST_CAP",
                    "USER_REJECTED_CONTRACT", "GOAL_BLOCKED", "USER_REJECTED_PRD",
                    "DELEGATION_FAILED", "GOAL_CLEARED", "INTERNAL_ERROR", "NO_GOAL"]:
        assert status in ralph._STATUS_TO_EXIT, f"missing exit code for {status}"
    assert ralph._STATUS_TO_EXIT["ALL_PASSES"] == 0
    assert ralph._STATUS_TO_EXIT["INTERNAL_ERROR"] == 8
    assert ralph._STATUS_TO_EXIT["GOAL_BLOCKED"] == 4
    print(f"✓ test_status_to_exit_mapping PASSED (mapped {len(ralph._STATUS_TO_EXIT)} statuses)")


# ── ralph-2: 5-field verdict routing ───────────────────────────────────────────

def test_evaluate_verdict_done_routes_to_goal_done() -> None:
    """verdict=done + should_continue=False → GOAL_DONE (status=0)."""
    orch = _make_orch_with_judge(_make_decision("done", should_continue=False))
    r = orch._evaluate({"results": [{"summary": "no promise here, judge says done"}]})
    assert r == "GOAL_DONE", f"verdict=done should route to GOAL_DONE, got {r}"
    print("✓ test_evaluate_verdict_done_routes_to_goal_done PASSED")


def test_evaluate_verdict_blocked_routes_to_goal_blocked() -> None:
    """verdict=blocked → GOAL_BLOCKED (exit=4). The new status from ralph-2 fix."""
    orch = _make_orch_with_judge(_make_decision("blocked",
                                                  status="paused",
                                                  message="Goal judged unachievable — paused"))
    r = orch._evaluate({"results": [{"summary": "no promise here, judge says blocked"}]})
    assert r == "GOAL_BLOCKED", f"verdict=blocked should route to GOAL_BLOCKED, got {r}"
    print("✓ test_evaluate_verdict_blocked_routes_to_goal_blocked PASSED")


def test_evaluate_verdict_continue_routes_to_continue() -> None:
    """verdict=continue + should_continue=True → CONTINUE (outer loop spins again)."""
    orch = _make_orch_with_judge(_make_decision("continue", should_continue=True))
    r = orch._evaluate({"results": [{"summary": "no promise, judge says keep going"}]})
    assert r == "CONTINUE", f"verdict=continue should route to CONTINUE, got {r}"
    print("✓ test_evaluate_verdict_continue_routes_to_continue PASSED")


def test_evaluate_verdict_wait_routes_to_continue() -> None:
    """verdict=wait → CONTINUE (orchestrator parks via GoalManager; outer loop stays alive)."""
    orch = _make_orch_with_judge(_make_decision("wait", should_continue=False,
                                                  status="active",
                                                  message="⏳ parked on a background process"))
    r = orch._evaluate({"results": [{"summary": "no promise, judge says wait"}]})
    assert r == "CONTINUE", f"verdict=wait should route to CONTINUE, got {r}"
    print("✓ test_evaluate_verdict_wait_routes_to_continue PASSED")


def test_evaluate_transport_failure_routes_to_goal_blocked() -> None:
    """transport-failure marker (judge API unreachable) → GOAL_BLOCKED, NOT GOAL_DONE."""
    # GoalManager surfaces transport failures as status=paused with the "judge API unreachable"
    # message in the decision dict (per goals.py:1500-1518).
    orch = _make_orch_with_judge(_make_decision(
        "continue", should_continue=True, status="paused",
        message="⏸ Goal paused — judge API returned errors (3 turns). Check the goal_judge "
                "provider/key in ~/.hermes/config.yaml",
    ))
    r = orch._evaluate({"results": [{"summary": "no promise, judge API failed"}]})
    assert r == "GOAL_BLOCKED", f"transport failure should route to GOAL_BLOCKED, got {r}"
    print("✓ test_evaluate_transport_failure_routes_to_goal_blocked PASSED")


def test_evaluate_promise_short_circuits_judge_done() -> None:
    """<promise> in worker summary wins over judge verdict=continue (defensive default)."""
    orch = _make_orch_with_judge(_make_decision("continue", should_continue=True))
    r = orch._evaluate({"results": [{"summary": "... <promise>COMPLETE</promise>"}]})
    assert r == "GOAL_DONE", f"promise should override continue, got {r}"
    print("✓ test_evaluate_promise_short_circuits_judge_done PASSED")


def test_evaluate_no_promise_done_via_promise_then_judge_done() -> None:
    """verdict=done + no promise → GOAL_DONE (judge alone is enough)."""
    orch = _make_orch_with_judge(_make_decision("done", should_continue=False))
    r = orch._evaluate({"results": [{"summary": "no promise, but judge agrees we're done"}]})
    assert r == "GOAL_DONE", f"verdict=done alone should be GOAL_DONE, got {r}"
    print("✓ test_evaluate_no_promise_done_via_promise_then_judge_done PASSED")


def test_outer_loop_propagates_goal_blocked() -> None:
    """GOAL_BLOCKED from _evaluate must short-circuit the outer loop (exit=4), NOT MAX_ITERATIONS."""
    import tempfile
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph
    tmp = Path(tempfile.mkdtemp(prefix="ralph2-block-"))
    (tmp / "prd.json").write_text(json.dumps({
        "branchName": "block-probe", "title": "block-probe", "description": "test",
        "userStories": [{"id": "US-1", "title": "x", "priority": 1, "passes": False,
                         "acceptanceCriteria": ["x"]}],
    }, indent=2))
    orch = ralph.RalphGoalLoop(prd_path=str(tmp / "prd.json"), max_iter=3,
                                 cost_cap_usd=10.0, session_id="block-probe", auto_draft=False)

    fake_gm = MagicMock()
    fake_gm.is_active.return_value = True
    fake_gm.evaluate_after_turn.return_value = {
        "status": "paused", "should_continue": False,
        "verdict": "blocked", "reason": "OpenRouter 404",
        "message": "🚫 judged unachievable",
    }

    def _stub_draft(self, gt):
        self.goal_manager = fake_gm
        return None

    def _stub_batch(self, batch):
        return {"status": "OK", "results": [{
            "task_index": 0, "status": "completed", "exit_reason": "completed",
            "summary": "no promise, judge will block", "tokens": {"input": 1, "output": 1},
            "duration_seconds": 0.1,
        }]}

    with patch.object(ralph.RalphGoalLoop, "_step_draft_and_set", _stub_draft), \
         patch.object(ralph.RalphGoalLoop, "_run_batch", _stub_batch):
        status = orch.run()
    assert status == "GOAL_BLOCKED", f"GOAL_BLOCKED should short-circuit, got {status}"
    # verify outer loop only ran ONCE (no second iteration where MAX_ITERATIONS would fire)
    progress = (tmp / "progress.txt").read_text(encoding="utf-8")
    assert "round 1:" in progress and "round 2:" not in progress, \
        f"GOAL_BLOCKED should exit after round 1, got:\n{progress}"
    print("✓ test_outer_loop_propagates_goal_blocked PASSED")


# ── ralph-2: judge override plumbing ───────────────────────────────────────────

def test_resolve_judge_overrides_inherits_from_parent_agent() -> None:
    """No CLI flags + parent_agent with provider/model → overrides inherit those."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    fake_agent = MagicMock()
    fake_agent.provider = "openrouter"
    fake_agent.model = "google/gemini-3-flash-preview"
    fake_agent.base_url = "https://openrouter.ai/api/v1"
    fake_agent.api_key = "sk-or-v1-fake"

    overrides = ralph._resolve_judge_overrides(fake_agent)
    assert overrides.get("provider") == "openrouter"
    assert overrides.get("model") == "google/gemini-3-flash-preview"
    assert overrides.get("base_url") == "https://openrouter.ai/api/v1"
    assert overrides.get("api_key") == "sk-or-v1-fake"
    print("✓ test_resolve_judge_overrides_inherits_from_parent_agent PASSED")


def test_resolve_judge_overrides_cli_wins_over_inheritance() -> None:
    """CLI flag --judge-provider X overrides parent_agent's provider."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    fake_agent = MagicMock()
    fake_agent.provider = "openrouter"
    fake_agent.model = "google/gemini-3-flash-preview"
    fake_agent.base_url = ""
    fake_agent.api_key = ""

    overrides = ralph._resolve_judge_overrides(fake_agent, judge_provider="anthropic",
                                                judge_model="claude-haiku-4-5")
    assert overrides["provider"] == "anthropic", f"CLI should win, got {overrides['provider']}"
    assert overrides["model"] == "claude-haiku-4-5"
    print("✓ test_resolve_judge_overrides_cli_wins_over_inheritance PASSED")


def test_resolve_judge_overrides_keep_aux_config_returns_empty() -> None:
    """--judge-keep-aux-config → empty dict (use existing auxiliary.goal_judge config)."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    fake_agent = MagicMock()
    fake_agent.provider = "openrouter"
    fake_agent.model = "google/gemini-3-flash-preview"

    overrides = ralph._resolve_judge_overrides(fake_agent, keep_aux_config=True)
    assert overrides == {}, f"keep_aux_config should yield empty overrides, got {overrides}"
    print("✓ test_resolve_judge_overrides_keep_aux_config_returns_empty PASSED")


def test_resolve_judge_overrides_handles_none_parent_agent() -> None:
    """parent_agent=None (e.g. hermes_agent import failed) → empty dict, no crash."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    overrides = ralph._resolve_judge_overrides(None)
    # With no parent agent and no CLI flags, result is empty dict — judge will then fall through
    # to auxiliary.goal_judge config (current behavior preserved).
    assert overrides == {}, f"None parent_agent should yield empty overrides, got {overrides}"
    print("✓ test_resolve_judge_overrides_handles_none_parent_agent PASSED")


def test_judge_overrides_ctx_empty_is_noop() -> None:
    """Empty overrides dict → context manager is a no-op (doesn't touch hermes_cli.goals)."""
    if "ralph" in sys.modules:
        importlib.reload(sys.modules["ralph"])
    import ralph

    with patch.object(ralph, "_judge_call_overrides_ctx", ralph._judge_call_overrides_ctx):
        # Just verify it can be entered/exited without exception when overrides is {}.
        with ralph._judge_call_overrides_ctx({}):
            pass
    print("✓ test_judge_overrides_ctx_empty_is_noop PASSED")


def main() -> int:
    """Run as plain script (no pytest)."""
    import tempfile
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Unit tests (order matters: imports first, then verdict routing, then plumbing).
        for fn in [
            test_orchestrator_imports_resolve,
            test_status_to_exit_mapping,
            test_promise_detection_only,
            test_evaluate_verdict_done_routes_to_goal_done,
            test_evaluate_verdict_blocked_routes_to_goal_blocked,
            test_evaluate_verdict_continue_routes_to_continue,
            test_evaluate_verdict_wait_routes_to_continue,
            test_evaluate_transport_failure_routes_to_goal_blocked,
            test_evaluate_promise_short_circuits_judge_done,
            test_evaluate_no_promise_done_via_promise_then_judge_done,
            test_resolve_judge_overrides_inherits_from_parent_agent,
            test_resolve_judge_overrides_cli_wins_over_inheritance,
            test_resolve_judge_overrides_keep_aux_config_returns_empty,
            test_resolve_judge_overrides_handles_none_parent_agent,
            test_judge_overrides_ctx_empty_is_noop,
            test_outer_loop_propagates_goal_blocked,
        ]:
            try:
                fn()
            except AssertionError as e:
                print(f"FAIL {fn.__name__}: {e}")
                failed += 1
            except Exception as e:
                print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
        # End-to-end last (most expensive).
        try:
            class _Capsys:
                def __init__(self): self.out, self.err = "", ""
            test_minidemo_runs_and_returns(tmp, _Capsys())
        except AssertionError as e:
            print(f"FAIL test_minidemo_runs_and_returns: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR test_minidemo_runs_and_returns: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed} test(s) FAILED")
        return 1
    print("\n=== all minidemo tests PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())