"""发送心跳告警：到点检查今天是否已经发出过日报。

主触发：Cloudflare Worker 的 Cron Trigger 在北京 09:33 调用 workflow_dispatch
（见 cloudflare/README.md），check 时机准确。
兜底：.github/workflows/heartbeat.yml 仍保留 1 档 GitHub schedule（UTC 19:23，
北京 03:23 排队）。按 2026-11 实测的 5h-7h 调度延迟约 08:23-10:23 运行；早于
--earliest（09:30）时直接退出，所以延迟短的日子这档不检查，属正常现象。

GitHub 的 schedule 是 best-effort，任务可能延迟数小时甚至被静默丢弃，
被丢弃时 Actions 里不会留下任何失败记录，所以主触发改由外部定时器保证。

判定依据是 state.json 里的 last_sent_date（main.py 发信成功后写入）。
为避免和仍在延迟中的主/兜底任务抢跑，默认最多轮询 10 分钟：
每 60 秒从 origin/main 重新拉一次 state.json，仍没有记录才发告警邮件。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import load_config, parse_push_time
from src.mailer import send_mail
from src.state import load_state

ACTIONS_URL = "https://github.com/LongjiangLi/ai-daily/actions"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _refresh_from_origin() -> None:
    """在 CI 中把工作区同步到 origin/main，拿到日报 workflow 刚提交的 state.json。

    actions/checkout 默认是 detached HEAD + 浅克隆，所以用 fetch + reset，
    不用 git pull。只在 GitHub Actions 里执行，避免本地运行时冲掉未提交改动。
    """
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    subprocess.run(
        ["git", "fetch", "--quiet", "origin",
         "+refs/heads/main:refs/remotes/origin/main"],
        check=False,
    )
    subprocess.run(["git", "reset", "--hard", "-q", "origin/main"], check=False)


def main() -> None:
    # Windows 控制台默认 gbk，强制 utf-8，避免中文日志显示乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="AI 日报心跳检查")
    parser.add_argument(
        "--earliest",
        default=None,
        metavar="HH:MM",
        help="早于该时刻（配置时区）不检查也不告警，直接退出",
    )
    args = parser.parse_args()

    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)

    # 与 daily.yml 的 --earliest 同一个道理：cron 是提前量，延迟消失时可能凌晨
    # 就跑起来，此时日报本来就不该发，更不能发「未发出」的假告警。
    if args.earliest:
        hh, mm = parse_push_time(args.earliest)
        earliest = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < earliest:
            _log(f"[heartbeat] 当前 {now:%H:%M} 早于 {args.earliest}，"
                 f"不检查（定时任务仍在补偿窗口内）")
            return

    today = now.strftime("%Y-%m-%d")

    wait_seconds = int(os.getenv("HEARTBEAT_WAIT_SECONDS", "600"))
    poll_seconds = int(os.getenv("HEARTBEAT_POLL_SECONDS", "60"))
    deadline = time.monotonic() + wait_seconds

    state = load_state()
    while state.get("last_sent_date") != today:
        if time.monotonic() >= deadline:
            break
        _log(f"[heartbeat] 尚未检测到 {today} 的发送记录，{poll_seconds}s 后复查…")
        time.sleep(poll_seconds)
        _refresh_from_origin()
        state = load_state()

    if state.get("last_sent_date") == today:
        _log(f"[heartbeat] {today} 的日报已发送"
             f"（{state.get('last_sent_at', '时间未知')}），一切正常")
        return

    now = datetime.now(tz)
    last_date = state.get("last_sent_date") or "无记录"
    last_at = state.get("last_sent_at") or "—"
    subject = f"⚠️ AI 日报可能未发出 · {today}"

    plain = (
        f"到北京时间 {now:%H:%M}，仍然没有检测到今天的日报发送记录。\n\n"
        f"今天：{today}\n"
        f"最近一次发送：{last_date}（{last_at}）\n\n"
        "可能原因：\n"
        "1. GitHub Actions 的定时任务被延迟（可能晚数小时）或被直接丢弃"
        "（丢弃不会产生任何失败记录）；\n"
        "2. workflow 运行失败（LLM / SMTP / 网络）；\n"
        "3. 邮件已发出，但 state.json 提交回仓库失败。\n\n"
        f"请打开 Actions 手动触发一次：{ACTIONS_URL}\n"
    )
    html = f"""\
<html><body style="font-family:-apple-system,'Segoe UI',sans-serif;line-height:1.7;color:#222;">
<h2 style="color:#b45309;margin:0 0 12px;">⚠️ 今天的 AI 日报可能没有发出</h2>
<p>到北京时间 <b>{now:%H:%M}</b>，仍然没有检测到今天的日报发送记录。</p>
<table cellpadding="6" style="border-collapse:collapse;font-size:14px;">
  <tr><td style="color:#666;">今天</td><td>{today}</td></tr>
  <tr><td style="color:#666;">最近一次发送</td><td>{last_date}（{last_at}）</td></tr>
</table>
<p><b>可能原因</b></p>
<ol>
  <li>GitHub Actions 的定时任务被延迟（可能晚数小时）或被直接丢弃（丢弃不会产生任何失败记录）；</li>
  <li>workflow 运行失败（LLM / SMTP / 网络）；</li>
  <li>邮件已发出，但 state.json 提交回仓库失败。</li>
</ol>
<p><a href="{ACTIONS_URL}">打开 Actions 手动触发一次 →</a></p>
</body></html>"""

    send_mail(subject, html, plain)
    _log(f"[heartbeat] 已发送告警邮件（今天={today}，最近发送={last_date}）")


if __name__ == "__main__":
    main()
