"""Root conftest: make the engine importable from anywhere the suite collects.

`tests/` is not the only place tests live. Each mission pack carries its own —
a pack asserts what it contains, which is a claim the engine cannot make on its
behalf — and `pytest.ini` collects them alongside. Those files sit outside
`tests/`, so they do not see its conftest, and the one thing they need is for
`surge_iw` to be importable.

Deliberately nothing else. A fixture defined here would be visible to a pack's
tests, and a pack test that depended on an engine fixture would stop working
the moment the pack was mounted somewhere else.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
