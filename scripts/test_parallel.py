"""Backward-compatibility stub: tests moved to scripts/tests/test_parallel.py.

v1.0 reorganized tests under scripts/tests/ per US-009.
Run tests from the new location: python scripts/tests/test_parallel.py
"""
import sys
from pathlib import Path

# Re-export from new location so existing imports still resolve
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(r"C:/Users/Administrator/.hermes/hermes-agent")))

# Redirect to the real test module
if __name__ == "__main__":
    from tests import test_parallel
    import unittest
    unittest.main(module=test_parallel, verbosity=2)