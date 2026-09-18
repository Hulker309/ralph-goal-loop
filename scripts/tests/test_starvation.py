#!/usr/bin/env python3
"""
test_starvation.py — tests for priority starvation and the failure/benching guard.

The bug being fixed: grouping by `min(priority)` made a story that could not pass into a wall.
It stayed `passes: false`, so it kept being the lowest priority, so every story behind it waited
forever while the run retried the same broken story until max_iterations — the whole budget spent
on one story, nothing else attempted. Two changes make that impossible:

  1. Priority ORDERS, it does not GATE. `dependsOn` decides what may run (that is a real
     constraint); priority only decides what to reach for first.
  2. A dispatched story that comes back still-not-passing is charged an ATTEMPT and, after
     `max_story_attempts`, BENCHED — skipped so the rest of the prd keeps moving. Stories waiting
     on a benched (or non-existent) dependency are recognised transitively as unreachable, and the
     run stops with STORIES_EXHAUSTED (exit 11) and an account of what died and why, instead of
     limping to MAX_ITERATIONS with no explanation.

All offline: no API calls, no delegate_task, no state.db.

Run:  python scripts/tests/test_starvation.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_SKILL_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_SKILL_ROOT / "scripts"))
sys.path.insert(0, str(Path(r"C:/Users/Administrator/.hermes/hermes-agent")))

import ralph  # noqa: E402

_ORIGINAL_PARENT_AGENT = None


def setUpModule():
    global _ORIGINAL_PARENT_AGENT
    _ORIGINAL_PARENT_AGENT = ralph.RalphGoalLoop.parent_agent
    ralph.RalphGoalLoop.parent_agent = object()   # never build a real AIAgent


def tearDownModule():
    ralph.RalphGoalLoop.parent_agent = _ORIGINAL_PARENT_AGENT


def _story(sid, priority=1, files=None, deps=None, passes=False):
    s = {"id": sid, "title": f"story {sid}", "priority": priority, "passes": passes,
         "acceptanceCriteria": ["whatever"]}
    if files is not None:
        s["files"] = files
    if deps is not None:
        s["dependsOn"] = deps
    return s


def _write_prd(path: Path, stories) -> None:
    path.write_text(json.dumps({
        "branchName": "ralph-starvation-test", "title": "t", "description": "d",
        "userStories": stories,
    }, indent=2), encoding="utf-8")


def _read_prd(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _make_orch(tmp, stories, mode="auto", max_attempts=3, max_iter=20, max_parallel=10):
    p = Path(tmp) / "prd.json"
    _write_prd(p, stories)
    return ralph.RalphGoalLoop(str(p), recon=False, auto_draft=False,
                               parallel_mode=mode, max_parallel=max_parallel,
                               max_story_attempts=max_attempts, max_iter=max_iter)


def _worker_stub(pass_ids, calls):
    """Stand in for _run_batch: record the batch, then mark `pass_ids` as having passed.

    Only stories actually dispatched in this batch may be marked — a worker can only touch the
    story it was handed. Anything not in pass_ids stays `passes: false`, which is exactly what a
    story that failed verification looks like to the orchestrator.
    """
    def _stub(self, batch):
        calls.append([s["id"] for s in batch])
        dispatched = {s["id"] for s in batch}
        prd = _read_prd(self.prd_path)
        for s in prd["userStories"]:
            if s["id"] in pass_ids and s["id"] in dispatched:
                s["passes"] = True
        Path(self.prd_path).write_text(json.dumps(prd, indent=2), encoding="utf-8")
        return {"status": "OK",
                "results": [{"summary": "", "tokens": {"input": 1, "output": 1}} for _ in batch]}
    return _stub


def _drive(orch, pass_ids, calls):
    with patch.object(ralph.RalphGoalLoop, "_run_batch", _worker_stub(pass_ids, calls)):
        return orch._outer_loop()


class BenchingTests(unittest.TestCase):
    def test_failing_story_is_benched_at_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"])]
            orch = _make_orch(tmp, stories, max_attempts=3, max_iter=50)
            calls = []
            status = _drive(orch, pass_ids=set(), calls=calls)
            self.assertEqual(orch.attempts["A"], 3)
            self.assertIn("A", orch.benched)
            self.assertEqual(status, "STORIES_EXHAUSTED")
            self.assertEqual(len(calls), 3, "must stop after the cap, not keep retrying")

    def test_passing_story_is_never_charged_an_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"])]
            orch = _make_orch(tmp, stories)
            calls = []
            status = _drive(orch, pass_ids={"A"}, calls=calls)
            self.assertEqual(status, "ALL_PASSES")
            self.assertEqual(orch.attempts, {})
            self.assertEqual(orch.benched, set())

    def test_cap_of_zero_disables_benching(self):
        """0 = retry forever (the old behaviour), for callers who want it."""
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"])]
            orch = _make_orch(tmp, stories, max_attempts=0, max_iter=4)
            calls = []
            status = _drive(orch, pass_ids=set(), calls=calls)
            self.assertEqual(status, "MAX_ITERATIONS")
            self.assertEqual(orch.benched, set())
            self.assertEqual(len(calls), 4)


class NoStarvationTests(unittest.TestCase):
    """The core regression: one broken story must not cost the others their turn."""

    def test_stuck_story_does_not_block_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]),
                       _story("B", 2, files=["b.py"]),
                       _story("C", 3, files=["c.py"])]
            orch = _make_orch(tmp, stories, max_attempts=3, max_iter=50)
            calls = []
            status = _drive(orch, pass_ids={"B", "C"}, calls=calls)
            prd = _read_prd(orch.prd_path)
            passed = {s["id"] for s in prd["userStories"] if s["passes"]}
            self.assertEqual(passed, {"B", "C"}, "B and C must still get done")
            self.assertIn("A", orch.benched)
            self.assertEqual(status, "STORIES_EXHAUSTED",
                             "stops because only the benched story is left")

    def test_stuck_story_does_not_block_after_benching_in_priority_mode(self):
        """The legacy `priority` mode recovered too — benching is what unsticks it."""
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]),
                       _story("B", 2, files=["b.py"]),
                       _story("C", 3, files=["c.py"])]
            orch = _make_orch(tmp, stories, mode="priority", max_attempts=2, max_iter=50)
            calls = []
            _drive(orch, pass_ids={"B", "C"}, calls=calls)
            # rounds 1-2: only A (lowest priority) ; round 3+: A benched, B and C proceed
            self.assertEqual(calls[0], ["A"])
            self.assertEqual(calls[1], ["A"])
            self.assertEqual(calls[2], ["B"], "priority 2 starts once priority 1 is benched")
            self.assertEqual(calls[3], ["C"])
            prd = _read_prd(orch.prd_path)
            self.assertEqual({s["id"] for s in prd["userStories"] if s["passes"]}, {"B", "C"})

    def test_priority_orders_but_does_not_gate_in_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]),
                       _story("B", 2, files=["b.py"])]
            orch = _make_orch(tmp, stories)
            calls = []
            _drive(orch, pass_ids={"B"}, calls=calls)
            self.assertEqual(sorted(calls[0]), ["A", "B"],
                             "a lower-priority-numbered story must not lock out a ready one")

    def test_run_stops_early_instead_of_burning_max_iterations(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"])]
            orch = _make_orch(tmp, stories, max_attempts=3, max_iter=99)
            calls = []
            _drive(orch, pass_ids=set(), calls=calls)
            self.assertLess(len(calls), 99)
            self.assertEqual(len(calls), 3)


class UnreachableTests(unittest.TestCase):
    def test_ghost_dependency_is_reported_not_retried_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"], deps=["GHOST"])]
            orch = _make_orch(tmp, stories, max_iter=50)
            calls = []
            status = _drive(orch, pass_ids=set(), calls=calls)
            self.assertEqual(status, "STORIES_EXHAUSTED")
            self.assertEqual(calls, [], "no worker may run for an unreachable story")
            prog = (Path(tmp) / "progress.txt").read_text(encoding="utf-8")
            self.assertIn("GHOST", prog)

    def test_story_behind_a_benched_story_is_reported_transitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]),
                       _story("B", 2, files=["b.py"], deps=["A"]),
                       _story("C", 3, files=["c.py"], deps=["B"])]
            orch = _make_orch(tmp, stories, max_attempts=2, max_iter=50)
            calls = []
            status = _drive(orch, pass_ids=set(), calls=calls)
            self.assertEqual(status, "STORIES_EXHAUSTED")
            dead = orch._dead_stories(_read_prd(orch.prd_path)["userStories"],
                                      _read_prd(orch.prd_path)["userStories"])
            self.assertIn("A", dead)
            self.assertIn("B", dead)
            self.assertIn("C", dead, "unreachability must propagate down the chain")
            prog = (Path(tmp) / "progress.txt").read_text(encoding="utf-8")
            self.assertIn("STORIES_EXHAUSTED", prog)

    def test_dependency_cycle_is_a_scheduler_deadlock_not_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"], deps=["B"]),
                       _story("B", 2, files=["b.py"], deps=["A"])]
            orch = _make_orch(tmp, stories, max_iter=50)
            calls = []
            status = _drive(orch, pass_ids=set(), calls=calls)
            self.assertEqual(status, "NO_PROGRESS",
                             "a cycle is unsatisfiable-now, not permanently dead")
            self.assertEqual(calls, [])

    def test_dead_stories_fixpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]),
                       _story("B", 2, files=["b.py"], deps=["A"]),
                       _story("C", 3, files=["c.py"], deps=["B"]),
                       _story("D", 4, files=["d.py"])]
            orch = _make_orch(tmp, stories)
            orch.benched = {"A"}
            reasons = orch._dead_stories(stories, stories)
            self.assertEqual(set(reasons), {"A", "B", "C"}, "D is still runnable")
            self.assertIn("waiting on unreachable", reasons["B"])
            self.assertIn("waiting on unreachable", reasons["C"])


class ExitCodeTests(unittest.TestCase):
    def test_stories_exhausted_has_its_own_exit_code(self):
        self.assertIn("STORIES_EXHAUSTED", ralph._STATUS_TO_EXIT)
        self.assertEqual(ralph._STATUS_TO_EXIT["STORIES_EXHAUSTED"], 11)
        self.assertNotEqual(ralph._STATUS_TO_EXIT["STORIES_EXHAUSTED"],
                            ralph._STATUS_TO_EXIT.get("INTERNAL_ERROR"))

    def test_every_status_has_a_code(self):
        """An unmapped status silently becomes 8 via .get(status, 8)."""
        for status in ("ALL_PASSES", "MAX_ITERATIONS", "COST_CAP",
                       "DELEGATION_FAILED", "INTERNAL_ERROR", "NO_PROGRESS",
                       "STORIES_EXHAUSTED", "RUN_PAUSED"):
            self.assertIn(status, ralph._STATUS_TO_EXIT)


class WiringTests(unittest.TestCase):
    def test_cli_flag_reaches_the_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_prd(Path(tmp) / "prd.json", [_story("A")])
            captured = {}
            real_init = ralph.RalphGoalLoop.__init__

            def spy_init(self, *a, **kw):
                captured.update(kw)
                real_init(self, *a, **kw)

            with patch.object(ralph.RalphGoalLoop, "__init__", spy_init), \
                 patch.object(ralph.RalphGoalLoop, "run", lambda self: "ALL_PASSES"), \
                 patch.object(ralph, "_ADAPTER_OK", True), \
                 patch.object(ralph, "HermesAdapter", lambda **kw: None):
                rc = ralph.main(["--prd", str(Path(tmp) / "prd.json"),
                                 "--max-story-attempts", "7"])
            self.assertEqual(rc, 0)
            self.assertEqual(captured["max_story_attempts"], 7)

    def test_benching_is_reported_in_progress_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"])]
            orch = _make_orch(tmp, stories, max_attempts=1, max_iter=10)
            calls = []
            _drive(orch, pass_ids=set(), calls=calls)
            prog = (Path(tmp) / "progress.txt").read_text(encoding="utf-8")
            self.assertIn("BENCHED A", prog)


if __name__ == "__main__":
    unittest.main(verbosity=2)