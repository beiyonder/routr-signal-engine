"""Email digest via SMTP. Sends the markdown digest as both text and HTML.

We don't depend on a third-party HTML-from-markdown lib; a small built-in renderer is enough
for the structure we produce.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ..lib.env import env
from ..lib.logging import info, warn
from ..lib.types import Digest
from .markdown_digest import render as render_markdown


def publish(digest: Digest) -> bool:
    host = env("EMAIL_SMTP_HOST")
    port_raw = env("EMAIL_SMTP_PORT")
    user = env("EMAIL_SMTP_USER")
    pwd = env("EMAIL_SMTP_PASS")
    sender = env("EMAIL_FROM") or user
    to_raw = env("EMAIL_TO")

    if not (host and port_raw and user and pwd and sender and to_raw):
        info("email: SMTP env not fully configured, skipping.")
        return False

    try:
        port = int(port_raw)
    except ValueError:
        warn(f"email: invalid EMAIL_SMTP_PORT={port_raw!r}")
        return False

    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
    if not recipients:
        warn("email: EMAIL_TO is empty, skipping.")
        return False

    markdown = render_markdown(digest)
    html = _markdown_to_html(markdown)

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Routr daily signal — {digest.date}"
    msg.set_content(markdown)
    msg.add_alternative(html, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(user, pwd)
                smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(user, pwd)
                smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
    except (smtplib.SMTPException, OSError) as e:
        warn(f"email: SMTP send failed: {e}")
        return False

    info(f"email: digest sent to {len(recipients)} recipient(s)")
    return True


def _markdown_to_html(md: str) -> str:
    """Very small markdown→HTML for the specific structure render() emits.

    Supports: H1/H2/H3, lists (`-`), bold `**…**`, italic `_…_`, inline code, links
    `[text](url)`, plus bare URLs that we wrap in <a>. Anything more exotic falls back to
    a <pre> block.
    """

    import html
    import re

    out: list[str] = ["<html><body style='font-family:system-ui,sans-serif;line-height:1.5'>"]

    in_list = False
    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("- ") or line.startswith("  - "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = line.lstrip(" -")
            out.append(f"<li>{_inline(html.escape(content))}</li>")
        elif line == "---":
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr/>")
        elif not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br/>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_inline(html.escape(line))}</p>")

    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(text: str) -> str:
    import re

    # bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # italic — single underscore not adjacent to letters
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)
    # inline code
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)
    # [label](url)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r"<a href='\2'>\1</a>",
        text,
    )
    # bare URLs
    text = re.sub(
        r"(?<!['\"\>])(https?://[^\s<]+)",
        r"<a href='\1'>\1</a>",
        text,
    )
    return text
