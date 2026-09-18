"""Promise token detection extracted from ralph.py."""

from __future__ import annotations

PROMISE_TOKEN = "<promise>COMPLETE</promise>"


def detect_promise(assistant_message: str) -> bool:
    """Return True when the literal <promise>COMPLETE</promise> token appears in the message.

    This is Ralph's original completion protocol: the worker declares done-ness by
    emitting the token in its final response. The orchestrator checks every story result
    for this token and treats it as a definitive ALL_PASSES signal, overriding any
    other judge or state mechanism.
    """
    return PROMISE_TOKEN in (assistant_message or "")
