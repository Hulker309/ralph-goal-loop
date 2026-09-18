"""Hermes platform adapter for ralph-goal-loop.

Constructs a persistent AIAgent and uses run_conversation for both orchestrator
turns and per-story execution. History is persisted to disk so the process can
be restarted and resumed.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ralph-goal-loop")

PROMISE_TOKEN = "<promise>COMPLETE</promise>"

# Lazy import so RalphCore (pure Python) can be imported without Hermes present.
try:
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    _HERMES_OK = True
    _HERMES_ERR: Optional[str] = None
except Exception as exc:
    _HERMES_OK = False
    _HERMES_ERR = repr(exc)


_SESSION_DIR = Path.home() / ".hermes" / "sessions"


def _session_dir(session_id: str) -> Path:
    return _SESSION_DIR / session_id


class HermesAdapter:
    """Hermes platform adapter using AIAgent.run_conversation.

    Implements PlatformAdapter. Uses a single AIAgent instance for the entire
    Ralph run so that all tool calls (from both orchestrator and worker turns)
    share one session and the agent can build on prior context.

    The adapter is Hermes-aware; all other Ralph code is pure Python.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        quiet_mode: bool = True,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_mode: Optional[str] = None,
    ) -> None:
        if not _HERMES_OK:
            raise RuntimeError(
                f"Hermes import failed — cannot use HermesAdapter: {_HERMES_ERR}"
            )

        self.session_id = session_id or f"ralph-{int(time.time())}"
        self.quiet_mode = quiet_mode

        # Resolve provider/model: CLI flag > config.yaml > None (let provider ladder decide)
        cfg = (load_config().get("model") or {}) if load_config else {}
        self.provider = provider or cfg.get("provider", "").strip() or None
        self.model = model or cfg.get("default", "").strip() or None

        rt = resolve_runtime_provider(
            requested=self.provider,
            target_model=self.model,
        )
        self._resolved_provider = rt.get("provider") or self.provider
        self._resolved_model = rt.get("model") or self.model
        self._resolved_base_url = rt.get("base_url") or base_url
        self._resolved_api_key = rt.get("api_key") or api_key
        self._resolved_api_mode = rt.get("api_mode") or api_mode

        self.agent = AIAgent(
            session_id=self.session_id,
            quiet_mode=self.quiet_mode,
            model=self._resolved_model,
            provider=self._resolved_provider,
            base_url=self._resolved_base_url,
            api_key=self._resolved_api_key,
            api_mode=self._resolved_api_mode,
        )

        # Load prior history if this is a resume
        self._history: List[Dict[str, Any]] = self._load_history()

    def _load_history(self) -> List[Dict[str, Any]]:
        """Load conversation history from disk if available."""
        sess_dir = _session_dir(self.session_id)
        hist_file = sess_dir / "conversation_history.json"
        if hist_file.exists():
            try:
                return json.loads(hist_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("load_history failed for %s: %s", self.session_id, exc)
        return []

    def _render_worker_prompt(self, story: Dict[str, Any], context: Dict[str, str]) -> str:
        """Build the worker prompt for a single story.

        This replicates the logic previously in RalphGoalLoop._render_worker_prompt
        so the adapter can build the prompt internally (US-002 acceptance criterion:
        "run_story(story, context) builds a worker prompt, then calls
        agent.run_conversation(...) again on the same agent").
        """
        from core.promise import PROMISE_TOKEN as _TOKEN

        sid = story.get("id", "?")
        stitle = story.get("title", "")
        sprio = story.get("priority", 1)
        ac_block = "\n".join(f"- {a}" for a in (story.get("acceptanceCriteria") or []))
        prd_path = context.get("prd_path", "")
        progress_path = context.get("progress_path", "")

        return (
            f"# Story {sid} — {stitle}\n\n"
            f"**Priority**: {sprio}\n\n"
            f"## Acceptance criteria\n{ac_block}\n\n"
            f"## Files\n- prd: {prd_path}\n- progress: {progress_path}\n\n"
            f"## Workflow\n"
            f"1. Read {prd_path}, confirm story {sid} is yours\n"
            f"2. Read {progress_path} — the `## Codebase Patterns` section first\n"
            f"3. Read the files you are about to change BEFORE writing; follow the patterns there\n"
            f"4. Implement story {sid}; verify each acceptance criterion by running its command\n"
            f"5. Set `passes: true` for story {sid} ONLY in {prd_path}\n"
            f"6. Append a `## [{sid}]` block to {progress_path} (append-only)\n"
            f"7. If ALL stories in {prd_path} have `passes: true`, end your response "
            f"with the literal token:\n\n{_TOKEN}\n\n"
            f"## Quality bar\n"
            f"- Do NOT mark `passes: true` without running the verification and pasting its output\n"
            f"- Do NOT commit broken code\n"
            f"- Follow the existing code patterns in the files you touch\n"
            f"- Write real assertions in tests, never placeholder `assert True`\n"
        )

    def run_story(
        self,
        story: Dict[str, Any],
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        """Run one story via the agent's run_conversation.

        Builds the worker prompt internally and reuses the agent instance so
        orchestrator history and worker tool calls share one session. On
        completion, saves updated history to disk.
        """
        # Enforce US-002 acceptance criteria 7 & 8: no delegate_task, no GoalManager refs
        assert "delegate_task" not in dir(self.agent), \
            "AIAgent must not implement delegate_task — no fan-out in v1.0"
        for _bad in ("GoalManager", "judge_goal", "draft_contract", "auxiliary.goal_judge"):
            assert _bad not in dir(type(self.agent)), \
                f"AIAgent must not reference {_bad} — these concepts were removed in v1.0"

        prompt = self._render_worker_prompt(story, context)

        try:
            response = self.agent.run_conversation(
                user_message=prompt,
                conversation_history=self._history,
            )
        except Exception as exc:
            logger.warning("run_conversation raised %s", exc)
            raise

        # Extract result — run_conversation returns flat snake_case fields, not a nested {message, tokens}.
        message = response.get("final_response", "") or ""
        messages = response.get("messages", []) or []
        tool_calls = []
        for m in messages:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    tool_calls.append(tc)
        # Fallback: if final_response is empty but last assistant message has content, use it
        if not message and messages:
            for m in reversed(messages):
                if m.get("role") == "assistant" and m.get("content"):
                    message = m["content"]
                    break

        # Update history
        self._history.append({"role": "user", "content": prompt})
        self._history.append({"role": "assistant", "content": message})

        self.save_history(self._history)

        return {
            "summary": message,
            "tokens": {
                "input": int(response.get("input_tokens", 0) or 0),
                "output": int(response.get("output_tokens", 0) or 0),
            },
            "tool_calls": tool_calls,
        }

    def run_orchestrator_turn(
        self,
        prompt: str,
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Run one orchestrator turn via the agent."""
        try:
            response = self.agent.run_conversation(
                user_message=prompt,
                conversation_history=history,
            )
        except Exception as exc:
            logger.warning("run_orchestrator_turn raised %s", exc)
            raise

        message = response.get("final_response", "") or ""
        messages = response.get("messages", []) or []
        tool_calls = []
        for m in messages:
            if m.get("role") == "assistant":
                tool_calls.extend(m.get("tool_calls") or [])
        if not message and messages:
            for m in reversed(messages):
                if m.get("role") == "assistant" and m.get("content"):
                    message = m["content"]
                    break

        return {
            "message": message,
            "tool_calls": tool_calls,
            "tokens": {
                "input": int(response.get("input_tokens", 0) or 0),
                "output": int(response.get("output_tokens", 0) or 0),
            },
        }

    def get_history(self) -> List[Dict[str, Any]]:
        """Return the agent's current conversation history."""
        return list(self._history)

    def save_history(self, history: List[Dict[str, Any]]) -> None:
        """Persist conversation history to disk for restart recovery.

        Best-effort: failures are logged but do not raise.
        """
        sess_dir = _session_dir(self.session_id)
        try:
            sess_dir.mkdir(parents=True, exist_ok=True)
            hist_file = sess_dir / "conversation_history.json"
            hist_file.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("save_history failed for %s: %s", self.session_id, exc)
