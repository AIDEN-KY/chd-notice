from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from config import settings

log = logging.getLogger(__name__)


def html_to_text(value: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(value or "", "html.parser").get_text(" ")).strip()


async def summarize(title: str, content_html: str) -> str:
    text = html_to_text(content_html)
    if not text:
        return title
    prompt = "请用 50-80 字总结以下通知的核心信息，面向大学生，语言简洁。\n\n" + text[:12_000]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                settings.deepseek_url,
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={
                    "model": settings.deepseek_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 180,
                },
            )
            response.raise_for_status()
            summary = response.json()["choices"][0]["message"]["content"].strip()
            return summary or title
    except Exception as exc:
        log.warning("DeepSeek 摘要失败，降级为标题：%s", exc)
        return title
