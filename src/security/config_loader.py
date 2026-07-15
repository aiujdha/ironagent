from functools import lru_cache
from pathlib import Path

import yaml

from src.shared.config import get_settings


def _config_dir() -> Path:
    return get_settings().config_dir


@lru_cache(maxsize=16)
def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def capability_file(profile_name: str) -> Path:
    return _config_dir() / "capabilities" / f"{profile_name}.yaml"


def pruning_file() -> Path:
    return _config_dir() / "pruning.yaml"


def approval_file() -> Path:
    return _config_dir() / "approval.yaml"
