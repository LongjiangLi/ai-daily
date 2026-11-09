from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from .config import PROJECT_ROOT
from .llm import LLMClient
from .models import Article, source_label

PROMPT_DIR = PROJECT_ROOT / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def attach_sources(rows: list, items: list[tuple[float, Article, str]]) -> list:
    """把 LLM 输出映射回原始 Article，补上数据来源标注。

    LLM 不知道条目从哪来，来源必须由程序注入，不能让它猜。
    优先按 id 匹配，其次按 url，避免模型改写链接导致丢失。
    """
    by_id: dict[str, Article] = {}
    by_url: dict[str, Article] = {}
    for _, a, _ in items:
        by_id[a.id] = a
        by_url[a.url.rstrip("/")] = a

    for row in rows:
        if not isinstance(row, dict):
            continue
        article = by_id.get(str(row.get("id") or "")) or by_url.get(
            str(row.get("url") or "").rstrip("/")
        )
        if article is not None:
            row["source_key"] = article.source
            row["source_label"] = source_label(article)
            row["heat"] = article.heat
        else:
            row.setdefault("source_label", "")
    return rows


# ---------------------------------------------------------------- 打分

async def score_articles(
    llm: LLMClient, articles: list[Article], cfg: dict
) -> dict[str, dict]:
    """把全部候选条目一次性批量打分（分批，每批 40 条）。

    这是成本最大的一步，所以输入用 brief 精简文本。
    """
    system = load_prompt("score")
    scores: dict[str, dict] = {}
    # 40 条 × 每条约 100 token 输出，容易顶到 max_tokens 被截断；30 条更稳
    batch_size = 30
    chunks = [articles[i : i + batch_size] for i in range(0, len(articles), batch_size)]

    async def score_chunk(idx: int, chunk: list[Article]) -> None:
        payload = {
            "items": [
                {
                    "id": a.id,
                    "source": a.source,
                    "title": a.title,
                    "heat": a.heat,
                    "summary": a.text_for_llm(300),
                }
                for a in chunk
            ]
        }
        data = await llm.json_call(system, _dumps(payload))
        got = 0
        for item in data.get("scores", []):
            if isinstance(item, dict) and item.get("id"):
                scores[item["id"]] = item
                got += 1
        print(f"    打分进度 {idx}/{len(chunks)} 批（{got} 条）", flush=True)

    # 批次之间互不依赖，并发跑；串行时 90 条要白等三次往返
    await asyncio.gather(*[score_chunk(i, c) for i, c in enumerate(chunks, 1)])
    return scores


def select_by_bucket(
    articles: list[Article], scores: dict[str, dict], cfg: dict
) -> dict[str, list[tuple[float, Article, str]]]:
    """按板块配额挑选；不足不补。"""
    quotas = cfg.get("sections", {})
    groups: dict[str, list[tuple[float, Article, str]]] = defaultdict(list)

    for a in articles:
        s = scores.get(a.id) or {}
        bucket = s.get("bucket") or "news"
        try:
            score = float(s.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        groups[bucket].append((score, a, str(s.get("reason", ""))))

    selected: dict[str, list[tuple[float, Article, str]]] = {}
    mapping = {
        "local_model": "llm_local",
        "frontier": "llm_frontier",
        "github": "github",
        "agent": "agent",
        "news": "news",
    }
    for bucket, key in mapping.items():
        # 同分时按 id 兜底排序，避免每次运行顺序漂移
        items = sorted(groups.get(bucket, []), key=lambda x: (-x[0], x[1].id))
        cap = int((quotas.get(key) or {}).get("max", 5))
        selected[bucket] = items[:cap]
    return selected


# ---------------------------------------------------------------- 本地模型卡片

async def extract_local_models(
    llm: LLMClient, items: list[tuple[float, Article, str]], cfg: dict
) -> list[dict]:
    if not items:
        return []

    payload = {
        "items": [
            {
                "id": a.id,
                "name": a.title,
                "params_b": a.extra.get("params_b"),
                "license": a.extra.get("license"),
                "likes": a.heat,
                "readme": (a.extra.get("readme") or "")[:3000],
                "url": a.url,
            }
            for _, a, _ in items
        ]
    }
    data = await llm.json_call(load_prompt("local_models"), _dumps(payload), max_tokens=3000)
    return attach_sources(_as_list(data.get("models")), items)


# ---------------------------------------------------------------- 前沿影响

async def extract_frontier(
    llm: LLMClient, items: list[tuple[float, Article, str]], cfg: dict
) -> list[dict]:
    if not items:
        return []

    payload = {
        "items": [
            {
                "id": a.id,
                "title": a.title,
                "source": a.source,
                "summary": a.text_for_llm(1500),
                "url": a.url,
            }
            for _, a, _ in items
        ]
    }
    # 这一步需要真正的推理，用 reasoner
    data = await llm.json_call(
        load_prompt("frontier"),
        _dumps(payload),
        model=llm.reasoning_model,
        max_tokens=3000,
    )
    return attach_sources(_as_list(data.get("items")), items)


# ---------------------------------------------------------------- 通用板块

async def extract_general(
    llm: LLMClient, items: list[tuple[float, Article, str]], section: str, cfg: dict
) -> list[dict]:
    if not items:
        return []

    payload = {
        "section": section,
        "items": [
            {
                "id": a.id,
                "title": a.title,
                "source": a.source,
                "summary": a.text_for_llm(1200),
                "heat": a.heat,
                "meta": a.extra,
                "url": a.url,
            }
            for _, a, _ in items
        ],
    }
    data = await llm.json_call(load_prompt("general"), _dumps(payload), max_tokens=2500)
    return attach_sources(_as_list(data.get("items")), items)


# ---------------------------------------------------------------- 头条与趋势

async def synthesize(
    llm: LLMClient,
    selected: dict[str, list[tuple[float, Article, str]]],
    cfg: dict,
) -> dict:
    brief = {
        bucket: [{"title": a.title, "summary": a.text_for_llm(200)} for _, a, _ in items]
        for bucket, items in selected.items()
        if items
    }
    if not brief:
        return {"headline": [], "signals": []}

    data = await llm.json_call(load_prompt("summary"), _dumps(brief), max_tokens=2000)
    return {
        "headline": [str(x) for x in _as_list(data.get("headline"))][:3],
        "signals": _as_list(data.get("signals"))[:3],
    }


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []
