from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


def load_env_file(path: str | Path) -> Dict[str, str]:
    env_path = Path(path)
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
            os.environ.setdefault(key, value)
    return values


def load_default_env() -> Dict[str, str]:
    if os.environ.get("LOAD_ENV_FILE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {}
    root = Path(__file__).resolve().parents[3]
    configured_path = os.environ.get("ENV_FILE", "").strip()
    return load_env_file(Path(configured_path) if configured_path else root / ".env.local")


def llm_config_from_env() -> Dict[str, str]:
    load_default_env()
    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    api_key = os.environ.get("API_KEY", "")
    model_name = os.environ.get("MODEL_NAME", "")
    model_api_name = os.environ.get("MODEL_API_NAME") or model_name
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
        "model_api_name": model_api_name,
    }
