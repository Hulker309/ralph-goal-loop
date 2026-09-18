"""OpenCLAW platform adapter stub for ralph-goal-loop.

This is a TODO stub. To implement OpenCLAW support, one would need to:

1. Implement run_story(story, context):
   - Construct an OpenCLAW agent run loop (similar to how HermesAdapter uses AIAgent)
   - Send the worker prompt to OpenCLAW and receive a response
   - Extract summary/tokens/tool_calls from OpenCLAW's response format
   - Check for <promise>COMPLETE</promise> in the response

2. Implement run_orchestrator_turn(prompt, history):
   - Run a turn with the OpenCLAW agent using the orchestrator prompt
   - Return dict with message/tool_calls/tokens in the same shape as HermesAdapter

3. Implement get_history() / save_history(history):
   - OpenCLAW may have its own session/history management
   - Need to persist history to disk so the process can be restarted and resumed

4. Implement detect_promise() or similar:
   - OpenCLAW output format may differ from Hermes AIAgent
   - Need a way to detect <promise>COMPLETE</promise> in OpenCLAW's response

The key difference from HermesAdapter is that OpenCLAW may use a different
agent API, different response format, and different session management.
"""

from __future__ import annotations

from typing import Any, Dict, List


class OpenCLAWAdapter:
    """OpenCLAW platform adapter — TODO stub.

    This adapter would need to implement the PlatformAdapter interface for
    an OpenCLAW agent run loop. See the module docstring above for what
    needs to be implemented.
    """

    def __init__(self, **kwargs) -> None:
        raise NotImplementedError(
            "OpenCLAW adapter TODO — see PRD US-013.\n\n"
            "To implement OpenCLAW support, you need to:\n"
            "1. Implement run_story(story, context) — construct an OpenCLAW agent,\n"
            "   send the worker prompt, extract response in {summary, tokens, tool_calls} shape\n"
            "2. Implement run_orchestrator_turn(prompt, history) — same shape as HermesAdapter\n"
            "3. Implement get_history() / save_history(history) — OpenCLAW session persistence\n"
            "4. Detect <promise>COMPLETE</promise> in OpenCLAW's output format\n\n"
            "Then update scripts/ralph.py to accept --platform {hermes,openclaw} and construct\n"
            "the appropriate adapter."
        )

    def run_story(
        self,
        story: Dict[str, Any],
        context: Dict[str, str],
    ) -> Dict[str, Any]:
        raise NotImplementedError("OpenCLAW adapter TODO — see __init__ docstring")

    def run_orchestrator_turn(
        self,
        prompt: str,
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        raise NotImplementedError("OpenCLAW adapter TODO — see __init__ docstring")

    def get_history(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("OpenCLAW adapter TODO — see __init__ docstring")

    def save_history(self, history: List[Dict[str, Any]]) -> None:
        raise NotImplementedError("OpenCLAW adapter TODO — see __init__ docstring")
