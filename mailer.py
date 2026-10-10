from __future__ import annotations

import html
import mimetypes
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

from config import settings


def _attachment_name(item: dict) -> str:
    """返回邮件正文与 MIME 附件共同使用的安全文件名。"""
    return " ".join(str(item.get("name") or "附件").split()) or "附件"


def _attachment_candidates(notices: list[dict]) -> tuple[list[dict], int]:
    items = []
    total = 0
    for notice in notices:
        for attachment in notice.get("attachments", []):
            path = Path(attachment.get("path", ""))
            if attachment.get("path") and path.is_file():
                size = path.stat().st_size
                items.append({**attachment, "path_obj": path, "actual_size": size})
                total += size
    return items, total


def _get_unsubscribe_url() -> str:
    if settings.unsubscribe_url:
        return settings.unsubscribe_url
    subject = quote("退订长安大学通知日报")
    body = quote("请停止向此邮箱发送长安大学通知日报。")
    return f"mailto:{settings.qq_email}?subject={subject}&body={body}"


def _render_html(notices: list[dict], attach_files: bool, date_text: str) -> str:
    sections = []
    for notice in notices:
        attachment_rows = []
        attachments = notice.get("attachments", [])
        for item in attachments:
            safe_name = html.escape(_attachment_name(item))
            path = Path(item.get("path", ""))
            is_attached = bool(attach_files and item.get("path") and path.is_file())
            if is_attached:
                attachment_rows.append(
                    f"<li style='margin:4px 0'>{safe_name} "
                    "<span style='color:#64748b'>（已附在邮件中）</span></li>"
                )
            elif item.get("url"):
                attachment_rows.append(
                    f"<li style='margin:4px 0'><a href='{html.escape(item['url'], quote=True)}' "
                    f"style='color:#1d4ed8'>{safe_name}</a> "
                    "<span style='color:#64748b'>（点击下载）</span></li>"
                )
            else:
                attachment_rows.append(
                    f"<li style='margin:4px 0'>{safe_name} "
                    "<span style='color:#64748b'>（未能取得下载链接）</span></li>"
                )
        attachment_html = (
            (
                "<div style='margin-top:12px;font-size:14px;color:#475569'>"
                f"<strong>附件（{len(attachments)}）</strong>"
                f"<ul style='margin:6px 0 0;padding-left:20px'>{''.join(attachment_rows)}</ul>"
                "</div>"
            )
            if attachment_rows
            else ""
        )
        metadata = " · ".join(
            html.escape(str(value))
            for value in (notice.get("department"), notice.get("publish_time"))
            if value
        )
        metadata_html = (
            f"<div style='margin:7px 0 12px;font-size:13px;color:#64748b'>{metadata}</div>"
            if metadata
            else ""
        )
        sections.append(
            "<div style='margin:0 0 20px;padding:20px;border:1px solid #e2e8f0;"
            "border-radius:10px;background:#ffffff'>"
            f"<div style='font-size:19px;line-height:1.5;font-weight:700;color:#0f172a'>"
            f"{html.escape(notice['title'])}</div>"
            f"{metadata_html}"
            f"<div style='font-size:14px;line-height:1.8;color:#334155'>"
            f"{html.escape(notice['summary'])}</div>"
            f"{attachment_html}"
            f"<div style='margin-top:14px'><a href='{html.escape(notice['detail_url'], quote=True)}' "
            "style='font-size:14px;font-weight:600;color:#1d4ed8;text-decoration:none'>"
            "查看通知详情 →</a></div>"
            "</div>"
        )
    unsubscribe_url = html.escape(_get_unsubscribe_url(), quote=True)
    return (
        "<!doctype html><html><body style='margin:0;padding:0;background:#f1f5f9'>"
        "<div style='display:none;max-height:0;overflow:hidden;color:transparent'>"
        f"{html.escape(date_text)} 共 {len(notices)} 条学校通知</div>"
        "<div style='max-width:680px;margin:0 auto;padding:28px 16px;"
        'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif\'>'
        "<div style='margin-bottom:22px'>"
        "<div style='font-size:26px;line-height:1.35;font-weight:800;color:#0f172a'>"
        "长安大学通知日报</div>"
        f"<div style='margin-top:8px;font-size:14px;color:#64748b'>{html.escape(date_text)} · "
        f"共 {len(notices)} 条</div></div>"
        + "".join(sections)
        + "<div style='padding:18px 8px 4px;text-align:center;font-size:12px;"
        "line-height:1.7;color:#94a3b8'>"
        "本邮件由校园通知系统自动发送。<br>"
        f"不希望继续接收？<a href='{unsubscribe_url}' "
        "style='color:#64748b;text-decoration:underline'>退订此日报</a>"
        "</div></div></body></html>"
    )


def _send_message(message: EmailMessage) -> None:
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.login(settings.qq_email, settings.qq_smtp_auth_code)
        smtp.send_message(message)


def send_digest(notices: list[dict], date_text: str, message_id: str) -> None:
    files, total = _attachment_candidates(notices)
    attach_files = total <= settings.max_attachment_bytes
    message = EmailMessage()
    message["From"] = settings.qq_email
    message["To"] = settings.my_email
    message["Subject"] = f"#【学校通知】—【{date_text}】学校通知日报（共 {len(notices)} 条）"
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"
    unsubscribe_url = _get_unsubscribe_url()
    message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    plain_sections = []
    for item in notices:
        attachment_text = ""
        if item.get("attachments"):
            names = "\n".join(
                f"- {_attachment_name(attachment)}" for attachment in item["attachments"]
            )
            attachment_text = f"\n附件：\n{names}"
        plain_sections.append(
            f"{item['title']}\n{item['summary']}{attachment_text}\n{item['detail_url']}"
        )
    message.set_content(
        f"长安大学通知日报（{date_text}，共 {len(notices)} 条）\n\n"
        + "\n\n".join(plain_sections)
        + f"\n\n退订：{unsubscribe_url}"
    )
    message.add_alternative(_render_html(notices, attach_files, date_text), subtype="html")
    if attach_files:
        for item in files:
            safe_name = _attachment_name(item)
            mime, _ = mimetypes.guess_type(safe_name)
            maintype, subtype = (mime or "application/octet-stream").split("/", 1)
            message.add_attachment(
                item["path_obj"].read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=safe_name,
            )

    _send_message(message)


def send_system_alert(title: str, detail: str) -> None:
    """发送独立故障告警；调用方必须捕获 SMTP 自身失败。"""
    now = datetime.now(settings.timezone).isoformat(timespec="seconds")
    message = EmailMessage()
    message["From"] = settings.qq_email
    message["To"] = settings.my_email
    message["Subject"] = f"【长安大学通知系统故障】{title}"
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(f"发生时间：{now}\n\n{detail}")
    message.add_alternative(
        "<!doctype html><html><body style='font-family:Segoe UI,Microsoft YaHei,Arial'>"
        "<h2 style='color:#b91c1c'>长安大学通知系统故障</h2>"
        f"<p><strong>{html.escape(title)}</strong></p>"
        f"<p style='color:#64748b'>发生时间：{html.escape(now)}</p>"
        f"<pre style='white-space:pre-wrap;background:#f8fafc;padding:12px'>"
        f"{html.escape(detail)}</pre>"
        "</body></html>",
        subtype="html",
    )
    _send_message(message)
