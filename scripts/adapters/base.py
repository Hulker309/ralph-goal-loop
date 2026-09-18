"""Abstract PlatformAdapter interface for ralph-goal-loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class PlatformAdapter(ABC):
    """Abstract adapter for running Ralph stories on a specific agent platform.

    Implementors must provide:
        run_story(story, context, prompt) — run one story, return {"summary": str, "tokens": dict}
        run_orchestrator_turn(prompt, history) — run an orchestrator turn, return dict
        get_history() — return the agent's conversation history
        save_history(history) — persist history to disk (best-effort)

    RalphCore (orchestrator.py) is platform-agnostic and drives the loop by calling these
    methods. The adapter is the only module that imports Hermes, OpenCLAW, or any other
    agent runtime.
    """

    @abstractmethod
    def run_story(
        self,
        story: Dict[str, Any],
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        """Run a single story and return the result.

        The adapter builds the worker prompt internally from the story and context.
        The prompt includes the story id/title/priority, acceptance criteria, and
        workflow instructions (read prd, read progress, implement, verify, mark passes).

        Args:
            story: The story dict from prd.json (id, title, acceptanceCriteria, etc.)
            context: Dict with keys prd_path, progress_path, project_root

        Returns:
            Dict with at least {"summary": str, "tokens": {"input": int, "output": int}}
            On failure, raises an exception or returns {"status": "DELEGATION_FAILED", ...}
        """
        ...

    @abstractmethod
    def run_orchestrator_turn(
        self,
        prompt: str,
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Run one orchestrator turn with the given prompt and history.

        Args:
            prompt: The user message to send to the agent.
            history: List of prior conversation messages.

        Returns:
            Dict with keys {message, tool_calls, tokens: {input, output}}
        """
        ...

    @abstractmethod
    def get_history(self) -> List[Dict[str, Any]]:
        """Return the agent's current conversation history."""
        ...

    @abstractmethod
    def save_history(self, history: List[Dict[str, Any]]) -> None:
        """Persist conversation history to disk for restart recovery.

        Best-effort: failures are logged but do not raise.
        """
        ...
