#!/usr/bin/env python3
"""
test_recon.py — unit tests for the recon (codebase grounding) pass and the learnings merge.

Why these tests exist: `recon` is the fix for the failure mode where a delegated worker received
story text carrying paper acceptance criteria, guessed the interfaces, and produced code that only
failed later at compile time (peer-contract US-008: 6 interface-level errors, every one of them
discovered only AFTER the worker had timed out, every one fixed by hand by the orchestrator).

What is verified here — all offline, no API calls, no real delegate_task, no state.db writes:
 1. _render_recon_prompt carries the stories, the paper criteria, the hard rules, the output format
 2. _parse_recon_block parses a block, tolerates ```json fences, returns None on garbage/absence
 3. _render_grounding_block renders interfaces/files/conventions/traps; empty when no recon ran
 4. _render_worker_prompt injects the grounding block and the shared facts
 5. _read_shared_facts reads EVERY Grounding / Codebase Patterns section (both are appended to
    repeatedly over a run, so reading only the first would silently lose most of the knowledge)
 6. _merge_learnings extracts Pattern/Gotcha/File lines and de-duplicates them
 7. _step_recon end-to-end with a mocked delegate_task: the ORCHESTRATOR — never a worker — writes
    the findings into prd.json and progress.txt, and the rewritten criteria replace the paper ones
 8. _step_recon is non-fatal: unparsable output from every worker leaves prd.json untouched
 9. --no-recon disables the pass entirely (delegate_task is never called)

Run:  python scripts/test_recon.py
or:   pytest scripts/test_recon.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Path setup so we can import the orchestrator, same as test_minidemo.py.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(Path(r"C:/Users/Administrator/.hermes/hermes-agent")))

import ralph  # noqa: E402

# Never construct a real AIAgent in these tests: the class attribute is pre-set so
# _init_parent_agent() takes its early-return path (it is shared class state, so this must be
# restored afterwards or later tests inherit a fake).
_ORIGINAL_PARENT_AGENT = None


def setUpModule():
    global _ORIGINAL_PARENT_AGENT
    _ORIGINAL_PARENT_AGENT = ralph.RalphGoalLoop.parent_agent
    ralph.RalphGoalLoop.parent_agent = object()


def tearDownModule():
    ralph.RalphGoalLoop.parent_agent = _ORIGINAL_PARENT_AGENT


def _write_prd(path: Path, stories: list) -> None:
    path.write_text(json.dumps({
        "branchName": "ralph-recon-test",
        "title": "Recon test prd",
        "description": "Exercises the recon pass",
        "userStories": stories,
    }, indent=2), encoding="utf-8")


def _paper_story(sid: str = "US-001") -> dict:
    return {
        "id": sid,
        "title": "Create tools/spawn-bus-session.ts",
        "description": "As a developer, I need the tool to exist.",
        # deliberately a paper criterion: an aspiration sentence, no command, no real signature
        "acceptanceCriteria": ["Uses context.api.runtime.agent.session.createSessionEntry",
                               "Typecheck passes"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }


def _recon_summary(sid: str = "US-001", fenced: bool = False) -> str:
    payload = json.dumps({"stories": [{
        "id": sid,
        "files": [{"path": "tools/spawn-bus-session.ts", "exists": False}],
        "interfaces": [{"name": "defineToolPlugin",
                        "signature": "defineToolPlugin<TConfigSchema>(...) -> DefinedToolPluginEntry",
                        "evidence": "plugin-sdk/tool-plugin.d.ts:107"}],
        "conventions": [{"fact": "state modules export top-level functions, not instance methods",
                         "evidence": "state/role-registry.ts:37,47"}],
        "gotchas": [{"fact": "import path is plugin-sdk/tool-plugin, NOT plugin-sdk/core",
                     "evidence": "plugin-sdk/tool-plugin.d.ts:107"}],
        "acceptanceCriteria": ["`npx tsc --noEmit` exits 0",
                               "`spawn-bus-session` imports defineToolPlugin from "
                               "openclaw/plugin-sdk/tool-plugin"],
    }]}, indent=2)
    if fenced:
        return "Here is what I found.\n\n<recon>\n```json\n" + payload + "\n```\n</recon>\n"
    return "Here is what I found.\n\n<recon>\n" + payload + "\n</recon>\n"


class _FakeDelegate:
    """Stands in for the module-level delegate_task. Records calls, replays canned summaries."""

    def __init__(self, summaries):
        self.summaries = list(summaries)
        self.calls = []

    def __call__(self, tasks=None, parent_agent=None, background=False, **kw):
        self.calls.append({"tasks": tasks, "background": background})
        return json.dumps({"results": [
            {"summary": s, "tokens": {"input": 1000, "output": 500}} for s in self.summaries
        ]})


class ReconPromptTests(unittest.TestCase):
    def test_prompt_carries_stories_criteria_rules_and_format(self):
        prompt = ralph._render_recon_prompt("C:/proj", [_paper_story("US-008")])
        self.assertIn("C:/proj", prompt)
        self.assertIn("US-008", prompt)
        # the paper criteria must be shown to the recon worker as-is
        self.assertIn("Uses context.api.runtime.agent.session.createSessionEntry", prompt)
        # the anti-hallucination rules must be present
        self.assertIn("NOT FOUND", prompt)
        self.assertIn("path:line", prompt)
        self.assertIn("READ ONLY", prompt)
        # and the machine-readable output contract
        self.assertIn("<recon>", prompt)
        self.assertIn("acceptanceCriteria", prompt)

    def test_prompt_covers_every_story_in_the_group(self):
        prompt = ralph._render_recon_prompt("C:/proj", [_paper_story("US-001"), _paper_story("US-002")])
        self.assertIn("US-001", prompt)
        self.assertIn("US-002", prompt)


class ParseReconTests(unittest.TestCase):
    def test_parses_plain_block(self):
        parsed = ralph._parse_recon_block(_recon_summary())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["stories"][0]["id"], "US-001")

    def test_tolerates_json_code_fence(self):
        parsed = ralph._parse_recon_block(_recon_summary(fenced=True))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["stories"][0]["interfaces"][0]["name"], "defineToolPlugin")

    def test_returns_none_without_block(self):
        self.assertIsNone(ralph._parse_recon_block("I could not find anything, sorry."))
        self.assertIsNone(ralph._parse_recon_block(""))

    def test_returns_none_on_malformed_json(self):
        self.assertIsNone(ralph._parse_recon_block("<recon>\n{not json at all}\n</recon>"))

    def test_returns_none_when_json_is_not_an_object(self):
        self.assertIsNone(ralph._parse_recon_block("<recon>\n[1, 2, 3]\n</recon>"))


class GroundingRenderTests(unittest.TestCase):
    def test_empty_when_no_recon(self):
        self.assertEqual(ralph._render_grounding_block("US-001", {}), "")

    def test_renders_all_four_sections_with_evidence(self):
        recon = ralph._parse_recon_block(_recon_summary())["stories"][0]
        block = ralph._render_grounding_block("US-001", recon)
        self.assertIn("defineToolPlugin", block)
        self.assertIn("plugin-sdk/tool-plugin.d.ts:107", block)
        self.assertIn("tools/spawn-bus-session.ts", block)
        self.assertIn("does NOT exist yet", block)
        self.assertIn("top-level functions", block)
        self.assertIn("NOT plugin-sdk/core", block)

    def test_missing_evidence_is_flagged_not_silently_dropped(self):
        block = ralph._render_grounding_block("US-001", {
            "interfaces": [{"name": "foo", "signature": "foo(): void"}]})
        self.assertIn("NO EVIDENCE", block)


class WorkerPromptTests(unittest.TestCase):
    def test_injects_grounding_and_shared_facts(self):
        story = _paper_story()
        story["recon"] = ralph._parse_recon_block(_recon_summary())["stories"][0]
        story["acceptanceCriteria"] = ["`npx tsc --noEmit` exits 0"]
        prompt = ralph._render_worker_prompt(Path("prd.json"), Path("progress.txt"), story,
                                             shared_facts="- Pattern: something [a.ts:1]")
        self.assertIn("## Grounding for US-001", prompt)
        self.assertIn("plugin-sdk/tool-plugin.d.ts:107", prompt)
        self.assertIn("## Facts already established about this codebase", prompt)
        self.assertIn("something [a.ts:1]", prompt)
        # the tail: workers must report what they learned
        self.assertIn("- Pattern:", prompt)
        self.assertIn("- Gotcha:", prompt)
        # quality bar that upstream Ralph had and the port had dropped
        self.assertIn("Follow the existing code patterns", prompt)
        self.assertIn("Do NOT commit broken code", prompt)

    def test_no_facts_section_when_nothing_known_yet(self):
        prompt = ralph._render_worker_prompt(Path("prd.json"), Path("progress.txt"), _paper_story())
        self.assertNotIn("## Facts already established", prompt)
        self.assertNotIn("## Grounding for", prompt)


class SharedFactsTests(unittest.TestCase):
    def test_reads_every_appended_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story()])
            prog = Path(tmp) / "progress.txt"
            prog.write_text(
                "# Ralph progress\n\n"
                "## Grounding\n"
                "- Interface `a` — a(): void [a.ts:1]\n\n"
                "## [US-001]\nworker stuff\n\n"
                "## Grounding\n"
                "- Interface `b` — b(): void [b.ts:2]\n\n"
                "## Codebase Patterns\n"
                "- Pattern: use x [c.ts:3]\n",
                encoding="utf-8")

            orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
            facts = orch._read_shared_facts()
            # both Grounding sections AND the Codebase Patterns section must be present
            self.assertIn("a.ts:1", facts)
            self.assertIn("b.ts:2", facts)
            self.assertIn("c.ts:3", facts)
            self.assertNotIn("worker stuff", facts)

    def test_returns_empty_string_when_no_progress_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story()])
            orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
            self.assertEqual(orch._read_shared_facts(), "")

    def _multi_section_orch(self, tmp: str):
        prd_path = Path(tmp) / "prd.json"
        _write_prd(prd_path, [_paper_story()])
        prog = Path(tmp) / "progress.txt"
        prog.write_text("## Grounding\n" + ("- Interface `x` — x(): void [a.ts:1]\n" * 30)
                        + "## Codebase Patterns\n- Pattern: use y [b.ts:2]\n", encoding="utf-8")
        return ralph.RalphGoalLoop(str(prd_path), recon=True)

    def test_non_positive_budget_shares_nothing(self):
        """Regression: `out[-0:]` is `out[0:]` in Python, so a budget of 0 used to return the
        ENTIRE shared context instead of none, and a negative budget silently sliced from the
        wrong end. Found by running the recon pass against this very codebase."""
        with tempfile.TemporaryDirectory() as tmp:
            orch = self._multi_section_orch(tmp)
            available = len(orch._read_shared_facts())
            self.assertGreater(available, 0)  # the fixture must have content to truncate
            self.assertEqual(orch._read_shared_facts(limit_chars=0), "")
            self.assertEqual(orch._read_shared_facts(limit_chars=-1), "")
            self.assertEqual(orch._read_shared_facts(limit_chars=-100000), "")

    def test_budget_truncates_to_exactly_the_requested_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self._multi_section_orch(tmp)
            for n in (5, 40, 120):
                self.assertEqual(len(orch._read_shared_facts(limit_chars=n)), n)

    def test_budget_keeps_the_most_recent_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self._multi_section_orch(tmp)
            tail = orch._read_shared_facts(limit_chars=40)
            self.assertTrue(orch._read_shared_facts().endswith(tail),
                            "truncation must keep the TAIL (newest knowledge), not the head")

    def test_oversized_budget_returns_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = self._multi_section_orch(tmp)
            full = orch._read_shared_facts()
            self.assertEqual(orch._read_shared_facts(limit_chars=10_000_000), full)


class MergeLearningsTests(unittest.TestCase):
    def test_extracts_and_dedupes_learnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story()])
            orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
            batch = {"results": [
                {"summary": "## [US-001]\n- Pattern: state uses top-level exports [r.ts:47]\n"
                            "- Gotcha: wrong import path [p.d.ts:107]\n"
                            "- File: types are in types.ts [types.ts:1]"},
                {"summary": "## [US-002]\n- Pattern: state uses top-level exports [r.ts:47]\n"
                            "- Gotcha: fresh one [q.ts:9]"},
                {"summary": "nothing learned here"},
            ]}
            orch._merge_learnings(batch)
            prog = (Path(tmp) / "progress.txt").read_text(encoding="utf-8")
            self.assertIn("## Codebase Patterns", prog)
            self.assertIn("- Pattern: state uses top-level exports [r.ts:47]", prog)
            self.assertIn("- Gotcha: wrong import path [p.d.ts:107]", prog)
            self.assertIn("- File: types are in types.ts [types.ts:1]", prog)
            self.assertIn("- Gotcha: fresh one [q.ts:9]", prog)
            # the duplicate across the two workers must appear once
            self.assertEqual(prog.count("state uses top-level exports"), 1)

    def test_no_section_written_when_nothing_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story()])
            orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
            orch._merge_learnings({"results": [{"summary": "just prose, no labels"}]})
            self.assertFalse((Path(tmp) / "progress.txt").exists())


class StepReconTests(unittest.TestCase):
    """The core contract: recon workers report, the ORCHESTRATOR writes."""

    def setUp(self):
        self._saved_parent = ralph.RalphGoalLoop.parent_agent
        ralph.RalphGoalLoop.parent_agent = object()  # skip real AIAgent construction

    def tearDown(self):
        ralph.RalphGoalLoop.parent_agent = self._saved_parent

    def test_grounds_prd_and_writes_progress_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story("US-001")])
            fake = _FakeDelegate([_recon_summary("US-001")])

            with patch.object(ralph, "delegate_task", fake), \
                 patch.object(ralph, "_DELEG_OK", True):
                orch = ralph.RalphGoalLoop(str(prd_path), recon=True, recon_group=3)
                prd = json.loads(prd_path.read_text(encoding="utf-8"))
                out = orch._step_recon(prd)

            story = out["userStories"][0]
            # the paper criteria were REPLACED by the grounded ones
            self.assertEqual(story["acceptanceCriteria"],
                             ["`npx tsc --noEmit` exits 0",
                              "`spawn-bus-session` imports defineToolPlugin from "
                              "openclaw/plugin-sdk/tool-plugin"])
            # and the findings are attached to the story for the worker prompt
            self.assertIn("recon", story)
            self.assertEqual(story["recon"]["interfaces"][0]["evidence"],
                             "plugin-sdk/tool-plugin.d.ts:107")
            # the ORCHESTRATOR wrote prd.json back to disk
            on_disk = json.loads(prd_path.read_text(encoding="utf-8"))
            self.assertIn("recon", on_disk["userStories"][0])
            # and it wrote the shared Grounding section
            prog = (Path(tmp) / "progress.txt").read_text(encoding="utf-8")
            self.assertIn("## Grounding", prog)
            self.assertIn("plugin-sdk/tool-plugin.d.ts:107", prog)
            # recon cost was accounted for
            self.assertGreater(orch.cost_so_far_usd, 0)
            # and the worker got a prompt telling it to ground on the code, not to guess
            self.assertIn("READ ONLY", fake.calls[0]["tasks"][0]["goal"])

    def test_groups_stories_into_one_worker_per_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story("US-001"), _paper_story("US-002"),
                                  _paper_story("US-003"), _paper_story("US-004")])
            fake = _FakeDelegate([_recon_summary("US-001")])
            with patch.object(ralph, "delegate_task", fake), \
                 patch.object(ralph, "_DELEG_OK", True):
                orch = ralph.RalphGoalLoop(str(prd_path), recon=True, recon_group=3)
                orch._step_recon(json.loads(prd_path.read_text(encoding="utf-8")))
            # 4 stories / group of 3  →  2 recon workers fanned out in ONE batch
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(fake.calls[0]["tasks"]), 2)

    def test_unparsable_workers_leave_prd_untouched_and_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story("US-001")])
            before = prd_path.read_text(encoding="utf-8")
            fake = _FakeDelegate(["I got confused and wrote no block at all.",
                                 "<recon>{broken json</recon>"])
            with patch.object(ralph, "delegate_task", fake), \
                 patch.object(ralph, "_DELEG_OK", True):
                orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
                orch._step_recon(json.loads(prd_path.read_text(encoding="utf-8")))
            # _step_recon must not write a `## Grounding` section it cannot stand behind.
            # (_log() legitimately creates progress.txt, which is why this asserts on content,
            # not on the file's existence.)
            self.assertEqual(prd_path.read_text(encoding="utf-8"), before)
            prog_path = Path(tmp) / "progress.txt"
            prog_text = prog_path.read_text(encoding="utf-8") if prog_path.exists() else ""
            self.assertNotIn("## Grounding", prog_text)
            self.assertNotIn("### US-001", prog_text)

    def test_delegate_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story("US-001")])
            before = prd_path.read_text(encoding="utf-8")

            def boom(**kw):
                raise RuntimeError("transport exploded")

            with patch.object(ralph, "delegate_task", boom), \
                 patch.object(ralph, "_DELEG_OK", True):
                orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
                orch._step_recon(json.loads(prd_path.read_text(encoding="utf-8")))
            self.assertEqual(prd_path.read_text(encoding="utf-8"), before)

    def test_no_recon_skips_the_pass_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story("US-001")])
            fake = _FakeDelegate([_recon_summary("US-001")])
            with patch.object(ralph, "delegate_task", fake), \
                 patch.object(ralph, "_DELEG_OK", True):
                orch = ralph.RalphGoalLoop(str(prd_path), recon=False)
                out = orch._step_recon(json.loads(prd_path.read_text(encoding="utf-8")))
            self.assertEqual(fake.calls, [])
            self.assertNotIn("recon", out["userStories"][0])

    def test_already_passed_stories_are_not_reconned(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            done = _paper_story("US-001")
            done["passes"] = True
            _write_prd(prd_path, [done, _paper_story("US-002")])
            fake = _FakeDelegate([_recon_summary("US-002")])
            with patch.object(ralph, "delegate_task", fake), \
                 patch.object(ralph, "_DELEG_OK", True):
                orch = ralph.RalphGoalLoop(str(prd_path), recon=True)
                orch._step_recon(json.loads(prd_path.read_text(encoding="utf-8")))
            goal = fake.calls[0]["tasks"][0]["goal"]
            self.assertIn("US-002", goal)
            self.assertNotIn("US-001", goal)


class CliWiringTests(unittest.TestCase):
    def test_cli_flags_reach_the_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            prd_path = Path(tmp) / "prd.json"
            _write_prd(prd_path, [_paper_story()])
            captured = {}

            real_init = ralph.RalphGoalLoop.__init__

            def spy_init(self, *a, **kw):
                captured.update(kw)
                real_init(self, *a, **kw)

            with patch.object(ralph.RalphGoalLoop, "__init__", spy_init), \
                 patch.object(ralph.RalphGoalLoop, "run", lambda self: "ALL_PASSES"), \
                 patch.object(ralph, "_resolve_judge_overrides", lambda *a, **kw: {}):
                rc = ralph.main(["--prd", str(prd_path), "--no-recon",
                                 "--recon-group", "7", "--project-root", tmp])
            self.assertEqual(rc, 0)
            self.assertFalse(captured["recon"])
            self.assertEqual(captured["recon_group"], 7)
            self.assertEqual(captured["project_root"], tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
