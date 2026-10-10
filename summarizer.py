from __future__ import annotations

import asyncio
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
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(1, settings.retry_attempts + 1):
            try:
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
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status != 429 and status < 500:
                    log.warning("DeepSeek 请求被拒绝（HTTP %d），降级为标题", status)
                    return title
                error: Exception = exc
            except Exception as exc:
                error = exc

            if attempt < settings.retry_attempts:
                delay = settings.retry_backoff_seconds * attempt
                log.warning(
                    "DeepSeek 摘要失败（第 %d/%d 次）：%s；%d 秒后重试",
                    attempt,
                    settings.retry_attempts,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                log.warning("DeepSeek 摘要失败，降级为标题：%s", error)
    return title
