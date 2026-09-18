"""Progress.txt append + Codebase Patterns merge logic extracted from ralph.py."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ralph-goal-loop")


def log(progress_path: Path, msg: str) -> None:
    """Append a timestamped line to progress.txt.

    progress.txt is the source of truth for run history (NOT stdout).
    Failures are logged to the module logger rather than raising.
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{ts}] ralph-orchestrator: {msg}"
    try:
        with open(progress_path, "a", encoding="utf-8") as fh:
            fh.write(log_line + "\n")
    except Exception as exc:
        logger.warning("progress.txt append failed: %s", exc)
    logger.info(msg)


def append_story_block(progress_path: Path, story_id: str, block: str) -> None:
    """Append a ## [US-xxx] block to progress.txt (append-only format)."""
    header = f"\n## [{story_id}]\n"
    try:
        with open(progress_path, "a", encoding="utf-8") as fh:
            fh.write(header)
            fh.write(block)
            fh.write("\n")
    except Exception as exc:
        logger.warning("append_story_block failed for %s: %s", story_id, exc)


def merge_codebase_patterns(progress_path: Path, batch_result: Dict[str, Any]) -> None:
    """Consolidate Pattern:/Gotcha:/File: lines from worker reports into ## Codebase Patterns.

    Each worker appends its own ## [US-xxx] block keyed by its story id (no race).
    The consolidated ## Codebase Patterns section is orchestrator-owned — written once
    per batch, after all workers have returned.
    """
    found: List[str] = []
    seen: set = set()

    for r in (batch_result.get("results") or []):
        for line in (r.get("summary", "") or "").splitlines():
            stripped = line.strip().lstrip("-*• ").strip()
            for label in ("Pattern:", "Gotcha:", "File:"):
                if stripped.startswith(label):
                    val = stripped[len(label):].strip()
                    if val and val not in seen:
                        seen.add(val)
                        found.append(f"- {label} {val}")

    if not found:
        return

    payload = [
        "",
        "## Codebase Patterns",
        "",
        f"(consolidated by the orchestrator from worker reports; {len(found)} fact(s))",
        "",
    ]
    payload.extend(found)

    try:
        with open(progress_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(payload) + "\n")
        log(progress_path, f"merged {len(found)} learned fact(s) into ## Codebase Patterns")
    except Exception as exc:
        logger.warning("merge_codebase_patterns failed: %s", exc)


def read_shared_facts(progress_path: Path, limit_chars: int = 6000) -> str:
    """Read back accumulated Grounding + Codebase Patterns for later workers to reuse.

    Both sections are appended to repeatedly over a run, so collect every occurrence.
    Returns the most recent characters up to limit_chars.
    """
    try:
        text = progress_path.read_text(encoding="utf-8", errors="replace")
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
    # Guard against the `[-0:]` slice inversion: non-positive budget means "share nothing".
    if limit_chars <= 0:
        return ""
    return out[-limit_chars:] if len(out) > limit_chars else out
