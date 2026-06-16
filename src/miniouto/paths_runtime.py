"""Process-wide constants captured at import time."""

from __future__ import annotations

import os
from pathlib import Path

INVOCATION_CWD: Path = Path(os.getcwd()).resolve()
