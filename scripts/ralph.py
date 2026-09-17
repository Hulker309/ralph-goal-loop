#!/usr/bin/env python3
"""
ralph-goal-loop orchestrator (Phase 2 main script).

Wraps Hermes /goal (judge + state persistence + cost cap) as the execution engine
and `delegate_task` batch mode for per-priority parallel worker fan-out. Pure in-process;
0 external CLI, 0 modify hermes-agent core.

CLI:
    python scripts/ralph.py --prd <path> --max-iter 10 --cost-cap 5.0 [--no-draft]
        [--judge-provider X --judge-model Y --judge-base-url Z --judge-api-key-env ENV_VAR]
        [--judge-keep-aux-config]

Exit codes:
    0  ALL_PASSES / GOAL_DONE       2  MAX_ITERATIONS      3  COST_CAP
    4  USER_REJECTED_CONTRACT / GOAL_BLOCKED
    5  USER_REJECTED_PRD           6  DELEGATION_FAILED   7  GOAL_CLEARED
    8  INTERNAL_ERROR              9  NO_GOAL

Judge routing: by default the orchestrator injects the parent agent's provider/model/base_url
into ``hermes_cli.goals._call_goal_judge_llm`` for the duration of ``evaluate_after_turn`` so the
judge goes to the SAME endpoint as the worker (no more OpenRouter 404 from a stale
``auxiliary.goal_judge.*`` config). Pass ``--judge-keep-aux-config`` to use the existing config
verbatim, or override individual fields with ``--judge-provider`` / ``--judge-model`` /
``--judge-base-url`` / ``--judge-api-key-env``.

REVIEW nodes emit a `REVIEW_NEEDED:` marker into progress.txt; the caller is expected
to call `goal_manager.resume()` after the boss reviews. v0.1.0 auto-resumes to keep
the script runnable end-to-end; interactive Hermes sessions can intercept the marker.

Stdlib + hermes imports only, no subprocess, <=400 LOC.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# Lazy, narrow hermes imports — avoid loading whole hermes_agent package.
try:
    from hermes_cli.goals import GoalContract, GoalManager, draft_contract
    _HERMES_OK, _HERMES_ERR = True, None
except Exception as exc:  # pragma: no cover
    _HERMES_OK, _HERMES_ERR = False, repr(exc)
try:
    from tools.delegate_tool import delegate_task
    _DELEG_OK, _DELEG_ERR = True, None
except Exception as exc:  # pragma: no cover
    _DELEG_OK, _DELEG_ERR = False, repr(exc)

logger = logging.getLogger("ralph-goal-loop")

PROMISE_TOKEN = "<promise>COMPLETE</promise>"
REVIEW_NEEDED = "REVIEW_NEEDED:"
DEFAULT_MAX_ITER = 10
DEFAULT_COST_CAP_USD = 5.0
# Rough $/1k tokens for main conversation model — used to estimate worker cost.
_WORKER_IN_USD_PER_1K = 0.003
_WORKER_OUT_USD_PER_1K = 0.015


def _resolve_judge_overrides(parent_agent: Any, *,
                              judge_provider: Optional[str] = None,
                              judge_model: Optional[str] = None,
                              judge_base_url: Optional[str] = None,
                              judge_api_key_env: Optional[str] = None,
                              keep_aux_config: bool = False) -> Dict[str, Any]:
    """Compute the judge call overrides from CLI flags + parent_agent.

    Returns an empty dict when ``keep_aux_config`` is set (use existing auxiliary.goal_judge config
    verbatim). Otherwise, fall back to parent_agent.provider/model/base_url/api_key so the judge
    routes to the same backend as the worker. Explicit CLI flags always win over inheritance.
    """
    if keep_aux_config:
        return {}
    overrides: Dict[str, Any] = {}
    # 1) Inherit from parent_agent (worker provider)
    if parent_agent is not None:
        for attr, key in (("provider", "provider"), ("model", "model"),
                           ("base_url", "base_url")):
            val = getattr(parent_agent, attr, None)
            if val:
                overrides.setdefault(key, val)
        # api_key on AIAgent is the secret — only use it if the user didn't pass --judge-api-key-env.
        api_key = getattr(parent_agent, "api_key", None)
        if isinstance(api_key, str) and api_key:
            overrides.setdefault("api_key", api_key)
    # 2) CLI flags override inheritance
    if judge_provider:
        overrides["provider"] = judge_provider
    if judge_model:
        overrides["model"] = judge_model
    if judge_base_url:
        overrides["base_url"] = judge_base_url
    if judge_api_key_env:
        env_val = os.environ.get(judge_api_key_env, "").strip()
        if env_val:
            overrides["api_key"] = env_val
        else:
            _LOG_FALLBACK(f"--judge-api-key-env={judge_api_key_env} but env var is empty")
    return overrides


@contextlib.contextmanager
def _judge_call_overrides_ctx(overrides: Dict[str, Any]) -> Iterator[None]:
    """Inject judge provider/model/base_url/api_key into the single chokepoint the GoalManager
    goes through to call the judge (``hermes_cli.goals._call_goal_judge_llm``).

    The chokepoint already calls ``auxiliary_client.call_llm(task="goal_judge", ...)``; we wrap it
    to also pass the explicit ``provider``/``model``/``base_url``/``api_key`` kwargs. This bypasses
    ``auxiliary.goal_judge.*`` config lookup in ``_resolve_task_provider_model`` (#35566) without
    touching hermes-agent.
    """
    if not overrides:
        yield
        return
    try:
        import hermes_cli.goals as _goals
    except Exception as exc:
        logger.warning("judge override skipped: hermes_cli.goals import failed: %s", exc)
        yield
        return
    _orig = _goals._call_goal_judge_llm

    def _wrapped(call_llm, system_prompt: str, user_prompt: str, timeout: Optional[float]) -> str:
        # Reach into auxiliary_client.call_llm — the same target _call_goal_judge_llm uses — and
        # apply overrides at the per-call boundary. Mirrors the goal_judge route_info contract.
        try:
            from agent.auxiliary_client import call_llm as _aux_call_llm
        except Exception as exc:
            logger.warning("judge override: auxiliary_client.call_llm import failed: %s", exc)
            return _orig(call_llm, system_prompt, user_prompt, timeout)
        # Resolve kwargs we want to override
        kwargs: Dict[str, Any] = {
            "task": "goal_judge",
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0,
            "timeout": timeout,
        }
        # Try to read the configured max_tokens so the override doesn't shrink it.
        try:
            max_tokens = _goals._goal_judge_max_tokens()
        except Exception:
            max_tokens = 4096
        kwargs["max_tokens"] = max_tokens
        for k in ("provider", "model", "base_url", "api_key"):
            if k in overrides:
                kwargs[k] = overrides[k]
        try:
            resp = _aux_call_llm(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            # Surface the transport failure as an empty reply so judge_goal returns transport_failed
            # → evaluate_after_turn pauses with status=paused → our _evaluate routes to GOAL_BLOCKED.
            logger.info("judge override: call_llm raised %s — falling through", exc)
            return ""

    _goals._call_goal_judge_llm = _wrapped
    try:
        yield
    finally:
        _goals._call_goal_judge_llm = _orig


# module-level log sink used by helpers above (avoids passing progress_path everywhere)
def _LOG_FALLBACK(msg: str) -> None:
    logger.info(msg)


def _read_prd(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _log(progress_path: Path, msg: str) -> None:
    """progress.txt is source of truth (NOT stdout)."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(progress_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] ralph-orchestrator: {msg}\n")
    except Exception as exc:
        logger.warning("progress.txt append failed: %s", exc)
    logger.info(msg)


def _render_worker_prompt(prd_path: Path, progress_path: Path,
                           story: Dict[str, Any]) -> str:
    sid, stitle, sprio = story.get("id", "?"), story.get("title", ""), story.get("priority", 1)
    ac_block = "\n".join(f"- {a}" for a in (story.get("acceptanceCriteria") or []))
    return (
        f"# Story {sid} — {stitle}\n\n"
        f"**Priority**: {sprio}\n\n"
        f"## Acceptance criteria\n{ac_block}\n\n"
        f"## Files\n- prd: {prd_path}\n- progress: {progress_path}\n\n"
        f"## Workflow\n"
        f"1. Read {prd_path}, confirm story {sid} is yours\n"
        f"2. Read {progress_path} `## Codebase Patterns` section\n"
        f"3. Implement story {sid}; verify each acceptance criterion command runs OK\n"
        f"4. Set `passes: true` for story {sid} ONLY in {prd_path}\n"
        f"5. Append a `## [{sid}]` block to {progress_path}\n"
        f"6. If ALL stories in {prd_path} have `passes: true`, end your response "
        f"with the literal token:\n\n{PROMISE_TOKEN}\n")


class RalphGoalLoop:
    """Phase 2 orchestrator. Public API: `run()` returning a status string."""

    parent_agent: Any = None  # shared across instances (AIAgent is heavy)
    parent_agent_err: Optional[str] = None

    def __init__(self, prd_path: str, max_iter: int = DEFAULT_MAX_ITER,
                 cost_cap_usd: float = DEFAULT_COST_CAP_USD,
                 session_id: Optional[str] = None,
                 auto_draft: bool = True,
                 judge_overrides: Optional[Dict[str, Any]] = None) -> None:
        self.prd_path = Path(prd_path).resolve()
        self.progress_path = self.prd_path.parent / "progress.txt"
        self.max_iter, self.cost_cap_usd = max_iter, cost_cap_usd
        self.session_id = session_id or f"ralph-{int(time.time())}"
        self.auto_draft = auto_draft
        self.cost_so_far_usd: float = 0.0
        self.goal_manager: Optional[GoalManager] = None
        self.contract: Optional[GoalContract] = None
        self.judge_overrides: Dict[str, Any] = dict(judge_overrides or {})
        self._init_parent_agent()

    def _init_parent_agent(self) -> None:
        if not _DELEG_OK or RalphGoalLoop.parent_agent is not None:
            return
        try:
            from run_agent import AIAgent
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider

            # Fix v0.2 bug: AIAgent() with empty kwargs loses model.provider/model
            # (custom_providers.{base_url,api_key} still load, but model.* is dropped).
            # Fix v0.3 bug: api_mode is required — without it child posts to /chat/completions
            # and MiniMax /anthropic endpoint 404s. Resolve via the SAME ladder hermes chat
            # uses (ladder 2-8 in hermes_cli.runtime_provider.resolve_runtime_provider) so
            # child inherits the proven-working runtime that lets the main conversation
            # work end-to-end (no more guessing from base_url suffix).
            model_cfg = (load_config().get("model") or {})
            worker_provider = (model_cfg.get("provider") or "minimax-cn").strip() or "minimax-cn"
            worker_model = (model_cfg.get("default") or "MiniMax-M3").strip() or "MiniMax-M3"

            rt = resolve_runtime_provider(
                requested=worker_provider,
                target_model=worker_model,
            )
            RalphGoalLoop.parent_agent = AIAgent(
                session_id=self.session_id,
                quiet_mode=True,
                model=rt.get("model") or worker_model,
                provider=rt.get("provider") or worker_provider,
                base_url=rt.get("base_url") or None,
                api_key=rt.get("api_key") or None,
                api_mode=rt.get("api_mode") or None,
            )
        except Exception as exc:
            RalphGoalLoop.parent_agent_err = repr(exc)

    # ── Steps 1-3: contract + GoalManager.set ───────────────────────────────
    def _step_draft_and_set(self, goal_text: str) -> Optional[GoalContract]:
        contract = None
        if _HERMES_OK and self.auto_draft:
            try:
                contract = draft_contract(goal_text)
            except Exception as exc:
                _log(self.progress_path, f"draft_contract exception {exc!r}")
        if not _HERMES_OK:
            _log(self.progress_path, f"hermes_cli.goals import FAILED: {_HERMES_ERR}")
            return contract
        try:
            self.goal_manager = GoalManager(self.session_id)
            self.goal_manager.set(goal_text, contract=contract)
            _log(self.progress_path, f"GoalManager.set OK session={self.session_id}")
        except Exception as exc:
            _log(self.progress_path, f"GoalManager.set exception {exc!r}")
            self.goal_manager = None
        return contract

    # ── Step 4: normalize prd.json ──────────────────────────────────────────
    def _step_normalize_prd(self) -> Dict[str, Any]:
        try:
            prd = _read_prd(self.prd_path)
        except Exception as exc:
            _log(self.progress_path, f"read prd.json FAILED: {exc!r}")
            return {"userStories": []}
        for idx, s in enumerate(prd.get("userStories", []), start=1):
            s.setdefault("priority", idx)
            s.setdefault("id", f"US-{idx}")
            s["passes"] = bool(s.get("passes", False))
        try:
            self.prd_path.write_text(json.dumps(prd, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
        except Exception as exc:
            _log(self.progress_path, f"write prd.json FAILED: {exc!r}")
        return prd

    # ── Step 5: review gate (auto-resume v0.1.0; pause hook for interactive) ─
    def _step_review_prd(self, prd: Dict[str, Any]) -> bool:
        n = len(prd.get("userStories", []))
        _log(self.progress_path, f"{REVIEW_NEEDED} PRD — {n} stories in {self.prd_path}. "
                                  "Boss: cat prd.json | jq '.userStories[].id' to review; "
                                  "then `goal_manager.resume()` to continue or `.clear()` to abort.")
        if self.goal_manager is not None:
            try:
                self.goal_manager.pause("boss-review-prd")
                self.goal_manager.resume(reset_budget=False)  # v0.1.0: auto-resume
            except Exception as exc:
                _log(self.progress_path, f"pause/resume exception {exc!r}")
        return True

    # ── Step 6: outer loop ──────────────────────────────────────────────────
    def _outer_loop(self) -> str:
        for round_idx in range(self.max_iter):
            prd = _read_prd(self.prd_path)
            pending = [s for s in prd.get("userStories", []) if not s.get("passes", False)]
            if not pending:
                _log(self.progress_path, f"round {round_idx + 1}: ALL_PASSES")
                return "ALL_PASSES"
            if self.goal_manager is not None and not self.goal_manager.is_active():
                _log(self.progress_path, f"round {round_idx + 1}: goal_manager inactive → GOAL_CLEARED")
                return "GOAL_CLEARED"
            top = min(s.get("priority", 99) for s in pending)
            batch = [s for s in pending if s.get("priority", 99) == top]
            _log(self.progress_path,
                 f"round {round_idx + 1}: priority={top} batch_size={len(batch)} "
                 f"stories={[s.get('id', '?') for s in batch]}")
            br = self._run_batch(batch)
            if br.get("status") != "OK":
                return "DELEGATION_FAILED"
            ev = self._evaluate(br)
            if ev == "GOAL_DONE":
                return "GOAL_DONE"
            if ev == "GOAL_BLOCKED":
                # Don't keep spinning when the judge (or transport layer) said the goal is unreachable.
                # Short-circuit so the boss sees exit=4 immediately and can re-scope / fix config.
                return "GOAL_BLOCKED"
            if self.cost_so_far_usd >= self.cost_cap_usd:
                _log(self.progress_path,
                     f"COST_CAP ${self.cost_so_far_usd:.4f} >= ${self.cost_cap_usd:.4f}")
                return "COST_CAP"
        return "MAX_ITERATIONS"

    # ── Step 7: delegate_task batch ─────────────────────────────────────────
    def _run_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not batch:
            return {"status": "OK", "results": []}
        if not _DELEG_OK:
            _log(self.progress_path, f"delegate_task import FAILED: {_DELEG_ERR}")
            return {"status": "DELEGATION_FAILED", "results": []}
        if RalphGoalLoop.parent_agent is None:
            _log(self.progress_path, f"parent_agent is None: {RalphGoalLoop.parent_agent_err}")
            return {"status": "DELEGATION_FAILED", "results": []}
        tasks = [{"goal": _render_worker_prompt(self.prd_path, self.progress_path, s),
                  "context": ""} for s in batch]
        try:
            raw = delegate_task(tasks=tasks, parent_agent=RalphGoalLoop.parent_agent,
                                background=False)
            # skip_memory NOT passed (child hard-coded True per delegate_tool.py:240).
            # max_iterations NOT passed (config.yaml is authoritative per delegate_tool.py:462).
            res = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            _log(self.progress_path, f"delegate_task raised {exc!r}")
            return {"status": "DELEGATION_FAILED", "results": []}
        results = (res or {}).get("results", []) if isinstance(res, dict) else []
        for e in results:
            tok = e.get("tokens", {}) or {}
            self.cost_so_far_usd += (int(tok.get("input", 0)) / 1000.0) * _WORKER_IN_USD_PER_1K
            self.cost_so_far_usd += (int(tok.get("output", 0)) / 1000.0) * _WORKER_OUT_USD_PER_1K
        _log(self.progress_path, f"got {len(results)} results cost=${self.cost_so_far_usd:.4f}")
        return {"status": "OK", "results": results}

    # ── Step 8: judge via evaluate_after_turn, double-check via <promise> ──
    # 5-field verdict semantics — see _evaluate's docstring. GoalManager.evaluate_after_turn returns
    # a dict with keys (status, should_continue, continuation_prompt, verdict, reason, message) and we
    # infer transport/parse failures from status=="paused" plus a heuristics check on the message
    # (the underlying tuple isn't exposed — see #ralph-2). The fallback below is fail-OPEN → CONTINUE
    # (matches goals.py:1518 default) so a misbehaving judge doesn't fake GOAL_DONE.
    def _evaluate(self, batch_result: Dict[str, Any]) -> str:
        summaries = [r.get("summary", "") for r in batch_result.get("results", [])]
        any_promise = any(PROMISE_TOKEN in (s or "") for s in summaries)
        if self.goal_manager is None:
            # stub mode: <promise> token is the only signal
            return "GOAL_DONE" if any_promise else "CONTINUE"
        last_response = "\n\n---\n\n".join(summaries) or "(empty)"
        try:
            with _judge_call_overrides_ctx(self.judge_overrides):
                decision = self.goal_manager.evaluate_after_turn(last_response)
        except Exception as exc:
            _log(self.progress_path, f"evaluate_after_turn exception {exc!r} → fail-OPEN CONTINUE")
            return "GOAL_DONE" if any_promise else "CONTINUE"

        verdict = str(decision.get("verdict", "") or "").strip().lower() or "continue"
        status = str(decision.get("status", "") or "").strip().lower()
        reason = str(decision.get("reason", "") or "")
        message = str(decision.get("message", "") or "")
        should_continue = bool(decision.get("should_continue", False))

        # Transport / persistent-API-error path. GoalManager exposes these as status=paused with a
        # message hinting at the goal_judge config (#35566 / #100954). We translate to a NEW status
        # GOAL_BLOCKED so the boss sees a distinct signal (NOT done) and exit code stays non-zero.
        is_transport = (
            "judge API unreachable" in message
            or "judge error" in message.lower()
            or "goal_judge provider" in message
            or verdict == "continue" and "judge" in message.lower() and "error" in message.lower()
        )
        if is_transport:
            _log(self.progress_path,
                 f"JUDGE_TRANSPORT_FAILED: {message[:200]!r} — boss must fix judge provider config")
            return "GOAL_BLOCKED"

        # 5-field decision tree — see SKILL.md §<promise>COMPLETE</promise> Protocol.
        if verdict == "blocked":
            _log(self.progress_path, f"GOAL_BLOCKED: judge ruled unachievable: {reason[:200]!r}")
            return "GOAL_BLOCKED"
        if verdict == "done" and not should_continue:
            _log(self.progress_path,
                 f"verdict=done reason={reason[:120]!r} status={status} (judge done)")
            return "GOAL_DONE"
        if verdict == "wait":
            # /goal wait logic in GoalManager already parked the loop via _waiting_decision;
            # orchestrator just keeps its outer loop alive (next round will read parked state).
            _log(self.progress_path,
                 f"verdict=wait reason={reason[:120]!r} status={status} (loop parks in GoalManager)")
            return "CONTINUE"

        # Double-insurance: <promise> token in any worker summary short-circuits to GOAL_DONE,
        # because the worker explicitly declared completion — trust it over the judge for this signal.
        # (This is the original v0.1.0 behavior, kept to handle judges that lag the worker by a round.)
        if any_promise:
            _log(self.progress_path,
                 f"verdict={verdict} but <promise>COMPLETE</promise> in summary → GOAL_DONE")
            return "GOAL_DONE"
        return "CONTINUE"

    # ── Main entry ──────────────────────────────────────────────────────────
    def run(self) -> str:
        try:
            _log(self.progress_path, f"=== start prd={self.prd_path} max_iter={self.max_iter} "
                                      f"cost_cap=${self.cost_cap_usd} session={self.session_id}")
            try:
                prd = _read_prd(self.prd_path)
            except Exception as exc:
                _log(self.progress_path, f"read prd.json FAILED: {exc!r}")
                return "INTERNAL_ERROR"
            goal_text = (f"实现 prd '{prd.get('title', '?')}' 的所有 userStories "
                         f"(共 {len(prd.get('userStories', []))} 个),全部 passes=true. "
                         f"Description: {prd.get('description', '')}")
            self.contract = self._step_draft_and_set(goal_text)
            self._step_normalize_prd()
            prd2 = _read_prd(self.prd_path)
            self._step_review_prd(prd2)
            status = self._outer_loop()
            _log(self.progress_path, f"=== end status={status} cost=${self.cost_so_far_usd:.4f}")
            return status
        except Exception as exc:
            _log(self.progress_path, f"UNCAUGHT {exc!r}\n{traceback.format_exc()}")
            return "INTERNAL_ERROR"


_STATUS_TO_EXIT = {
    "ALL_PASSES": 0, "GOAL_DONE": 0,
    "MAX_ITERATIONS": 2, "COST_CAP": 3,
    "USER_REJECTED_CONTRACT": 4, "GOAL_BLOCKED": 4,
    "USER_REJECTED_PRD": 5,
    "DELEGATION_FAILED": 6, "GOAL_CLEARED": 7,
    "INTERNAL_ERROR": 8, "NO_GOAL": 9,
}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="ralph",
                                  description="ralph-goal-loop orchestrator (Phase 2)")
    p.add_argument("--prd", required=True, help="Path to prd.json")
    p.add_argument("--max-iter", type=int, default=DEFAULT_MAX_ITER)
    p.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP_USD)
    p.add_argument("--session-id", default=None)
    p.add_argument("--no-draft", action="store_true",
                   help="Skip draft_contract() (boss already wrote prd.json)")
    # Judge routing — inherit from parent_agent (worker provider) by default.
    p.add_argument("--judge-provider", default=None,
                   help="Judge LLM provider (default: inherit from parent agent)")
    p.add_argument("--judge-model", default=None,
                   help="Judge LLM model (default: inherit from parent agent)")
    p.add_argument("--judge-base-url", default=None,
                   help="Judge LLM base_url (default: inherit from parent agent)")
    p.add_argument("--judge-api-key-env", default=None,
                   help="Env var name holding the judge API key (default: inherit from parent agent)")
    p.add_argument("--judge-keep-aux-config", action="store_true",
                   help="Don't override auxiliary.goal_judge — use existing config as-is")
    args = p.parse_args(argv)
    if not Path(args.prd).exists():
        print(f"FATAL: prd not found: {args.prd}", file=sys.stderr)
        return 8
    # Build the orchestrator WITHOUT judge_overrides first so parent_agent is initialized, then
    # derive overrides from it. This keeps parent_agent init order stable across CLI invocations.
    orch = RalphGoalLoop(args.prd, args.max_iter, args.cost_cap,
                         args.session_id, auto_draft=not args.no_draft)
    orch.judge_overrides = _resolve_judge_overrides(
        RalphGoalLoop.parent_agent,
        judge_provider=args.judge_provider,
        judge_model=args.judge_model,
        judge_base_url=args.judge_base_url,
        judge_api_key_env=args.judge_api_key_env,
        keep_aux_config=args.judge_keep_aux_config,
    )
    if orch.judge_overrides:
        _log(orch.progress_path, f"judge_overrides={ {k: (v[:6] + '…' if isinstance(v, str) and len(v) > 12 else v) for k, v in orch.judge_overrides.items()} }")
    else:
        _log(orch.progress_path, "judge_overrides: none (using auxiliary.goal_judge config verbatim)")
    status = orch.run()
    print(status)
    return _STATUS_TO_EXIT.get(status, 8)


if __name__ == "__main__":
    sys.exit(main())
