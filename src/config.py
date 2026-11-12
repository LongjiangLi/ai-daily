from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def parse_push_time(value: str) -> tuple[int, int]:
    """把 "08:30" 解析成 (8, 30)。"""
    try:
        hh, mm = value.strip().split(":")
        hh_i, mm_i = int(hh), int(mm)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"时间格式应为 HH:MM，实际是 {value!r}") from exc
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        raise ValueError(f"时间超出范围: {value}")
    return hh_i, mm_i


def compute_window(
    cfg: dict[str, Any], now_end: bool = False
) -> tuple[datetime, datetime]:
    """计算统计窗口，本地时区。

    默认是「昨天推送时间 → 今天推送时间」。
    now_end=True 时截止到当前时刻，窗口为「最近 24h」，用于下午手动调试：
    否则 08:30 之后发布的内容会被上界挡掉。
    """
    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)

    if now_end:
        end = now
        return end - timedelta(days=1), end

    hour, minute = parse_push_time(cfg.get("push_time", "08:30"))
    end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < end:
        # 还没到今天的推送时间，窗口回退一天
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return start, end
