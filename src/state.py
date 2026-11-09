from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .models import Article

STATE_PATH = PROJECT_ROOT / "state.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or STATE_PATH
    if not p.exists():
        return {"seen": {}}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("seen", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"seen": {}}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or STATE_PATH
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def seen_ids(state: dict[str, Any], days: int = 3) -> set[str]:
    """最近 N 天内推送过的 id，用于跳过重复条目。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {k for k, v in state.get("seen", {}).items() if str(v) >= cutoff}


def mark_seen(state: dict[str, Any], articles: list[Article]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    seen = state.setdefault("seen", {})
    for a in articles:
        seen[a.id] = now


def prune(state: dict[str, Any], days: int = 14) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    seen = state.get("seen", {})
    for key in [k for k, v in seen.items() if str(v) < cutoff]:
        seen.pop(key, None)
