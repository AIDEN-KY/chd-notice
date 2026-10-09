from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from playwright.async_api import BrowserContext, ElementHandle, Page, async_playwright

from config import settings
from db import upsert_notice

log = logging.getLogger(__name__)


class SessionExpiredError(RuntimeError):
    pass


async def _first_text(root: ElementHandle, selectors: list[str]) -> str:
    for selector in selectors:
        node = await root.query_selector(selector)
        if node:
            text = (await node.inner_text()).strip()
            if text:
                return re.sub(r"\s+", " ", text)
    return ""


async def _first_href(root: ElementHandle, selectors: list[str], base_url: str) -> str:
    for selector in selectors:
        node = await root.query_selector(selector)
        if node:
            href = await node.get_attribute("href")
            if href and not href.lower().startswith(("javascript:", "#")):
                return urljoin(base_url, href)
    return ""


async def _matching_selector(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        if await page.locator(selector).count():
            return selector
    return None


async def _is_login_page(page: Page) -> bool:
    parsed = urlparse(page.url)
    if parsed.netloc == "ids.chd.edu.cn" or "/authserver/login" in parsed.path:
        return True
    return await page.locator("input[name='username'], #username").count() > 0


def _safe_filename(name: str, url: str) -> str:
    name = unquote(name).strip() or Path(urlparse(url).path).name or "attachment"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:160]
    suffix = hashlib.sha256(url.encode()).hexdigest()[:10]
    path = Path(name)
    return f"{path.stem}_{suffix}{path.suffix}"


async def _download_attachment(context: BrowserContext, url: str, name: str) -> dict:
    item = {"name": name, "url": url, "path": "", "size": 0}
    try:
        response = await context.request.get(url, timeout=settings.action_timeout_ms)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}")
        body = await response.body()
        settings.attachment_dir.mkdir(parents=True, exist_ok=True)
        target = settings.attachment_dir / _safe_filename(name, url)
        target.write_bytes(body)
        item.update(path=str(target.resolve()), size=len(body))
    except Exception as exc:
        log.warning("附件下载失败，将仅保留链接：%s (%s)", url, exc)
    return item


async def _scrape_detail(context: BrowserContext, url: str) -> tuple[str, list[dict]]:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="commit", timeout=settings.navigation_timeout_ms)
        await page.wait_for_timeout(settings.spa_wait_ms)
        if await _is_login_page(page):
            raise SessionExpiredError("会话已过期，请重新手动登录")
        content_selector = await _matching_selector(page, settings.detail_content_selectors)
        content_html = await page.locator(content_selector).first.inner_html() if content_selector else ""
        seen: set[str] = set()
        attachments: list[dict] = []
        for selector in settings.attachment_selectors:
            for link in await page.locator(selector).all():
                href = await link.get_attribute("href")
                if not href:
                    continue
                absolute = urljoin(page.url, href)
                if absolute in seen:
                    continue
                seen.add(absolute)
                name = (await link.inner_text()).strip() or Path(urlparse(absolute).path).name
                attachments.append(await _download_attachment(context, absolute, name))
        return content_html, attachments
    finally:
        await page.close()


async def _extract_list_page(page: Page) -> list[dict]:
    selector = await _matching_selector(page, settings.list_item_selectors)
    if not selector:
        raise RuntimeError("未找到通知列表项，请按 README 调整 LIST_ITEM_SELECTORS")

    # 1) 先读取所有条目的静态信息（标题/时间/栏目）
    items = await page.query_selector_all(selector)
    raw: list[dict] = []
    for item in items:
        title = await _first_text(item, settings.title_selectors)
        if not title:
            continue
        raw.append({
            "title": title,
            "publish_time": await _first_text(item, settings.time_selectors),
            "department": await _first_text(item, settings.department_selectors),
        })

    if not raw:
        raise RuntimeError("找到了列表容器，但未提取到有效通知标题")

    # 2) 本系统列表项是 <div>，没有 <a href>，必须逐条点击抓 hash 路由 URL
    result: list[dict] = []
    for i, info in enumerate(raw):
        log.info("正在解析详情链接 %d/%d：%s", i + 1, len(raw), info["title"])
        target = page.locator(selector).nth(i)
        await target.scroll_into_view_if_needed()
        await target.click()
        await page.wait_for_timeout(2000)
        info["detail_url"] = page.url
        result.append(info)
        await page.go_back()
        # 等列表重新渲染出来
        try:
            await page.locator("#readClass").first.wait_for(timeout=10000)
        except Exception:
            await page.wait_for_timeout(3000)

    return result



async def _go_next(page: Page, page_number: int) -> bool:
    # 优先点击明确页码，适配常见 jQuery/Element UI 分页器。
    candidates = [
        f".pagination a:text-is('{page_number}')",
        f".el-pager li:text-is('{page_number}')",
        f"[class*='page'] a:text-is('{page_number}')",
    ]
    for selector in [*candidates, *settings.next_page_selectors]:
        locator = page.locator(selector).first
        if await locator.count() and await locator.is_visible():
            old = await page.locator("body").inner_text()
            await locator.click()
            try:
                await page.wait_for_function(
                    "oldText => document.body.innerText !== oldText", old,
                    timeout=settings.action_timeout_ms,
                )
            except Exception:
                await page.wait_for_timeout(settings.spa_wait_ms)
            return True
    return False


async def scrape_notices(headless: bool = True) -> int:
    if not settings.auth_file.exists():
        raise SessionExpiredError("未找到 auth.json，请先运行 python main.py init-auth")
    added = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=settings.auth_file)
        page = await context.new_page()
        try:
            log.info("正在打开通知列表")
            await page.goto(
                settings.notices_url,
                wait_until="commit",
                timeout=settings.navigation_timeout_ms,
            )
            await page.wait_for_timeout(settings.spa_wait_ms)
            try:

                await page.locator("#readClass").first.wait_for(timeout=15000)
            except Exception:
                pass

            if await _is_login_page(page):
                raise SessionExpiredError("会话已过期，请重新手动登录")
            log.info("通知列表已加载：%s", page.url)
            seen: set[str] = set()
            for page_number in range(1, settings.pages_to_scrape + 1):
                log.info("开始处理列表第 %d/%d 页", page_number, settings.pages_to_scrape)
                summaries = await _extract_list_page(page)
                for index, notice in enumerate(summaries, start=1):
                    if notice["detail_url"] in seen:
                        continue
                    seen.add(notice["detail_url"])
                    log.info(
                        "正在抓取第 %d 页详情 %d/%d：%s",
                        page_number, index, len(summaries), notice["title"],
                    )
                    content, attachments = await _scrape_detail(context, notice["detail_url"])
                    notice.update(content_html=content, attachments=attachments)
                    if upsert_notice(notice):
                        added += 1
                        log.info("新增通知：%s", notice["title"])
                if page_number < settings.pages_to_scrape and not await _go_next(page, page_number + 1):
                    log.warning("无法进入第 %d 页，提前结束", page_number + 1)
                    break
            await context.storage_state(path=settings.auth_file)
        finally:
            await context.close()
            await browser.close()
    return added


async def initialize_auth() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(
            settings.notices_url,
            wait_until="commit",
            timeout=settings.navigation_timeout_ms,
        )
        print("请在打开的浏览器中完成 CAS 登录，并确认已进入通知页面。")
        await asyncio.to_thread(input, "完成后回到终端按 Enter 保存会话：")
        if await _is_login_page(page):
            await context.close()
            await browser.close()
            raise RuntimeError("仍停留在登录页，未保存会话")
        await context.storage_state(path=settings.auth_file)
        await context.close()
        await browser.close()
        print(f"会话已保存到 {settings.auth_file}")
