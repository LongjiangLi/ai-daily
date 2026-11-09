from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from src.collectors import collect_all
from src.config import PROJECT_ROOT, compute_window, load_config
from src.extractors import (
    extract_frontier,
    extract_general,
    extract_local_models,
    score_articles,
    select_by_bucket,
    synthesize,
)
from src.llm import LLMClient
from src.mailer import send_mail
from src.models import Article, source_label
from src.prefilter import classify, prefilter
from src.render import render_html, render_plain
from src.state import load_state, mark_seen, prune, save_state, seen_ids

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _log(msg: str) -> None:
    print(msg, flush=True)


def build_report_dry(articles: list[Article], cfg: dict, start: datetime, end: datetime,
                     collect_stat: dict[str, int]) -> dict:
    """不调用 LLM，用规则分桶，用于验证采集与渲染链路。"""
    buckets: dict[str, list[Article]] = {"local_model": [], "frontier": [], "github": [], "agent": [], "news": []}
    for a in articles:
        buckets.setdefault(classify(a, cfg.get("keywords", {})), []).append(a)

    def simple(bucket: str, cap: int) -> list[dict]:
        out = []
        for a in sorted(buckets.get(bucket, []), key=lambda x: x.heat, reverse=True)[:cap]:
            out.append({
                "title": a.title,
                "name": a.title,
                "what": a.text_for_llm(120),
                "why_matters": "（dry-run：未调用 LLM）",
                "opportunity": "（dry-run：未调用 LLM）",
                "url": a.url,
                "source_label": source_label(a),
                "meta": f"🔥 {a.heat}" if a.heat else "",
            })
        return out

    local_models = []
    for a in sorted(buckets.get("local_model", []), key=lambda x: x.heat, reverse=True)[:5]:
        local_models.append({
            "name": a.title,
            "params": f"{a.extra.get('params_b') or '?'}B",
            "license": a.extra.get("license") or "?",
            "context": "?",
            "vram": [],
            "speed": "",
            "benchmarks": "",
            "stacks": [],
            "judgement": "（dry-run：未调用 LLM）",
            "url": a.url,
            "source_label": source_label(a),
        })

    return {
        "date": end.strftime("%Y-%m-%d"),
        "weekday": WEEKDAYS[end.weekday()],
        "window_start": start.strftime("%m-%d %H:%M"),
        "window_end": end.strftime("%m-%d %H:%M"),
        "scanned": len(articles),
        "selected": sum(len(simple(b, 99)) for b in buckets),
        "headline": ["（dry-run 模式，未生成摘要）"],
        "signals": [],
        "local_models": local_models,
        "frontier": [],
        "github": simple("github", 5),
        "agent": simple("agent", 3),
        "news": simple("news", 4),
        "stats": collect_stat,
        "usage": "",
    }


async def run_async(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    start, end = compute_window(cfg)
    date_str = end.strftime("%Y-%m-%d")
    _log(f"统计窗口：{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} "
         f"({cfg.get('timezone')})")

    state = load_state()
    selected = None

    _log("① 采集")
    articles, collect_stat = await collect_all(cfg, start)
    _log(f"    原始 {len(articles)} 条 {collect_stat}")

    _log("② 粗筛")
    articles = prefilter(articles, start, end, cfg, seen_ids(state))
    _log(f"    送入 LLM {len(articles)} 条")

    if args.dry_run:
        report = build_report_dry(articles, cfg, start, end, collect_stat)
    else:
        _log("③ 初始化 LLM")
        llm = LLMClient(cfg)

        _log("④ 打分与分桶")
        scores = await score_articles(llm, articles, cfg)
        selected = select_by_bucket(articles, scores, cfg)

        _log("⑤ 抽取内容"
             f"（本地 {len(selected['local_model'])} / 前沿 {len(selected['frontier'])} / "
             f"开源 {len(selected['github'])} / Agent {len(selected['agent'])} / 资讯 {len(selected['news'])}）")
        local_models = await extract_local_models(llm, selected["local_model"], cfg)
        frontier = await extract_frontier(llm, selected["frontier"], cfg)
        github = await extract_general(llm, selected["github"], "github", cfg)
        agent = await extract_general(llm, selected["agent"], "agent", cfg)
        news = await extract_general(llm, selected["news"], "news", cfg)

        _log("⑥ 生成头条与趋势")
        summary = await synthesize(llm, selected, cfg)

        selected_count = sum(len(v) for v in selected.values())
        report = {
            "date": date_str,
            "weekday": WEEKDAYS[end.weekday()],
            "window_start": start.strftime("%m-%d %H:%M"),
            "window_end": end.strftime("%m-%d %H:%M"),
            "scanned": len(articles),
            "selected": selected_count,
            "headline": summary.get("headline", []),
            "signals": summary.get("signals", []),
            "local_models": local_models,
            "frontier": frontier,
            "github": github,
            "agent": agent,
            "news": news,
            "stats": collect_stat,
            "usage": llm.usage_summary(),
        }
        _log(f"    {llm.usage_summary()}")

    sent_articles = [a for items in (selected or {}).values() for _, a, _ in items]

    _log("⑦ 渲染")
    html = render_html(report)
    plain = render_plain(report)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(html, encoding="utf-8")
        _log(f"    已写入 {out_path}")

    if args.dry_run or args.no_mail:
        _log("（跳过发信）")
        return

    _log("⑧ 发送邮件")
    subject = f"AI 日报 · {report['date']}（{report['weekday']}）"
    send_mail(subject, html, plain)

    # 只标记「真正推送出去」的条目，避免把没选上的内容也永久去重
    mark_seen(state, sent_articles)
    prune(state)
    save_state(state)
    _log("完成")


def main() -> None:
    # Windows 控制台默认 gbk，强制 utf-8 避免中文报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="AI 日报")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="不调用 LLM、不发信，只验证采集与渲染")
    parser.add_argument("--no-mail", action="store_true", help="跑完整流程但不发信")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "preview.html"), help="HTML 输出路径")
    args = parser.parse_args()

    asyncio.run(run_async(args))


if __name__ == "__main__":
    main()
