#!/usr/bin/env python3
"""
test_parallel.py — unit tests for parallel batch planning.

Why these tests exist: the port shipped a fan-out that never fired. Grouping was "stories with
equal `priority`", while upstream's own `ralph` skill numbers stories 1,2,3… (one priority each),
so every batch had exactly one story and the loop ran fully serial — the parallel machinery was
dead weight no matter how many stories the prd had. These tests pin the replacement: a strategy
the caller chooses, with a default that actually parallelises safe work.

Covered (all offline, no API calls, no delegate_task, no state.db):
 1.  off       — one story per batch, lowest priority first
 2.  priority  — legacy equal-priority grouping still available
 3.  auto      — groups disjoint-file stories ACROSS priority levels
 4.  auto      — refuses to group two stories that touch the same file
 5.  auto      — a story with an unknown file set runs alone (unknown ≠ no conflict)
 6.  auto      — respects dependsOn
 7.  auto      — a dependsOn that can never be satisfied yields an empty batch + a reason
 8.  manual    — groups by the explicit parallelGroup label
 9.  manual    — obeys an overlapping manual group but says so in the reason
10.  max_parallel caps every strategy
11.  _story_files / _story_deps normalisation
12.  _outer_loop maps a scheduling deadlock to NO_PROGRESS (exit 10) instead of spinning
13.  a bad parallel_mode fails loudly at construction
14.  NO_PROGRESS is in the exit-code map (an unmapped status silently becomes 8 = INTERNAL_ERROR)

Run:  python scripts/test_parallel.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(Path(r"C:/Users/Administrator/.hermes/hermes-agent")))

import ralph  # noqa: E402

_ORIGINAL_PARENT_AGENT = None


def setUpModule():
    global _ORIGINAL_PARENT_AGENT
    _ORIGINAL_PARENT_AGENT = ralph.RalphGoalLoop.parent_agent
    ralph.RalphGoalLoop.parent_agent = object()   # never build a real AIAgent


def tearDownModule():
    ralph.RalphGoalLoop.parent_agent = _ORIGINAL_PARENT_AGENT


def _story(sid, priority=1, files=None, recon_files=None, group=None, deps=None, passes=False):
    s = {"id": sid, "title": f"story {sid}", "priority": priority, "passes": passes,
         "acceptanceCriteria": ["whatever"]}
    if files is not None:
        s["files"] = files
    if recon_files is not None:
        s["recon"] = {"files": [{"path": p, "exists": False} for p in recon_files]}
    if group is not None:
        s["parallelGroup"] = group
    if deps is not None:
        s["dependsOn"] = deps
    return s


def _write_prd(path: Path, stories) -> None:
    path.write_text(json.dumps({
        "branchName": "ralph-parallel-test", "title": "t", "description": "d",
        "userStories": stories,
    }, indent=2), encoding="utf-8")


def _orch(tmp, stories, mode="auto", max_parallel=10, write=True):
    p = Path(tmp) / "prd.json"
    if write:
        _write_prd(p, stories)
    return ralph.RalphGoalLoop(str(p), recon=False, parallel_mode=mode,
                               max_parallel=max_parallel, auto_draft=False)


def _ids(batch):
    return [s["id"] for s in batch]


class ModeTests(unittest.TestCase):
    def test_off_runs_one_story_lowest_priority_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]), _story("B", 1, files=["b.py"]),
                       _story("C", 2, files=["c.py"])]
            orch = _orch(tmp, stories, mode="off")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(_ids(batch), ["A"])
            self.assertIn("off", reason)

    def test_max_parallel_one_forces_serial_even_in_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]), _story("B", 1, files=["b.py"])]
            orch = _orch(tmp, stories, mode="auto", max_parallel=1)
            batch, _ = orch._plan_batch(stories, set())
            self.assertEqual(len(batch), 1)

    def test_priority_mode_groups_equal_priorities(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]), _story("B", 1, files=["b.py"]),
                       _story("C", 2, files=["c.py"])]
            orch = _orch(tmp, stories, mode="priority")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(sorted(_ids(batch)), ["A", "B"])
            self.assertIn("top=1", reason)


class AutoTests(unittest.TestCase):
    def test_groups_disjoint_stories_across_priorities(self):
        """The whole point: upstream numbers stories 1,2,3…; auto must still parallelise safe ones."""
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]), _story("B", 2, files=["b.py"]),
                       _story("C", 3, files=["c.py"])]
            orch = _orch(tmp, stories, mode="auto")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(sorted(_ids(batch)), ["A", "B", "C"])
            self.assertIn("mode=auto", reason)

    def test_does_not_group_overlapping_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["shared.py"]), _story("B", 1, files=["shared.py"])]
            orch = _orch(tmp, stories, mode="auto")
            batch, _ = orch._plan_batch(stories, set())
            self.assertEqual(_ids(batch), ["A"])

    def test_overlap_detected_through_recon_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, recon_files=["src/x.ts"]),
                       _story("B", 1, recon_files=["src/x.ts"])]
            orch = _orch(tmp, stories, mode="auto")
            batch, _ = orch._plan_batch(stories, set())
            self.assertEqual(len(batch), 1, "recon-reported shared file must block grouping")

    def test_unknown_file_set_runs_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1), _story("B", 2, files=["b.py"])]
            orch = _orch(tmp, stories, mode="auto")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(_ids(batch), ["B"], "the story with known files is schedulable")
            self.assertIn("deferred_unknown_files", reason)

    def test_all_unknown_still_makes_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1), _story("B", 2)]
            orch = _orch(tmp, stories, mode="auto")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(len(batch), 1, "must never return an empty batch here")
            self.assertIn("serial", reason)

    def test_respects_depends_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]),
                       _story("B", 2, files=["b.py"], deps=["A"])]
            orch = _orch(tmp, stories, mode="auto")
            batch, _ = orch._plan_batch(stories, set())
            self.assertEqual(_ids(batch), ["A"], "B is not eligible until A passes")
            batch2, _ = orch._plan_batch(
                [s for s in stories if s["id"] == "B"], {"A"})
            self.assertEqual(_ids(batch2), ["B"])

    def test_max_parallel_caps_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story(x, 1, files=[f"{x}.py"]) for x in ("A", "B", "C", "D", "E")]
            orch = _orch(tmp, stories, mode="auto", max_parallel=3)
            batch, _ = orch._plan_batch(stories, set())
            self.assertEqual(len(batch), 3)


class DeadlockTests(unittest.TestCase):
    def test_unsatisfiable_dependency_gives_empty_batch_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"], deps=["GHOST"])]
            orch = _orch(tmp, stories, mode="auto")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(batch, [])
            self.assertIn("GHOST", reason)
            self.assertIn("NOTHING RUNNABLE", reason)

    def test_outer_loop_returns_no_progress_instead_of_spinning(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"], deps=["GHOST"])]
            orch = _orch(tmp, stories, mode="auto")
            orch.max_iter = 50          # would spin 50 rounds if the guard were missing
            calls = []
            with patch.object(ralph.RalphGoalLoop, "_run_batch",
                              lambda self, b: calls.append(b) or {"status": "OK", "results": []}):
                status = orch._outer_loop()
            self.assertEqual(status, "NO_PROGRESS")
            self.assertEqual(calls, [], "no worker may be spawned when nothing is runnable")
            prog = (Path(tmp) / "progress.txt").read_text(encoding="utf-8")
            self.assertIn("NOTHING RUNNABLE", prog)

    def test_no_progress_has_an_exit_code(self):
        """An unmapped status silently becomes 8 (INTERNAL_ERROR) via .get(status, 8)."""
        self.assertIn("NO_PROGRESS", ralph._STATUS_TO_EXIT)
        self.assertEqual(ralph._STATUS_TO_EXIT["NO_PROGRESS"], 10)


class ManualTests(unittest.TestCase):
    def test_groups_by_parallel_group_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"], group="g1"),
                       _story("B", 3, files=["b.py"], group="g1"),
                       _story("C", 2, files=["c.py"], group="g2")]
            orch = _orch(tmp, stories, mode="manual")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(sorted(_ids(batch)), ["A", "B"], "group wins over priority")
            self.assertIn("g1", reason)

    def test_ungrouped_story_runs_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["a.py"]), _story("B", 1, files=["b.py"])]
            orch = _orch(tmp, stories, mode="manual")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(len(batch), 1)
            self.assertIn("no parallelGroup", reason)

    def test_overlapping_manual_group_is_obeyed_but_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story("A", 1, files=["same.py"], group="g1"),
                       _story("B", 2, files=["same.py"], group="g1")]
            orch = _orch(tmp, stories, mode="manual")
            batch, reason = orch._plan_batch(stories, set())
            self.assertEqual(len(batch), 2, "manual means the caller decides")
            self.assertIn("OVERLAPPING_FILES", reason)


class HelperTests(unittest.TestCase):
    def test_story_files_reads_recon_and_explicit_and_normalizes(self):
        s = _story("A", 1, files=["./Src/Foo.py", "src\\bar.ts"],
                   recon_files=["SRC/foo.py", "src/baz.ts"])
        got = ralph._story_files(s)
        self.assertIn("src/foo.py", got)
        self.assertIn("src/bar.ts", got)
        self.assertIn("src/baz.ts", got)

    def test_story_files_empty_when_unknown(self):
        self.assertEqual(ralph._story_files(_story("A", 1)), set())

    def test_story_deps_accepts_camel_snake_and_string(self):
        self.assertEqual(ralph._story_deps({"dependsOn": ["A", "B"]}), {"A", "B"})
        self.assertEqual(ralph._story_deps({"depends_on": "A"}), {"A"})
        self.assertEqual(ralph._story_deps({"dependsOn": "A"}), {"A"})
        self.assertEqual(ralph._story_deps({}), set())
        self.assertEqual(ralph._story_deps({"dependsOn": []}), set())


class ConstructionTests(unittest.TestCase):
    def test_bad_parallel_mode_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_prd(Path(tmp) / "prd.json", [_story("A")])
            with self.assertRaises(ValueError):
                ralph.RalphGoalLoop(str(Path(tmp) / "prd.json"), recon=False,
                                    auto_draft=False, parallel_mode="whenever")

    def test_mode_is_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_prd(Path(tmp) / "prd.json", [_story("A")])
            orch = ralph.RalphGoalLoop(str(Path(tmp) / "prd.json"), recon=False,
                                       auto_draft=False, parallel_mode=" AUTO ")
            self.assertEqual(orch.parallel_mode, "auto")

    def test_cli_flags_reach_the_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_prd(Path(tmp) / "prd.json", [_story("A")])
            captured = {}
            real_init = ralph.RalphGoalLoop.__init__

            def spy_init(self, *a, **kw):
                captured.update(kw)
                real_init(self, *a, **kw)

            with patch.object(ralph.RalphGoalLoop, "__init__", spy_init), \
                 patch.object(ralph.RalphGoalLoop, "run", lambda self: "ALL_PASSES"), \
                 patch.object(ralph, "_resolve_judge_overrides", lambda *a, **kw: {}):
                rc = ralph.main(["--prd", str(Path(tmp) / "prd.json"), "--no-recon",
                                 "--parallel-mode", "manual", "--max-parallel", "4",
                                 "--project-root", tmp])
            self.assertEqual(rc, 0)
            self.assertEqual(captured["parallel_mode"], "manual")
            self.assertEqual(captured["max_parallel"], 4)

    def test_rejects_unknown_mode_at_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_prd(Path(tmp) / "prd.json", [_story("A")])
            with self.assertRaises(SystemExit):
                ralph.main(["--prd", str(Path(tmp) / "prd.json"), "--parallel-mode", "nope"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
