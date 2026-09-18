"""RalphGoalLoop orchestrator — pure Python, no Hermes imports."""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .prd import (
    all_pass,
    get_passed_ids,
    get_pending_stories,
    normalize_prd,
    read_prd,
    story_deps,
    story_files,
    write_prd,
)
from .progress import (
    log,
    merge_codebase_patterns,
)
from .promise import PROMISE_TOKEN, detect_promise

logger = logging.getLogger("ralph-goal-loop")

DEFAULT_MAX_ITER = 10
DEFAULT_COST_CAP_USD = 5.0
_PARALLEL_MODES = ("off", "priority", "auto", "manual")
_PARALLEL_MODE_DEFAULT = "auto"
_MAX_PARALLEL_DEFAULT = 10
_MAX_STORY_ATTEMPTS_DEFAULT = 3
_WORKER_IN_USD_PER_1K = 0.003
_WORKER_OUT_USD_PER_1K = 0.015

# Backward compatibility: old tests set this to a fake object to prevent real AIAgent construction
parent_agent: Optional[object] = None


class RalphGoalLoop:
    """Ralph goal-loop orchestrator.

    Public API:
        run() -> status string

    The orchestrator reads prd.json and progress.txt each iteration, picks the
    highest-priority eligible story, asks the adapter to run it, and repeats until
    all stories pass or a terminal status is reached.

    It delegates actual LLM interaction to a PlatformAdapter so the core logic
    is platform-agnostic (HermesAdapter for Hermes, OpenCLAWAdapter for OpenCLAW).
    """

    # Backward compatibility: old tests set this to a fake object to prevent real AIAgent construction
    parent_agent: Optional[object] = None

    def __init__(
        self,
        prd_path: str,
        adapter=None,
        max_iter: int = DEFAULT_MAX_ITER,
        cost_cap_usd: float = DEFAULT_COST_CAP_USD,
        project_root: Optional[str] = None,
        parallel_mode: str = _PARALLEL_MODE_DEFAULT,
        max_parallel: int = _MAX_PARALLEL_DEFAULT,
        max_story_attempts: int = _MAX_STORY_ATTEMPTS_DEFAULT,
        # v1.0: recon and auto_draft are no-ops (recon removed in v1.0, no boss review node)
        recon: bool = False,
        auto_draft: bool = False,
    ) -> None:
        self.prd_path = Path(prd_path).resolve()
        self.progress_path = self.prd_path.parent / "progress.txt"
        self.adapter = adapter
        self.max_iter = max_iter
        self.cost_cap_usd = cost_cap_usd
        self.project_root = str(Path(project_root).resolve()) if project_root else str(Path.cwd())

        mode = (parallel_mode or _PARALLEL_MODE_DEFAULT).strip().lower()
        if mode not in _PARALLEL_MODES:
            raise ValueError(f"parallel_mode must be one of {_PARALLEL_MODES}, got {parallel_mode!r}")
        self.parallel_mode = mode
        self.max_parallel = max(1, int(max_parallel))
        self.max_story_attempts = int(max_story_attempts)

        # Failure bookkeeping
        self.attempts: Dict[str, int] = {}
        self.benched: set = set()
        self.cost_so_far_usd: float = 0.0

    # ── PRD lifecycle ─────────────────────────────────────────────────────────

    def _load_prd(self) -> Dict[str, Any]:
        """Read prd.json from disk."""
        try:
            return read_prd(self.prd_path)
        except Exception as exc:
            log(self.progress_path, f"read prd.json FAILED: {exc!r}")
            return {"userStories": []}

    def _save_prd(self, prd: Dict[str, Any]) -> None:
        """Write prd.json back to disk."""
        try:
            write_prd(self.prd_path, prd)
        except Exception as exc:
            log(self.progress_path, f"write prd.json FAILED: {exc!r}")

    # ── Batch planning ────────────────────────────────────────────────────────

    def _plan_batch(
        self, pending: List[Dict[str, Any]], passed_ids: set
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Return (batch, reason). An empty batch means nothing is runnable this round."""
        mode = self.parallel_mode
        if mode == "off" or self.max_parallel <= 1:
            one = min(pending, key=lambda s: s.get("priority", 99))
            return [one], f"mode={mode} (serial)"
        if mode == "priority":
            top = min(s.get("priority", 99) for s in pending)
            batch = [s for s in pending if s.get("priority", 99) == top]
            return batch[: self.max_parallel], f"mode=priority top={top}"
        if mode == "manual":
            return self._plan_manual(pending, passed_ids)
        return self._plan_auto(pending, passed_ids)

    def _plan_auto(
        self, pending: List[Dict[str, Any]], passed_ids: set
    ) -> Tuple[List[Dict[str, Any]], str]:
        eligible = [s for s in pending if story_deps(s) <= passed_ids]
        if not eligible:
            return [], self._unrunnable_reason(pending, passed_ids)
        eligible.sort(key=lambda s: s.get("priority", 99))
        chosen: List[Dict[str, Any]] = []
        used: set = set()
        unknown: List[str] = []
        for s in eligible:
            if len(chosen) >= self.max_parallel:
                break
            files = story_files(s)
            if not files:
                unknown.append(str(s.get("id", "?")))
                continue
            if files & used:
                continue
            chosen.append(s)
            used |= files
        if not chosen:
            one = eligible[0]
            return [one], (f"mode=auto serial — no disjoint pair available "
                           f"(unknown file set for {unknown[:4] or 'all'})")
        reason = f"mode=auto batch={len(chosen)}"
        if unknown:
            reason += f" deferred_unknown_files={unknown[:4]}"
        return chosen, reason

    def _plan_manual(
        self, pending: List[Dict[str, Any]], passed_ids: set
    ) -> Tuple[List[Dict[str, Any]], str]:
        eligible = [s for s in pending if story_deps(s) <= passed_ids]
        if not eligible:
            return [], self._unrunnable_reason(pending, passed_ids)
        eligible.sort(key=lambda s: s.get("priority", 99))
        head = eligible[0]
        key = str(head.get("parallelGroup") or "").strip()
        if not key:
            return [head], "mode=manual — no parallelGroup on the head story, running it alone"
        members = [s for s in eligible
                   if str(s.get("parallelGroup") or "").strip() == key][: self.max_parallel]
        seen: set = set()
        clashes = []
        for s in members:
            f = story_files(s)
            if f & seen:
                clashes.append(str(s.get("id", "?")))
            seen |= f
        reason = f"mode=manual group={key!r} batch={len(members)}"
        if clashes:
            reason += f" ⚠ OVERLAPPING_FILES={clashes}"
        return members, reason

    def _unrunnable_reason(self, pending: List[Dict[str, Any]], passed_ids: set) -> str:
        """Explain why nothing is runnable."""
        known = {s.get("id") for s in self._load_prd().get("userStories", [])}
        bad = {}
        for s in pending:
            missing = {d for d in story_deps(s) if d not in passed_ids}
            unknown_dep = {d for d in missing if d not in known}
            if unknown_dep:
                bad[str(s.get("id", "?"))] = sorted(unknown_dep)
        if bad:
            return (f"mode={self.parallel_mode} NOTHING RUNNABLE — dependsOn names non-existent "
                    f"story(ies): {bad}")
        waiting = {str(s.get("id", "?")): sorted(story_deps(s) - passed_ids) for s in pending}
        return (f"mode={self.parallel_mode} NOTHING RUNNABLE — every pending story is waiting on "
                f"an unpassed dependency: {waiting}")

    def _dead_stories(
        self, stories: List[Dict[str, Any]], pending: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """Map every story id that can NEVER run → the reason, propagated to a fixpoint."""
        all_ids = {str(s.get("id", "?")) for s in stories}
        reasons: Dict[str, str] = {
            sid: f"benched after {self.attempts.get(sid, 0)} failed attempt(s)"
            for sid in self.benched
        }
        changed = True
        while changed:
            changed = False
            for s in pending:
                sid = str(s.get("id", "?"))
                if sid in reasons:
                    continue
                deps = {str(d) for d in story_deps(s)}
                ghosts = sorted(d for d in deps if d not in all_ids)
                if ghosts:
                    reasons[sid] = f"dependsOn names non-existent story(ies) {ghosts}"
                    changed = True
                    continue
                dead_deps = sorted(d for d in deps if d in reasons)
                if dead_deps:
                    reasons[sid] = f"waiting on unreachable {dead_deps}"
                    changed = True
        return reasons

    def _record_attempts(self, batch: List[Dict[str, Any]]) -> None:
        """Charge one attempt to every dispatched story still not passes=True; bench at the cap."""
        if self.max_story_attempts <= 0:
            return
        try:
            prd = self._load_prd()
        except Exception as exc:
            log(self.progress_path, f"attempt bookkeeping skipped — prd.json unreadable: {exc!r}")
            return
        state = {str(s.get("id", "?")): bool(s.get("passes", False))
                 for s in prd.get("userStories", [])}
        for s in batch:
            sid = str(s.get("id", "?"))
            if state.get(sid, False):
                continue
            n = self.attempts.get(sid, 0) + 1
            self.attempts[sid] = n
            if n >= self.max_story_attempts and sid not in self.benched:
                self.benched.add(sid)
                log(self.progress_path,
                    f"BENCHED {sid} after {n} failed attempt(s) (cap="
                    f"{self.max_story_attempts}) — skipping it so remaining stories can finish")
            else:
                log(self.progress_path, f"attempt {n}/{self.max_story_attempts} failed for {sid}")

    def _exhausted_report(self, pending: List[Dict[str, Any]], dead: Dict[str, str]) -> str:
        """One-line account of why the run has nothing left to do."""
        parts = [f"NOTHING LEFT TO RUN — {len(dead)} of {len(pending)} pending story(ies) are "
                 f"unreachable"]
        for sid in sorted(dead):
            parts.append(f"{sid}: {dead[sid]}")
        return " | ".join(parts)

    # ── Story execution ───────────────────────────────────────────────────────

    def _run_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute a batch of stories via the adapter.

        Returns {"status": "OK", "results": [...]} on success.
        With no fan-out, batch is executed sequentially via repeated adapter.run_story() calls.
        """
        if not batch:
            return {"status": "OK", "results": []}

        results = []
        for story in batch:
            try:
                result = self.adapter.run_story(story, {
                    "prd_path": str(self.prd_path),
                    "progress_path": str(self.progress_path),
                    "project_root": self.project_root,
                })
                results.append(result)
            except Exception as exc:
                log(self.progress_path, f"adapter.run_story raised {exc!r}")
                return {"status": "DELEGATION_FAILED", "results": []}

        # Aggregate cost
        for r in results:
            tok = r.get("tokens", {}) or {}
            self.cost_so_far_usd += (int(tok.get("input", 0)) / 1000.0) * _WORKER_IN_USD_PER_1K
            self.cost_so_far_usd += (int(tok.get("output", 0)) / 1000.0) * _WORKER_OUT_USD_PER_1K

        log(self.progress_path, f"got {len(results)} results cost=${self.cost_so_far_usd:.4f}")
        return {"status": "OK", "results": results}

    # ── Outer loop ────────────────────────────────────────────────────────────

    def _outer_loop(self) -> str:
        """Run the outer iteration loop.

        v1.0: this is the inner part of run(), exposed for backward compat with tests.
        Does NOT reload prd.json at entry (caller does that).
        Returns status string.
        """
        try:
            for round_idx in range(self.max_iter):
                prd = self._load_prd()
                stories = prd.get("userStories", [])

                if all_pass(prd):
                    return "ALL_PASSES"

                pending = get_pending_stories(prd)
                if not pending:
                    return "ALL_PASSES"

                passed_ids = get_passed_ids(prd)

                dead = self._dead_stories(stories, pending)
                runnable = [s for s in pending if str(s.get("id", "?")) not in dead]
                if not runnable:
                    log(self.progress_path,
                        f"round {round_idx + 1}: STORIES_EXHAUSTED — "
                        f"{self._exhausted_report(pending, dead)}")
                    return "STORIES_EXHAUSTED"

                batch, _ = self._plan_batch(runnable, passed_ids)
                if not batch:
                    reason = (f"round {round_idx + 1}: mode={self.parallel_mode} "
                              "NOTHING RUNNABLE — dependency cycle or live deadlock")
                    log(self.progress_path, reason)
                    return "NO_PROGRESS"

                result = self._run_batch(batch)
                for r in (result.get("results") or []):
                    if detect_promise(r.get("summary", "") or ""):
                        prd_chk = self._load_prd()
                        if all_pass(prd_chk):
                            return "ALL_PASSES"

                self._record_attempts(batch)

                if self.cost_so_far_usd >= self.cost_cap_usd:
                    return "COST_CAP"

            return "MAX_ITERATIONS"

        except Exception as exc:
            log(self.progress_path, f"UNCAUGHT {exc!r}\n{traceback.format_exc()}")
            return "INTERNAL_ERROR"

    def run(self) -> str:
        """Run the Ralph goal-loop until completion or a terminal status is reached."""
        try:
            log(self.progress_path,
                f"=== start prd={self.prd_path} max_iter={self.max_iter} "
                f"cost_cap=${self.cost_cap_usd}")

            prd = self._load_prd()
            prd = normalize_prd(prd)
            self._save_prd(prd)

            for round_idx in range(self.max_iter):
                prd = self._load_prd()
                stories = prd.get("userStories", [])

                if all_pass(prd):
                    log(self.progress_path, f"round {round_idx + 1}: ALL_PASSES")
                    return "ALL_PASSES"

                pending = get_pending_stories(prd)
                if not pending:
                    log(self.progress_path, f"round {round_idx + 1}: ALL_PASSES")
                    return "ALL_PASSES"

                passed_ids = get_passed_ids(prd)

                dead = self._dead_stories(stories, pending)
                runnable = [s for s in pending if str(s.get("id", "?")) not in dead]
                if not runnable:
                    log(self.progress_path,
                        f"round {round_idx + 1}: STORIES_EXHAUSTED — "
                        f"{self._exhausted_report(pending, dead)}")
                    return "STORIES_EXHAUSTED"
                if dead:
                    log(self.progress_path,
                        f"round {round_idx + 1}: skipping {sorted(dead)} "
                        f"({len(runnable)} story(ies) still runnable)")

                batch, reason = self._plan_batch(runnable, passed_ids)
                if not batch:
                    log(self.progress_path, f"round {round_idx + 1}: {reason}")
                    return "NO_PROGRESS"

                log(self.progress_path,
                    f"round {round_idx + 1}: {reason} batch_size={len(batch)} "
                    f"stories={[s.get('id', '?') for s in batch]}")

                for story in batch:
                    try:
                        result = self.adapter.run_story(story, {
                            "prd_path": str(self.prd_path),
                            "progress_path": str(self.progress_path),
                            "project_root": self.project_root,
                        })
                        merge_codebase_patterns(self.progress_path, {"results": [result]})
                    except Exception as exc:
                        log(self.progress_path, f"adapter.run_story raised {exc!r}")
                        return "DELEGATION_FAILED"

                    # Check for <promise> token
                    summary = result.get("summary", "") or ""
                    if detect_promise(summary):
                        # Double-check all stories are actually done
                        prd_check = self._load_prd()
                        if all_pass(prd_check):
                            log(self.progress_path, f"round {round_idx + 1}: ALL_PASSES (via <promise>)")
                            return "ALL_PASSES"

                self._record_attempts(batch)

                if self.cost_so_far_usd >= self.cost_cap_usd:
                    log(self.progress_path,
                        f"COST_CAP ${self.cost_so_far_usd:.4f} >= ${self.cost_cap_usd:.4f}")
                    return "COST_CAP"

            return "MAX_ITERATIONS"

        except Exception as exc:
            log(self.progress_path, f"UNCAUGHT {exc!r}\n{traceback.format_exc()}")
            return "INTERNAL_ERROR"


_STATUS_TO_EXIT = {
    "ALL_PASSES": 0,
    "MAX_ITERATIONS": 2,
    "COST_CAP": 3,
    "DELEGATION_FAILED": 6,
    "RUN_PAUSED": 7,
    "INTERNAL_ERROR": 8,
    "NO_PROGRESS": 10,
    "STORIES_EXHAUSTED": 11,
}
