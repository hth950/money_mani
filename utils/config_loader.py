"""YAML config loader with .env support."""

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def get_env(key: str, default: str = "") -> str:
    load_dotenv(ENV_PATH)
    return os.getenv(key, default)


def _substitute_env_vars(obj):
    """Recursively substitute ${VAR} patterns with environment variables."""
    if isinstance(obj, str):
        pattern = re.compile(r"\$\{(\w+)\}")
        def replacer(m):
            return os.getenv(m.group(1), m.group(0))
        return pattern.sub(replacer, obj)
    elif isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_substitute_env_vars(item) for item in obj]
    return obj


def load_config(config_name: str = "settings.yaml") -> dict:
    """Load a YAML config file from config/ directory with env var substitution."""
    load_dotenv(ENV_PATH)
    config_path = PROJECT_ROOT / "config" / config_name
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _substitute_env_vars(raw)


def load_strategy(strategy_file: str) -> dict:
    """Load a strategy YAML from config/strategies/."""
    path = PROJECT_ROOT / "config" / "strategies" / strategy_file
    if not path.exists():
        raise FileNotFoundError(f"Strategy not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
