from __future__ import annotations

import argparse
import asyncio
import sys
import time
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
    start, end = compute_window(cfg, now_end=args.now)
    date_str = end.strftime("%Y-%m-%d")
    _log(f"统计窗口：{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} "
         f"({cfg.get('timezone')})")

    state = load_state()
    selected = None

    # 当天已发送则直接退出：主 cron 与兜底 cron 可能都触发，
    # 被延迟的主任务也可能在兜底任务之后才启动，靠这个标记避免重复邮件。
    # dry-run / --no-mail / --force 不参与该检查。
    if (not args.dry_run and not args.no_mail and not args.force
            and state.get("last_sent_date") == date_str):
        _log(f"    {date_str} 的日报已发送过"
             f"（{state.get('last_sent_at', '时间未知')}），跳过；--force 可强制重发")
        return

    _log("① 采集")
    _t = time.perf_counter()
    articles, collect_stat = await collect_all(cfg, start)
    _log(f"    原始 {len(articles)} 条 {collect_stat}（{time.perf_counter() - _t:.1f}s）")

    _log("② 粗筛")
    funnel: dict[str, int] = {}
    articles = prefilter(
        articles,
        start,
        end,
        cfg,
        set() if args.ignore_seen else seen_ids(state),
        funnel,
    )
    _log(f"    去重后 {funnel['after_dedupe']}/{funnel['raw']} 条"
         f"（最近已推送 {funnel['already_seen']} 条）")
    _log(f"    时间窗内 {funnel['after_window']} 条")
    _log(f"    关键词过滤后 {funnel['after_keyword']} 条")
    if "github_mature" in funnel:
        _log(f"    GitHub 候选池：成熟 {funnel['github_mature']} / "
             f"高成长 {funnel['github_growth']} / Java {funnel['github_java']}")
    _log(f"    截断后送入 LLM {funnel['after_cap']} 条")

    if args.dry_run:
        report = build_report_dry(articles, cfg, start, end, collect_stat)
    else:
        _log("③ 初始化 LLM")
        llm = LLMClient(cfg)

        _log("④ 打分与分桶")
        _t = time.perf_counter()
        scores = await score_articles(llm, articles, cfg)
        selected = select_by_bucket(articles, scores, cfg)
        _log(f"    打分完成（{time.perf_counter() - _t:.1f}s）")

        _log("⑤ 抽取内容与头条"
             f"（本地 {len(selected['local_model'])} / 前沿 {len(selected['frontier'])} / "
             f"开源 {len(selected['github'])} / Agent {len(selected['agent'])} / 资讯 {len(selected['news'])}）")
        _t = time.perf_counter()
        # 五个板块互不依赖；synthesize 也只用 selected，可以和它们一起并发。
        # 串行时总耗时是「本地 + 前沿(reasoner) + 三个通用 + 摘要」之和。
        local_models, frontier, github, agent, news, summary = await asyncio.gather(
            extract_local_models(llm, selected["local_model"], cfg),
            extract_frontier(llm, selected["frontier"], cfg),
            extract_general(llm, selected["github"], "github", cfg),
            extract_general(llm, selected["agent"], "agent", cfg),
            extract_general(llm, selected["news"], "news", cfg),
            synthesize(llm, selected, cfg),
        )
        _log(f"    抽取与摘要完成（{time.perf_counter() - _t:.1f}s）")

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

    _log("⑥ 渲染")
    html = render_html(report)
    plain = render_plain(report)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(html, encoding="utf-8")
        _log(f"    已写入 {out_path}")

    if args.dry_run or args.no_mail:
        _log("（跳过发信）")
        return

    _log("⑦ 发送邮件")
    subject = f"AI 日报 · {report['date']}（{report['weekday']}）"
    send_mail(subject, html, plain)

    # 记录「今天已发」：兜底 cron / 手动补发靠它判断是否还要再发一封。
    # end.tzinfo 就是 config 里配置的时区（默认 Asia/Shanghai）。
    state["last_sent_date"] = date_str
    state["last_sent_at"] = datetime.now(end.tzinfo).isoformat(timespec="seconds")

    if args.ignore_seen:
        _log("（--ignore-seen：不更新 state）")
        return

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
    parser.add_argument(
        "--now",
        action="store_true",
        help="统计窗口截止到当前时刻（最近 24h），下午手动调试用",
    )
    parser.add_argument(
        "--ignore-seen",
        action="store_true",
        help="忽略 state.json 里最近推送过的条目（调试用，不会写 state）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使 state.json 记录今天已发送过，也强制再发一封",
    )
    parser.add_argument("--out", default=str(PROJECT_ROOT / "preview.html"), help="HTML 输出路径")
    args = parser.parse_args()

    asyncio.run(run_async(args))


if __name__ == "__main__":
    main()
