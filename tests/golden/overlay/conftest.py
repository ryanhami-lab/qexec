"""Expose the shared WP2-OVERLAY test helper (``_overlay_helpers``) on ``sys.path``."""

from __future__ import annotations

import sys
from pathlib import Path

_HELPER_DIR = Path(__file__).parents[2] / "unit" / "sim"
sys.path.insert(0, str(_HELPER_DIR))
