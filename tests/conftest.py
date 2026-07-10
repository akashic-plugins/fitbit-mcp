from __future__ import annotations

import os
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parents[1]
agent_root = Path(
    os.environ.get("AKASHIC_AGENT_ROOT", "").strip()
    or repo_root.parents[1] / "akasic-agent"
)
for path in (repo_root, agent_root):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
