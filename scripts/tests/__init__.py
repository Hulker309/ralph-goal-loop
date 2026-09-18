"""scripts/tests/ — ralph-goal-loop unit tests.

Active tests:
  test_parallel.py  — batch planning (off/priority/auto/manual modes)
  test_starvation.py — dependsOn gating + benching after max-story-attempts

Archived (v0.x-specific, not applicable to v1.0):
  .archive/test_minidemo.py — tested v0.x GoalManager + judge LLM integration
  .archive/test_recon.py    — tested recon pass (removed in v1.0; see US-004)
"""
