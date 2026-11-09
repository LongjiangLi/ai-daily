from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from .config import env


def send_mail(subject: str, html: str, plain: str) -> None:
    host = env("SMTP_HOST")
    user = env("SMTP_USER")
    password = env("SMTP_PASS")
    sender = env("MAIL_FROM", user)
    recipients = env("MAIL_TO")

    missing = [k for k, v in {"SMTP_HOST": host, "SMTP_USER": user,
                              "SMTP_PASS": password, "MAIL_TO": recipients}.items() if not v]
    if missing:
        raise RuntimeError(f"邮件配置缺失: {', '.join(missing)}")

    port = int(env("SMTP_PORT", "465") or 465)
    to_list = [x.strip() for x in (recipients or "").split(",") if x.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("AI 日报", sender or ""))
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)

    print(f"  [mail] 已发送至 {', '.join(to_list)}")
