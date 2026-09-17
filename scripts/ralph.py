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
    8  INTERNAL_ERROR              9  NO_GOAL            10  NO_PROGRESS
   11  STORIES_EXHAUSTED (remaining stories are unreachable: benched, or dependsOn is dead)

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
import re
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

# ── Recon: parallel codebase grounding (the missing "head" of the loop) ──────
# Upstream Ralph grounds interfaces IMPLICITLY: the iteration agent sits inside the project
# directory and reads real code while it works, so it never has to guess a signature. When
# execution is delegated to a fan-out of workers, that implicit grounding vanishes — a worker
# receives only the story text and guesses the interfaces, and the guess surfaces much later
# as compile errors plus orchestrator rework.
#
# Recon restores grounding EXPLICITLY: before execution, N recon workers are fanned out to read
# the real codebase and rewrite each story's acceptance criteria into verifiable form (real
# signatures + runnable commands). Recon workers are read-only and never write prd.json — the
# orchestrator merges their findings per batch, so parallel workers cannot race on shared state.
_RECON_GROUP_DEFAULT = 3          # stories handed to one recon worker
_RECON_BLOCK_RE = re.compile(r"<recon>(.*?)</recon>", re.DOTALL)

# ── Parallel batch planning ──────────────────────────────────────────────────
# Upstream Ralph is strictly serial (one story per iteration) and gets its concurrency from the
# agent inside that single iteration. Here execution is a fan-out, so parallelism has to be
# DECIDED — and the caller (the agent driving this skill) decides it, via --parallel-mode:
#
#   off       one story per round. Strict serial; upstream fidelity.
#   priority  group by equal `priority` value (the original behaviour). NOTE: upstream's `ralph`
#             skill numbers stories 1,2,3… so every story gets its own priority, which makes this
#             mode serial in practice. Kept for callers that group deliberately.
#   auto      dependency-aware + file-overlap-aware grouping ACROSS priorities (default). A story
#             is eligible once its `dependsOn` are all `passes: true`; two stories may share a
#             batch only when their file sets are known and disjoint. Unknown file set ⇒ the story
#             runs alone, because an unknown conflict surface is not evidence of no conflict.
#   manual    respect explicit `parallelGroup` labels written into prd.json, so the caller can
#             choose the exact grouping (max freedom, no inference).
_PARALLEL_MODES = ("off", "priority", "auto", "manual")
_PARALLEL_MODE_DEFAULT = "auto"
_MAX_PARALLEL_DEFAULT = 10        # matches delegate_task's DaemonThreadPoolExecutor budget

# ── Failure handling / starvation guard ──────────────────────────────────────
# A story that is dispatched and still comes back `passes: false` has spent a full attempt. Left
# unbounded, one unsatisfiable story owns the whole run: it stays pending forever, so in the old
# `min(priority)` grouping it also blocked every story behind it until max_iterations burned out.
# Correct behaviour is to BENCH it after N failed attempts and keep the run productive on the
# stories that can still finish, then report what was benched and why.
#
# Priority is an ORDERING preference, never a gate: `dependsOn` decides what may run, priority
# only decides what to reach for first. 0 disables benching (retry forever, the old behaviour).
_MAX_STORY_ATTEMPTS_DEFAULT = 3


def _normalize_path(p: str) -> str:
    return p.strip().replace("\\", "/").lstrip("./").lower()


def _story_files(story: Dict[str, Any]) -> set:
    """File paths a story is expected to touch.

    Prefers the explicit `files` field on the story, falling back to what recon read off the real
    codebase. Empty set means "conflict surface unknown" — callers must treat that as a blocker,
    never as "no conflicts".
    """
    out = set()
    for p in (story.get("files") or []):
        if isinstance(p, str) and p.strip():
            out.add(_normalize_path(p))
        elif isinstance(p, dict) and (p.get("path") or "").strip():
            out.add(_normalize_path(str(p["path"])))
    for f in ((story.get("recon") or {}).get("files") or []):
        p = (f or {}).get("path") if isinstance(f, dict) else f
        if isinstance(p, str) and p.strip():
            out.add(_normalize_path(p))
    return out


def _story_deps(story: Dict[str, Any]) -> set:
    """Story ids this one waits on. Accepts `dependsOn` (camel) or `depends_on` (snake)."""
    raw = story.get("dependsOn")
    if raw is None:
        raw = story.get("depends_on")
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set()


def _render_recon_prompt(project_root: str, stories: List[Dict[str, Any]]) -> str:
    """Build the prompt for one recon worker (grounds a group of stories, writes nothing)."""
    blocks = []
    for s in stories:
        ac = "\n".join(f"    - {a}" for a in (s.get("acceptanceCriteria") or []))
        blocks.append(
            f"### {s.get('id', '?')} — {s.get('title', '')}\n"
            f"    description: {s.get('description', '')}\n"
            f"    current acceptance criteria (WRITTEN ON PAPER, NEVER CHECKED AGAINST CODE):\n"
            f"{ac}"
        )
    stories_block = "\n\n".join(blocks)
    return (
        "# Recon task — ground these stories in the REAL codebase (do NOT write feature code)\n\n"
        f"Project root: {project_root}\n\n"
        "## Stories to ground\n\n"
        f"{stories_block}\n\n"
        "## Why you are here\n"
        "Those acceptance criteria were written from a requirements document. Nobody checked them\n"
        "against the actual code. If the implementer trusts them and guesses the interfaces, the\n"
        "guess fails at compile time and has to be fixed by hand afterwards. Your job is to find\n"
        "the truth BEFORE anyone writes code.\n\n"
        "## Answer these, per story\n"
        "1. **Which files** will this story create or modify? Give real paths relative to the\n"
        "   project root, and state whether each exists today.\n"
        "2. **Real signatures** of every existing interface this story must call. For each: name,\n"
        "   exact signature, and EVIDENCE as `path:line`. If you cannot find it, write `NOT FOUND`.\n"
        "3. **Existing conventions** this module follows: export style (named export vs instance\n"
        "   method vs default), naming, error handling, the build and test commands. Each with evidence.\n"
        "4. **Traps**: same-name-different-thing, deprecated APIs, implicit types, constraints that\n"
        "   a typechecker will not catch.\n"
        "5. **Rewritten acceptance criteria**: rewrite each story's criteria in VERIFIABLE form —\n"
        "   a command that can be run plus the output that proves it. Cite the real interface names\n"
        "   you found. Do not write aspiration sentences like \"implement X correctly\".\n\n"
        "## Hard rules\n"
        "- READ ONLY. Do not write feature code. Do not modify prd.json or progress.txt.\n"
        "- EVERY claim needs `path:line` evidence. A claim without evidence is a guess — omit it.\n"
        "- A missing interface is written `NOT FOUND`. Inventing a plausible-looking signature is\n"
        "  far more damaging than reporting that it does not exist.\n"
        "- You may be handed a signature that we ourselves got WRONG earlier. **The code is the\n"
        "  authority, not our description of it.** If they disagree, report what the code says.\n\n"
        "## Output format (strict — the orchestrator parses this)\n"
        "End your reply with exactly one JSON block wrapped in <recon> tags:\n\n"
        "<recon>\n"
        '{"stories": [\n'
        '  {"id": "US-008",\n'
        '   "files": [{"path": "tools/spawn-bus-session.ts", "exists": false}],\n'
        '   "interfaces": [{"name": "defineToolPlugin", "signature": "defineToolPlugin<TConfig>(...) -> DefinedToolPluginEntry", "evidence": "plugin-sdk/tool-plugin.d.ts:107"}],\n'
        '   "conventions": [{"fact": "state modules export top-level functions; getters return plain data with no methods", "evidence": "state/role-registry.ts:37,47"}],\n'
        '   "gotchas": [{"fact": "import path is plugin-sdk/tool-plugin, NOT plugin-sdk/core", "evidence": "plugin-sdk/tool-plugin.d.ts:107"}],\n'
        '   "acceptanceCriteria": ["`npx tsc --noEmit` exits 0", "..."]}\n'
        "]}\n"
        "</recon>\n"
    )


def _parse_recon_block(text: str) -> Optional[Dict[str, Any]]:
    """Extract the <recon> JSON block from a recon worker's summary. None if absent/invalid."""
    m = _RECON_BLOCK_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


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


def _render_grounding_block(story_id: str, recon: Dict[str, Any]) -> str:
    """Render a story's recon findings into a prompt block. Empty string when no recon ran."""
    if not recon:
        return ""
    lines = [
        f"## Grounding for {story_id} — read from the real codebase by a recon pass",
        "Trust these: each was read out of the code and carries `path:line` evidence.",
        "If the code disagrees with one, follow the code and report the discrepancy.",
        "",
    ]
    ifs = recon.get("interfaces") or []
    if ifs:
        lines.append("**Interfaces (real signatures)**:")
        for i in ifs:
            lines.append(f"- `{i.get('name', '?')}` — {i.get('signature', '?')}  "
                         f"[{i.get('evidence', 'NO EVIDENCE')}]")
        lines.append("")
    files = recon.get("files") or []
    if files:
        lines.append("**Files this story touches**:")
        for f in files:
            state = "exists" if f.get("exists") else "does NOT exist yet"
            lines.append(f"- `{f.get('path', '?')}` ({state})")
        lines.append("")
    for key, label in (("conventions", "**Conventions**"), ("gotchas", "**Traps**")):
        items = recon.get(key) or []
        if items:
            lines.append(f"{label}:")
            for it in items:
                lines.append(f"- {it.get('fact', '?')}  [{it.get('evidence', 'NO EVIDENCE')}]")
            lines.append("")
    return "\n".join(lines) + "\n"


def _render_worker_prompt(prd_path: Path, progress_path: Path,
                           story: Dict[str, Any],
                           shared_facts: str = "") -> str:
    sid, stitle, sprio = story.get("id", "?"), story.get("title", ""), story.get("priority", 1)
    ac_block = "\n".join(f"- {a}" for a in (story.get("acceptanceCriteria") or []))
    grounding = _render_grounding_block(sid, story.get("recon") or {})
    facts_block = ""
    if shared_facts and shared_facts.strip():
        facts_block = (
            "## Facts already established about this codebase\n"
            "Learned by recon / earlier workers on this same run. Reuse them instead of\n"
            "rediscovering them; if the code contradicts one, follow the code and report it.\n\n"
            f"{shared_facts.strip()}\n\n"
        )
    return (
        f"# Story {sid} — {stitle}\n\n"
        f"**Priority**: {sprio}\n\n"
        f"## Acceptance criteria\n{ac_block}\n\n"
        f"{grounding}"
        f"{facts_block}"
        f"## Files\n- prd: {prd_path}\n- progress: {progress_path}\n\n"
        f"## Workflow\n"
        f"1. Read {prd_path}, confirm story {sid} is yours\n"
        f"2. Read {progress_path} — the `## Grounding` and `## Codebase Patterns` sections first\n"
        f"3. Read the files you are about to change BEFORE writing; follow the patterns already there\n"
        f"4. Implement story {sid}; verify each acceptance criterion by running its command\n"
        f"5. Set `passes: true` for story {sid} ONLY in {prd_path}\n"
        f"6. Append a `## [{sid}]` block to {progress_path} (append-only; never rewrite earlier blocks)\n"
        f"7. If ALL stories in {prd_path} have `passes: true`, end your response "
        f"with the literal token:\n\n{PROMISE_TOKEN}\n\n"
        f"## Quality bar\n"
        f"- Do NOT mark `passes: true` without running the verification and pasting its output\n"
        f"- Do NOT commit broken code\n"
        f"- Follow the existing code patterns in the files you touch\n"
        f"- Write real assertions in tests, never placeholder `assert True`\n\n"
        f"## Report what you learned (the orchestrator merges this into shared state)\n"
        f"Inside your `## [{sid}]` progress block, include these three lines so the next story does\n"
        f"not have to rediscover them. Omit a line only if you genuinely found nothing:\n"
        f"```\n"
        f"- Pattern: <reusable fact about how this codebase does things, with file:line>\n"
        f"- Gotcha: <non-obvious trap you hit, with file:line>\n"
        f"- File: <where X is defined or stored, with path:line>\n"
        f"```\n")


class RalphGoalLoop:
    """Phase 2 orchestrator. Public API: `run()` returning a status string."""

    parent_agent: Any = None  # shared across instances (AIAgent is heavy)
    parent_agent_err: Optional[str] = None

    def __init__(self, prd_path: str, max_iter: int = DEFAULT_MAX_ITER,
                 cost_cap_usd: float = DEFAULT_COST_CAP_USD,
                 session_id: Optional[str] = None,
                 auto_draft: bool = True,
                 judge_overrides: Optional[Dict[str, Any]] = None,
                 recon: bool = True,
                 recon_group: int = _RECON_GROUP_DEFAULT,
                 project_root: Optional[str] = None,
                 worker_provider: Optional[str] = None,
                 worker_model: Optional[str] = None,
                 parallel_mode: str = _PARALLEL_MODE_DEFAULT,
                 max_parallel: int = _MAX_PARALLEL_DEFAULT,
                 max_story_attempts: int = _MAX_STORY_ATTEMPTS_DEFAULT) -> None:
        self.prd_path = Path(prd_path).resolve()
        self.progress_path = self.prd_path.parent / "progress.txt"
        self.max_iter, self.cost_cap_usd = max_iter, cost_cap_usd
        self.session_id = session_id or f"ralph-{int(time.time())}"
        self.auto_draft = auto_draft
        # Recon: ground each story's acceptance criteria against the real codebase before any
        # worker writes code. project_root is the codebase the workers read (usually the repo
        # root, not the directory holding prd.json).
        self.recon = recon
        self.recon_group = max(1, int(recon_group))
        self.project_root = str(Path(project_root).resolve()) if project_root else os.getcwd()
        # Worker model. None → inherit config.yaml's model.provider/model.default (what `hermes chat`
        # uses). Set explicitly to run ralph on a different model than the global default without
        # editing config.yaml — e.g. when the configured provider is rate-limited or down.
        self.worker_provider = worker_provider
        self.worker_model = worker_model
        # How this round's stories are grouped for parallel fan-out. Validated here so a bad value
        # fails loudly at construction rather than silently degrading to serial.
        mode = (parallel_mode or _PARALLEL_MODE_DEFAULT).strip().lower()
        if mode not in _PARALLEL_MODES:
            raise ValueError(f"parallel_mode must be one of {_PARALLEL_MODES}, got {parallel_mode!r}")
        self.parallel_mode = mode
        self.max_parallel = max(1, int(max_parallel))
        # Failure bookkeeping. In-memory is authoritative for scheduling: prd.json is rewritten by
        # workers (they flip their own `passes`), so a counter stored there could be clobbered.
        # Attempt counts are mirrored into progress.txt for humans; a fresh process starts over.
        self.max_story_attempts = int(max_story_attempts)
        self.attempts: Dict[str, int] = {}
        self.benched: set = set()
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

            # Provider/model resolution order: --worker-provider/--worker-model, else config.yaml
            # (the same values `hermes chat` uses). Deliberately NO literal model fallback: baking a
            # vendor name in here would pin the loop to a model the user never chose and to an
            # endpoint that may be down, and would make the skill vendor-specific. When nothing is
            # configured we pass None and let Hermes' own provider ladder decide.
            model_cfg = (load_config().get("model") or {})
            worker_provider = (self.worker_provider or model_cfg.get("provider") or "").strip() or None
            worker_model = (self.worker_model or model_cfg.get("default") or "").strip() or None

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

    # ── Step 5.5: recon — ground the stories against the real codebase ──────
    def _step_recon(self, prd: Dict[str, Any]) -> Dict[str, Any]:
        """Fan out recon workers so each story's criteria come out of the real codebase.

        Parallelism is preserved (N recon workers ride in ONE delegate_task batch). All WRITES
        happen here, after the batch returns, so parallel workers can never race on prd.json.

        Failure is non-fatal on purpose: if recon yields nothing, the loop proceeds exactly as
        before, and progress.txt records that it ran ungrounded.
        """
        if not self.recon:
            _log(self.progress_path, "recon: disabled (--no-recon)")
            return prd
        pending = [s for s in prd.get("userStories", []) if not s.get("passes", False)]
        if not pending:
            return prd
        if not _DELEG_OK or RalphGoalLoop.parent_agent is None:
            _log(self.progress_path, "recon: skipped — delegate unavailable "
                                     f"({_DELEG_ERR or RalphGoalLoop.parent_agent_err})")
            return prd

        groups = [pending[i:i + self.recon_group]
                  for i in range(0, len(pending), self.recon_group)]
        _log(self.progress_path,
             f"recon: {len(pending)} pending stories → {len(groups)} recon workers "
             f"(group={self.recon_group}) root={self.project_root}")
        tasks = [{"goal": _render_recon_prompt(self.project_root, g), "context": ""}
                 for g in groups]
        try:
            raw = delegate_task(tasks=tasks, parent_agent=RalphGoalLoop.parent_agent,
                                background=False)
            res = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            _log(self.progress_path, f"recon delegate_task raised {exc!r} → proceed ungrounded")
            return prd

        results = (res or {}).get("results", []) if isinstance(res, dict) else []
        merged: Dict[str, Any] = {}
        unparsed = 0
        for e in results:
            tok = e.get("tokens", {}) or {}
            self.cost_so_far_usd += (int(tok.get("input", 0)) / 1000.0) * _WORKER_IN_USD_PER_1K
            self.cost_so_far_usd += (int(tok.get("output", 0)) / 1000.0) * _WORKER_OUT_USD_PER_1K
            parsed = _parse_recon_block(e.get("summary", ""))
            if not parsed:
                unparsed += 1
                continue
            for st in (parsed.get("stories") or []):
                if isinstance(st, dict) and st.get("id"):
                    merged[st["id"]] = st
        _log(self.progress_path,
             f"recon: {len(results)} worker(s), unparsed={unparsed}, grounded={len(merged)} story(ies)")
        if not merged:
            _log(self.progress_path, "recon: 0 stories grounded → proceeding ungrounded")
            return prd

        grounded_ac = 0
        for s in prd.get("userStories", []):
            r = merged.get(s.get("id"))
            if not r:
                continue
            s["recon"] = r
            new_ac = [a for a in (r.get("acceptanceCriteria") or [])
                      if isinstance(a, str) and a.strip()]
            if new_ac:
                s["acceptanceCriteria"] = new_ac
                grounded_ac += 1
        try:
            self.prd_path.write_text(json.dumps(prd, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        except Exception as exc:
            _log(self.progress_path, f"recon: write prd.json FAILED {exc!r}")
            return prd
        _log(self.progress_path, f"recon: prd.json grounded — {len(merged)} stories, "
                                 f"{grounded_ac} got rewritten criteria")
        self._write_grounding_section(merged)
        return prd

    def _write_grounding_section(self, merged: Dict[str, Any]) -> None:
        """Append the recon findings to progress.txt — the shared facts later stories reuse."""
        lines = ["", "## Grounding (verified against the real codebase by recon)", ""]
        for sid in sorted(merged):
            r = merged[sid]
            lines.append(f"### {sid}")
            for i in (r.get("interfaces") or []):
                lines.append(f"- Interface `{i.get('name', '?')}` — {i.get('signature', '?')} "
                             f"[{i.get('evidence', 'NO EVIDENCE')}]")
            for f in (r.get("files") or []):
                state = "exists" if f.get("exists") else "does NOT exist yet"
                lines.append(f"- File `{f.get('path', '?')}` ({state})")
            for it in (r.get("conventions") or []):
                lines.append(f"- Pattern: {it.get('fact', '?')} [{it.get('evidence', 'NO EVIDENCE')}]")
            for it in (r.get("gotchas") or []):
                lines.append(f"- Gotcha: {it.get('fact', '?')} [{it.get('evidence', 'NO EVIDENCE')}]")
            lines.append("")
        try:
            with open(self.progress_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception as exc:
            _log(self.progress_path, f"grounding section append failed: {exc!r}")

    def _read_shared_facts(self, limit_chars: int = 6000) -> str:
        """Read back accumulated Grounding + Codebase Patterns so later workers inherit them.

        Both sections are appended to repeatedly over a run, so collect every occurrence.
        """
        try:
            text = self.progress_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        chunks: List[str] = []
        for header in ("## Grounding", "## Codebase Patterns"):
            parts = re.split(rf"^{re.escape(header)}\s*$", text, flags=re.MULTILINE)
            for part in parts[1:]:
                nxt = re.search(r"^## ", part, flags=re.MULTILINE)
                chunk = (part[:nxt.start()] if nxt else part).strip()
                if chunk:
                    chunks.append(f"{header}\n{chunk}")
        if not chunks:
            return ""
        out = "\n\n".join(chunks)
        # Guard against the `[-0:]` slice inversion: in Python `out[-0:]` is `out[0:]`, so a
        # budget of 0 would return EVERYTHING instead of nothing, and a negative budget would
        # silently slice from the wrong end. A non-positive budget means "share nothing".
        if limit_chars <= 0:
            return ""
        return out[-limit_chars:] if len(out) > limit_chars else out

    def _merge_learnings(self, batch_result: Dict[str, Any]) -> None:
        """Consolidate the `Pattern:/Gotcha:/File:` lines workers reported.

        Each worker appends its own `## [US-xxx]` block (append-only, keyed by its own story id,
        so no race). The consolidated `## Codebase Patterns` section is orchestrator-owned —
        written once per batch, after the workers have all returned.
        """
        found, seen = [], set()
        for r in (batch_result.get("results") or []):
            for line in (r.get("summary", "") or "").splitlines():
                stripped = line.strip().lstrip("-*\u2022 ").strip()
                for label in ("Pattern:", "Gotcha:", "File:"):
                    if stripped.startswith(label):
                        val = stripped[len(label):].strip()
                        if val and val not in seen:
                            seen.add(val)
                            found.append(f"- {label} {val}")
        if not found:
            return
        payload = ["", "## Codebase Patterns", "",
                   f"(consolidated by the orchestrator from worker reports; {len(found)} fact(s))", ""]
        payload.extend(found)
        try:
            with open(self.progress_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(payload) + "\n")
            _log(self.progress_path,
                 f"merged {len(found)} learned fact(s) into ## Codebase Patterns")
        except Exception as exc:
            _log(self.progress_path, f"learnings merge failed: {exc!r}")

    # ── Batch planning: WHO runs together this round ────────────────────────
    # The caller picks the strategy (--parallel-mode); the planner only applies it. Every mode is
    # dependency-aware: a story whose `dependsOn` are not all passed yet is never scheduled.
    def _plan_batch(self, pending: List[Dict[str, Any]],
                    passed_ids: set) -> tuple:
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

    def _plan_auto(self, pending: List[Dict[str, Any]], passed_ids: set) -> tuple:
        eligible = [s for s in pending if _story_deps(s) <= passed_ids]
        if not eligible:
            return [], self._unrunnable_reason(pending, passed_ids)
        eligible.sort(key=lambda s: s.get("priority", 99))
        chosen: List[Dict[str, Any]] = []
        used: set = set()
        unknown: List[str] = []
        for s in eligible:
            if len(chosen) >= self.max_parallel:
                break
            files = _story_files(s)
            if not files:
                # Unknown conflict surface ≠ no conflict. Run it alone rather than risk two
                # workers writing the same file.
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

    def _plan_manual(self, pending: List[Dict[str, Any]], passed_ids: set) -> tuple:
        eligible = [s for s in pending if _story_deps(s) <= passed_ids]
        if not eligible:
            return [], self._unrunnable_reason(pending, passed_ids)
        eligible.sort(key=lambda s: s.get("priority", 99))
        head = eligible[0]
        key = str(head.get("parallelGroup") or "").strip()
        if not key:
            return [head], "mode=manual — no parallelGroup on the head story, running it alone"
        members = [s for s in eligible
                   if str(s.get("parallelGroup") or "").strip() == key][: self.max_parallel]
        # The caller owns the grouping, so an overlap is obeyed — but never silently: two workers
        # on one file corrupts it with no error, so say so in the log.
        seen: set = set()
        clashes = []
        for s in members:
            f = _story_files(s)
            if f & seen:
                clashes.append(str(s.get("id", "?")))
            seen |= f
        reason = f"mode=manual group={key!r} batch={len(members)}"
        if clashes:
            reason += f" ⚠ OVERLAPPING_FILES={clashes} — same file may be written twice, in parallel"
        return members, reason

    def _unrunnable_reason(self, pending: List[Dict[str, Any]], passed_ids: set) -> str:
        """Explain why nothing is runnable — usually a dependsOn that can never be satisfied."""
        known = {s.get("id") for s in _read_prd(self.prd_path).get("userStories", [])}
        bad = {}
        for s in pending:
            missing = {d for d in _story_deps(s) if d not in passed_ids}
            unknown_dep = {d for d in missing if d not in known}
            if unknown_dep:
                bad[str(s.get("id", "?"))] = sorted(unknown_dep)
        if bad:
            return (f"mode={self.parallel_mode} NOTHING RUNNABLE — dependsOn names stories that do "
                    f"not exist: {bad}")
        waiting = {str(s.get("id", "?")): sorted(_story_deps(s) - passed_ids) for s in pending}
        return (f"mode={self.parallel_mode} NOTHING RUNNABLE — every pending story is waiting on an "
                f"unpassed dependency: {waiting}")

    def _dead_stories(self, stories: List[Dict[str, Any]],
                      pending: List[Dict[str, Any]]) -> Dict[str, str]:
        """Map every story id that can NEVER run → the reason, propagated to a fixpoint.

        Two ways a story becomes unreachable: it was benched after repeated failure, or it waits
        on something that can never finish (a `dependsOn` naming a story that does not exist in
        the prd, or one that is itself dead). The second case matters: without propagating, a
        story behind a benched story is retried-forever-in-its-own-way and the loop limps to
        max_iterations instead of stopping with an accurate account.
        """
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
                deps = {str(d) for d in _story_deps(s)}
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
        """Charge one attempt to every dispatched story still not `passes: true`; bench at the cap.

        Called once per round, after the batch has fully returned (delegate_task is blocking, so
        by then every worker has stopped writing and the prd.json we read is stable).
        """
        if self.max_story_attempts <= 0:
            return
        try:
            prd = _read_prd(self.prd_path)
        except Exception as exc:
            _log(self.progress_path, f"attempt bookkeeping skipped — prd.json unreadable: {exc!r}")
            return
        state = {str(s.get("id", "?")): bool(s.get("passes", False))
                 for s in prd.get("userStories", [])}
        for s in batch:
            sid = str(s.get("id", "?"))
            if state.get(sid, False):
                continue                      # it passed; no attempt to charge
            n = self.attempts.get(sid, 0) + 1
            self.attempts[sid] = n
            if n >= self.max_story_attempts and sid not in self.benched:
                self.benched.add(sid)
                _log(self.progress_path,
                     f"BENCHED {sid} after {n} failed attempt(s) (cap="
                     f"{self.max_story_attempts}) — skipping it from here so the remaining "
                     f"stories can still finish")
            else:
                _log(self.progress_path,
                     f"attempt {n}/{self.max_story_attempts} failed for {sid}")

    def _exhausted_report(self, pending: List[Dict[str, Any]],
                          dead: Dict[str, str]) -> str:
        """One-line account of why the run has nothing left to do."""
        parts = [f"NOTHING LEFT TO RUN — {len(dead)} of {len(pending)} pending story(ies) are "
                 f"unreachable"]
        for sid in sorted(dead):
            parts.append(f"{sid}: {dead[sid]}")
        return " | ".join(parts)

    # ── Step 6: outer loop ──────────────────────────────────────────────────
    def _outer_loop(self) -> str:
        for round_idx in range(self.max_iter):
            prd = _read_prd(self.prd_path)
            stories = prd.get("userStories", [])
            pending = [s for s in stories if not s.get("passes", False)]
            if not pending:
                _log(self.progress_path, f"round {round_idx + 1}: ALL_PASSES")
                return "ALL_PASSES"
            if self.goal_manager is not None and not self.goal_manager.is_active():
                _log(self.progress_path, f"round {round_idx + 1}: goal_manager inactive → GOAL_CLEARED")
                return "GOAL_CLEARED"
            passed_ids = {str(s.get("id")) for s in stories if s.get("passes", False)}
            # Priority orders candidates; it never gates them. What can run is decided by
            # dependsOn, and stories that can never finish are excluded so one broken story
            # cannot starve the rest of the prd.
            dead = self._dead_stories(stories, pending)
            runnable = [s for s in pending if str(s.get("id", "?")) not in dead]
            if not runnable:
                _log(self.progress_path,
                     f"round {round_idx + 1}: STORIES_EXHAUSTED — "
                     f"{self._exhausted_report(pending, dead)}")
                return "STORIES_EXHAUSTED"
            if dead:
                _log(self.progress_path,
                     f"round {round_idx + 1}: skipping {sorted(dead)} "
                     f"({len(runnable)} story(ies) still runnable)")
            batch, reason = self._plan_batch(runnable, passed_ids)
            if not batch:
                # Never spin to max_iter on a scheduling deadlock: say why and stop.
                _log(self.progress_path, f"round {round_idx + 1}: {reason}")
                return "NO_PROGRESS"
            _log(self.progress_path,
                 f"round {round_idx + 1}: {reason} batch_size={len(batch)} "
                 f"stories={[s.get('id', '?') for s in batch]}")
            br = self._run_batch(batch)
            if br.get("status") != "OK":
                return "DELEGATION_FAILED"
            # Charge an attempt to whatever did not come back green, benching at the cap. Done
            # after the batch has fully returned, so no worker is still writing prd.json.
            self._record_attempts(batch)
            # Fold what these workers learned into the shared section, so the next batch inherits it
            # instead of rediscovering it. Orchestrator-owned → parallel workers never race on it.
            self._merge_learnings(br)
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
        # Hand each worker everything this run has established so far (recon grounding + facts
        # merged from earlier batches), so it reuses them instead of rediscovering or re-guessing.
        shared = self._read_shared_facts()
        tasks = [{"goal": _render_worker_prompt(self.prd_path, self.progress_path, s,
                                                shared_facts=shared),
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
            # Ground every story against the real codebase BEFORE any worker writes code. This is
            # the step upstream Ralph got for free by running each iteration inside the project;
            # once execution is delegated, it has to be done explicitly or workers guess interfaces.
            prd2 = self._step_recon(prd2)
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
    "NO_PROGRESS": 10,   # scheduler deadlock: nothing runnable (see _unrunnable_reason)
    "STORIES_EXHAUSTED": 11,   # remaining stories are unreachable (benched / dead dependsOn)
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
    p.add_argument("--no-recon", action="store_true",
                   help="Skip the recon pass — stories keep their paper acceptance criteria")
    p.add_argument("--recon-group", type=int, default=_RECON_GROUP_DEFAULT,
                   help=f"Stories per recon worker (default {_RECON_GROUP_DEFAULT})")
    p.add_argument("--project-root", default=None,
                   help="Codebase root the workers read (default: current working directory)")
    p.add_argument("--worker-provider", default=None,
                   help="Provider for the parent/worker agent (default: config.yaml model.provider)")
    p.add_argument("--worker-model", default=None,
                   help="Model for the parent/worker agent (default: config.yaml model.default)")
    p.add_argument("--parallel-mode", default=_PARALLEL_MODE_DEFAULT, choices=list(_PARALLEL_MODES),
                   help="How to group stories into a parallel batch (default: "
                        f"{_PARALLEL_MODE_DEFAULT}). 'off'=one at a time; 'priority'=equal priority; "
                        "'auto'=dependency+file-overlap aware (needs recon's file info); "
                        "'manual'=respect each story's parallelGroup label")
    p.add_argument("--max-parallel", type=int, default=_MAX_PARALLEL_DEFAULT,
                   help=f"Cap on stories per batch (default {_MAX_PARALLEL_DEFAULT})")
    p.add_argument("--max-story-attempts", type=int, default=_MAX_STORY_ATTEMPTS_DEFAULT,
                   help="Bench a story after this many failed attempts so it stops blocking the "
                        f"rest of the prd (default {_MAX_STORY_ATTEMPTS_DEFAULT}; 0 = never bench)")
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
                         args.session_id, auto_draft=not args.no_draft,
                         recon=not args.no_recon, recon_group=args.recon_group,
                         project_root=args.project_root,
                         worker_provider=args.worker_provider,
                         worker_model=args.worker_model,
                         parallel_mode=args.parallel_mode,
                         max_parallel=args.max_parallel,
                         max_story_attempts=args.max_story_attempts)
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
