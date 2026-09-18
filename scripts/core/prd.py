"""PRD read/write/normalize logic extracted from ralph.py."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("ralph-goal-loop")


def read_prd(prd_path: Path) -> Dict[str, Any]:
    """Read and parse a prd.json file."""
    return json.loads(prd_path.read_text(encoding="utf-8"))


def write_prd(prd_path: Path, prd: Dict[str, Any]) -> None:
    """Write prd.json back to disk."""
    prd_path.write_text(json.dumps(prd, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_prd(prd: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure every story has id, priority, and passes fields populated.

    - priority defaults to its index position (1-based) so ordering is stable
    - id defaults to US-<index>
    - passes is False by default (only True when the story is complete)
    """
    for idx, s in enumerate(prd.get("userStories", []), start=1):
        s.setdefault("priority", idx)
        s.setdefault("id", f"US-{idx}")
        s["passes"] = bool(s.get("passes", False))
    return prd


def get_pending_stories(prd: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return stories where passes is False."""
    return [s for s in prd.get("userStories", []) if not s.get("passes", False)]


def get_passed_ids(prd: Dict[str, Any]) -> set:
    """Return set of story ids that have passes=True."""
    return {str(s.get("id")) for s in prd.get("userStories", []) if s.get("passes", False)}


def all_pass(prd: Dict[str, Any]) -> bool:
    """Return True when every story in the prd has passes=True."""
    return all(s.get("passes", False) for s in prd.get("userStories", []))


def story_deps(story: Dict[str, Any]) -> set:
    """Story ids this story waits on.

    Accepts both camelCase (dependsOn) and snake_case (depends_on) field names.
    Returns a set of dependency story ids.
    """
    raw = story.get("dependsOn")
    if raw is None:
        raw = story.get("depends_on")
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set()


def story_files(story: Dict[str, Any]) -> set:
    """File paths a story is expected to touch.

    Reads both the explicit `files` field and the deprecated `recon.files` field
    (for backward compatibility with v0.4.0 stories that used recon). Empty set
    means "conflict surface unknown" — callers must treat that as a blocker,
    never as "no conflicts".
    """
    out = set()
    for p in (story.get("files") or []):
        if isinstance(p, str) and p.strip():
            out.add(_normalize_path(p))
        elif isinstance(p, dict) and (p.get("path") or "").strip():
            out.add(_normalize_path(str(p["path"])))
    # Backward compatibility: also read recon.files (v0.4.0 stories had recon grounding)
    recon = story.get("recon") or {}
    for p in (recon.get("files") or []):
        if isinstance(p, str) and p.strip():
            out.add(_normalize_path(p))
        elif isinstance(p, dict) and (p.get("path") or "").strip():
            out.add(_normalize_path(str(p["path"])))
    return out


def _normalize_path(p: str) -> str:
    """Normalize a file path for comparison purposes."""
    return p.strip().replace("\\", "/").lstrip("./").lower()
