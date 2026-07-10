from __future__ import annotations

import os
from pathlib import Path


CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("AKA_PLUGIN_DATA_DIR", CODE_DIR)).expanduser().resolve()


def data_path(name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else DATA_DIR / path

