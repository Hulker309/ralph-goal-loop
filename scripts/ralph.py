#!/usr/bin/env python3
"""Ralph goal-loop — thin entry point wiring RalphCore + PlatformAdapter.

CLI:
    python scripts/ralph.py --prd <path> [--max-iter N] [--cost-cap USD]
        [--project-root PATH] [--worker-provider NAME] [--worker-model NAME]
        [--parallel-mode {off,priority,auto,manual}] [--max-parallel N]
        [--max-story-attempts N]

Exit codes:
    0  ALL_PASSES                  2  MAX_ITERATIONS       3  COST_CAP
    6  DELEGATION_FAILED           8  INTERNAL_ERROR      10  NO_PROGRESS
   11  STORIES_EXHAUSTED

Architecture:
    scripts/core/        — RalphCore: pure Python orchestrator (no Hermes imports)
    scripts/adapters/    — PlatformAdapter abstract + HermesAdapter concrete
    scripts/ralph.py     — thin entry point: parse args, wire adapter + orchestrator, run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from core.orchestrator import (
    DEFAULT_COST_CAP_USD,
    DEFAULT_MAX_ITER,
    RalphGoalLoop,
    _STATUS_TO_EXIT,
    _MAX_PARALLEL_DEFAULT,
    _MAX_STORY_ATTEMPTS_DEFAULT,
    _PARALLEL_MODE_DEFAULT,
    _PARALLEL_MODES,
)
from core.progress import log
from core.prd import story_deps as _story_deps, story_files as _story_files


def _resolve_judge_overrides(*args, **kwargs):
    """v1.0: judge override system removed; returns empty dict. Kept for test compat."""
    return {}


try:
    from adapters.hermes import HermesAdapter
    from adapters.openclaw import OpenCLAWAdapter as _OpenCLAWAdapter
    _ADAPTER_OK = True
    _ADAPTER_ERR: Optional[str] = None
except Exception as exc:
    _ADAPTER_OK = False
    _ADAPTER_ERR = repr(exc)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ralph",
        description="ralph-goal-loop orchestrator (v1.0 rewrite)",
    )
    parser.add_argument(
        "--prd", required=True,
        help="Path to prd.json",
    )
    parser.add_argument(
        "--max-iter", type=int, default=DEFAULT_MAX_ITER,
        help=f"Maximum iterations (default {DEFAULT_MAX_ITER})",
    )
    parser.add_argument(
        "--cost-cap", type=float, default=DEFAULT_COST_CAP_USD,
        help=f"Cost cap in USD (default {DEFAULT_COST_CAP_USD})",
    )
    parser.add_argument(
        "--project-root", default=None,
        help="Codebase root workers read (default: current working directory)",
    )
    parser.add_argument(
        "--worker-provider", default=None,
        help="LLM provider for the agent (default: config.yaml model.provider)",
    )
    parser.add_argument(
        "--worker-model", default=None,
        help="LLM model for the agent (default: config.yaml model.default)",
    )
    parser.add_argument(
        "--parallel-mode",
        default=_PARALLEL_MODE_DEFAULT,
        choices=list(_PARALLEL_MODES),
        help=f"How to group stories into a batch (default {_PARALLEL_MODE_DEFAULT})",
    )
    parser.add_argument(
        "--max-parallel", type=int, default=_MAX_PARALLEL_DEFAULT,
        help=f"Cap on stories per batch (default {_MAX_PARALLEL_DEFAULT})",
    )
    parser.add_argument(
        "--max-story-attempts", type=int, default=_MAX_STORY_ATTEMPTS_DEFAULT,
        help=f"Bench a story after this many failures (default {_MAX_STORY_ATTEMPTS_DEFAULT}; 0=never)",
    )
    parser.add_argument(
        "--platform",
        default="hermes",
        choices=["hermes", "openclaw"],
        help="Platform adapter to use (default hermes). openclaw raises NotImplementedError.",
    )
    args = parser.parse_args(argv)

    if not Path(args.prd).exists():
        print(f"FATAL: prd not found: {args.prd}", file=sys.stderr)
        return 8

    if not _ADAPTER_OK:
        print(f"FATAL: HermesAdapter unavailable: {_ADAPTER_ERR}", file=sys.stderr)
        return 8

    # Instantiate adapter and orchestrator
    try:
        if args.platform == "hermes":
            adapter = HermesAdapter(
                provider=args.worker_provider,
                model=args.worker_model,
            )
        elif args.platform == "openclaw":
            adapter = _OpenCLAWAdapter(
                provider=args.worker_provider,
                model=args.worker_model,
            )
    except Exception as exc:
        print(f"FATAL: {args.platform} adapter init failed: {exc}", file=sys.stderr)
        return 8

    orch = RalphGoalLoop(
        prd_path=args.prd,
        adapter=adapter,
        max_iter=args.max_iter,
        cost_cap_usd=args.cost_cap,
        project_root=args.project_root,
        parallel_mode=args.parallel_mode,
        max_parallel=args.max_parallel,
        max_story_attempts=args.max_story_attempts,
    )

    status = orch.run()
    print(status)
    return _STATUS_TO_EXIT.get(status, 8)


if __name__ == "__main__":
    sys.exit(main())
