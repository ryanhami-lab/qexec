"""Make ``_fhelpers`` importable from this directory as a bare module."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
