"""Expose the shared WP1-BOOK test helper (``_helpers``) on ``sys.path``."""

from __future__ import annotations

import sys
from pathlib import Path

_HELPER_DIR = Path(__file__).parents[1] / "reference"
sys.path.insert(0, str(_HELPER_DIR))
