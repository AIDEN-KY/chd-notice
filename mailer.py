from __future__ import annotations

import html
import mimetypes
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import make_msgid
from pathlib import Path
from urllib.parse import quote

from config import settings
from db import validate_email


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


def _send_message(message: EmailMessage, recipients: list[str] | None = None) -> dict:
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.login(settings.qq_email, settings.qq_smtp_auth_code)
        try:
            refused = (
                smtp.send_message(message, from_addr=settings.qq_email, to_addrs=recipients)
                if recipients is not None
                else smtp.send_message(message)
            )
        except smtplib.SMTPRecipientsRefused as exc:
            if recipients is None:
                raise
            refused = exc.recipients
        except smtplib.SMTPDataError as exc:
            if recipients is None:
                raise
            refused = {address: (exc.smtp_code, exc.smtp_error) for address in recipients}
        if recipients is None and refused:
            raise RuntimeError("SMTP 收件人拒收")
        return {
            "accepted": [address for address in (recipients or []) if address not in refused],
            "refused": refused,
        }


def _build_digest(
    notices: list[dict],
    date_text: str,
    message_id: str | None,
    *,
    subject_prefix: str = "",
    recipients: list[str] | None = None,
    attach_files_override: bool | None = None,
) -> EmailMessage:
    if recipients is not None:
        recipients = list(dict.fromkeys(validate_email(address) for address in recipients))
        if not recipients or len(recipients) > 20:
            raise ValueError("BCC 收件人数必须为 1–20")
    files, total = _attachment_candidates(notices)
    attach_files = total <= settings.max_attachment_bytes
    if attach_files_override is False:
        attach_files = False
    message = EmailMessage()
    message["From"] = settings.qq_email
    message["To"] = "undisclosed-recipients:;" if recipients is not None else settings.my_email
    message["Subject"] = (
        f"{subject_prefix}#【学校通知】—【{date_text}】学校通知日报（共 {len(notices)} 条）"
    )
    message["Message-ID"] = message_id or make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    unsubscribe_url = _get_unsubscribe_url()
    message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    plain_sections = []
    for item in notices:
        attachment_text = ""
        if item.get("attachments"):
            rows = []
            for attachment in item["attachments"]:
                path = Path(attachment.get("path", ""))
                attached = bool(attach_files and attachment.get("path") and path.is_file())
                suffix = (
                    "（已附在邮件中）"
                    if attached
                    else f"（下载：{attachment['url']}）"
                    if attachment.get("url")
                    else "（未能取得下载链接）"
                )
                rows.append(f"- {_attachment_name(attachment)} {suffix}")
            names = "\n".join(rows)
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
            with item["path_obj"].open("rb") as handle:
                payload = handle.read(settings.max_attachment_bytes + 1)
            if len(payload) != item["actual_size"] or len(payload) > settings.max_attachment_bytes:
                raise ValueError("构建邮件期间附件大小变化，未发送邮件")
            message.add_attachment(
                payload,
                maintype=maintype,
                subtype=subtype,
                filename=safe_name,
            )

    return message


def send_digest(notices, date_text, message_id, *, subject_prefix="", recipients=None) -> dict:
    if recipients is not None:
        recipients = list(dict.fromkeys(validate_email(address) for address in recipients))
        if not recipients or len(recipients) > 20:
            raise ValueError("BCC 收件人数必须为 1–20")
    budget = getattr(settings, "max_message_bytes", 20 * 1024 * 1024)
    if budget <= 0:
        raise ValueError("邮件大小预算必须大于零")
    message = _build_digest(
        notices, date_text, message_id, subject_prefix=subject_prefix, recipients=recipients
    )
    if len(message.as_bytes(policy=SMTP)) > budget and list(message.iter_attachments()):
        message = _build_digest(
            notices,
            date_text,
            message_id,
            subject_prefix=subject_prefix,
            recipients=recipients,
            attach_files_override=False,
        )
    if len(message.as_bytes(policy=SMTP)) > budget:
        raise ValueError("仅链接邮件仍超过 MIME 大小预算，未连接 SMTP；请减少批次通知数量")
    return _send_message(message) if recipients is None else _send_message(message, recipients)


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
