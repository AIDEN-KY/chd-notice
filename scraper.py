from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, ElementHandle, Page, async_playwright

from config import settings
from db import get_notice_hash, save_notice

log = logging.getLogger(__name__)


class SessionExpiredError(RuntimeError):
    pass


@dataclass
class ScrapeStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    baselined: int = 0
    pages: int = 0

    @property
    def changed(self) -> int:
        return self.new + self.updated


def evaluate_pagination_page(
    urls: tuple[str, ...],
    seen_pages: set[tuple[str, ...]],
    consecutive_known_pages: int,
    page_all_known: bool,
    known_pages_to_stop: int,
) -> tuple[str | None, int]:
    """返回停止原因，并更新连续已知页计数。"""
    if urls in seen_pages:
        return "repeated", consecutive_known_pages
    seen_pages.add(urls)
    consecutive = consecutive_known_pages + 1 if page_all_known else 0
    if consecutive >= known_pages_to_stop:
        return "known-pages", consecutive
    return None, consecutive


def notice_content_hash(notice: dict) -> str:
    """对用户可见文本和附件元数据生成稳定哈希，忽略 HTML 样式变化。"""
    content_text = BeautifulSoup(
        notice.get("content_html", ""), "html.parser"
    ).get_text(" ")

    def normalized(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    attachments = sorted(
        (
            {
                "name": normalized(item.get("name")),
                "url": normalized(item.get("url")),
            }
            for item in notice.get("attachments", [])
        ),
        key=lambda item: (item["url"], item["name"]),
    )
    payload = {
        "title": normalized(notice.get("title")),
        "publish_time": normalized(notice.get("publish_time")),
        "department": normalized(notice.get("department")),
        "content": normalized(content_text),
        "attachments": attachments,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _goto_with_retry(page: Page, url: str) -> None:
    """门户偶发超时；仅对幂等的页面导航做有限重试。"""
    for attempt in range(1, settings.retry_attempts + 1):
        try:
            await page.goto(
                url,
                wait_until="commit",
                timeout=settings.navigation_timeout_ms,
            )
            return
        except Exception as exc:
            if attempt == settings.retry_attempts:
                raise
            delay = settings.retry_backoff_seconds * attempt
            log.warning(
                "页面打开失败（第 %d/%d 次）：%s；%d 秒后重试",
                attempt, settings.retry_attempts, exc, delay,
            )
            await page.wait_for_timeout(delay * 1000)


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
        await _goto_with_retry(page, url)
        await page.wait_for_timeout(settings.spa_wait_ms)
        if await _is_login_page(page):
            raise SessionExpiredError("会话已过期，请重新手动登录")
        content_selector = await _matching_selector(page, settings.detail_content_selectors)
        if not content_selector:
            raise RuntimeError(f"详情页未找到正文区域：{url}")
        content_html = await page.locator(content_selector).first.inner_html()
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
                attachments.append(
                    {"name": name, "url": absolute, "path": "", "size": 0}
                )
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
    # 优先点击明确页码，最后再使用“文本恰好等于页码”的通用候选。
    selectors = [
        f".pagination a:text-is('{page_number}')",
        f".el-pager li:text-is('{page_number}')",
        f".bh-pagination li:text-is('{page_number}')",
        f".bh-pager li:text-is('{page_number}')",
        f"[class*='page'] a:text-is('{page_number}')",
    ]
    candidates = [page.locator(selector).first for selector in selectors]
    candidates.append(
        page.locator("a, button, li, span")
        .filter(has_text=re.compile(rf"^\s*{page_number}\s*$"))
        .first
    )
    candidates.extend(page.locator(selector).first for selector in settings.next_page_selectors)

    list_selector = await _matching_selector(page, settings.list_item_selectors)
    if not list_selector:
        return False
    old_items = await page.locator(list_selector).all_inner_texts()
    attempted = 0
    for locator in candidates:
        if not await locator.count() or not await locator.is_visible():
            continue
        attempted += 1
        try:
            await locator.click()
            await page.wait_for_timeout(settings.spa_wait_ms)
        except Exception as exc:
            log.warning("分页候选点击失败：%s", exc)
            continue
        new_items = await page.locator(list_selector).all_inner_texts()
        if new_items != old_items:
            log.info("已进入列表第 %d 页", page_number)
            return True

    page_number_nodes = page.locator("a, button, li, span").filter(
        has_text=re.compile(rf"^\s*{page_number}\s*$")
    )
    diagnostics = []
    for index in range(min(await page_number_nodes.count(), 5)):
        node = page_number_nodes.nth(index)
        diagnostics.append(
            await node.evaluate(
                "el => ({tag: el.tagName, className: el.className, "
                "title: el.getAttribute('title'), aria: el.getAttribute('aria-label')})"
            )
        )
    log.warning(
        "翻页失败：尝试 %d 个可见候选；页码节点诊断=%s",
        attempted,
        diagnostics,
    )
    return False


async def scrape_notices(headless: bool = True) -> ScrapeStats:
    if not settings.auth_file.exists():
        raise SessionExpiredError("未找到 auth.json，请先运行 python main.py init-auth")
    stats = ScrapeStats()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=settings.auth_file)
        page = await context.new_page()
        try:
            log.info("正在打开通知列表")
            await _goto_with_retry(page, settings.notices_url)
            await page.wait_for_timeout(settings.spa_wait_ms)
            try:

                await page.locator("#readClass").first.wait_for(timeout=15000)
            except Exception:
                pass

            if await _is_login_page(page):
                raise SessionExpiredError("会话已过期，请重新手动登录")
            log.info("通知列表已加载：%s", page.url)
            seen: set[str] = set()
            page_fingerprints: set[tuple[str, ...]] = set()
            consecutive_known_pages = 0
            for page_number in range(1, settings.max_pages_to_scrape + 1):
                stats.pages = page_number
                log.info(
                    "开始处理列表第 %d 页（安全上限 %d 页）",
                    page_number,
                    settings.max_pages_to_scrape,
                )
                summaries = await _extract_list_page(page)
                fingerprint = tuple(item["detail_url"] for item in summaries)
                old_hashes = {
                    item["detail_url"]: get_notice_hash(item["detail_url"])
                    for item in summaries
                }
                page_all_known = all(value is not None for value in old_hashes.values())
                stop_reason, consecutive_known_pages = evaluate_pagination_page(
                    fingerprint,
                    page_fingerprints,
                    consecutive_known_pages,
                    page_all_known,
                    settings.known_pages_to_stop,
                )
                if stop_reason == "repeated":
                    log.warning("分页后列表内容重复，为避免循环已停止抓取")
                    break
                for index, notice in enumerate(summaries, start=1):
                    if notice["detail_url"] in seen:
                        continue
                    seen.add(notice["detail_url"])
                    old_hash = old_hashes[notice["detail_url"]]
                    log.info(
                        "正在检查第 %d 页详情 %d/%d：%s",
                        page_number, index, len(summaries), notice["title"],
                    )
                    content, attachments = await _scrape_detail(context, notice["detail_url"])
                    notice.update(content_html=content, attachments=attachments)
                    content_hash = notice_content_hash(notice)
                    if old_hash == content_hash:
                        stats.unchanged += 1
                        log.info("内容未变化：%s", notice["title"])
                        continue

                    notice["attachments"] = [
                        await _download_attachment(context, item["url"], item["name"])
                        for item in attachments
                    ]
                    status = save_notice(notice, content_hash)
                    if status == "new":
                        stats.new += 1
                        log.info("新增通知：%s", notice["title"])
                    elif status == "updated":
                        stats.updated += 1
                        log.info("通知内容有更新，已重新加入待发队列：%s", notice["title"])
                    elif status == "baseline":
                        stats.baselined += 1
                        log.info("已为旧通知建立内容哈希基线：%s", notice["title"])
                    else:
                        stats.unchanged += 1

                if stop_reason == "known-pages":
                    log.info(
                        "已连续处理 %d 个全部已知页面，安全停止翻页",
                        consecutive_known_pages,
                    )
                    break
                if (
                    page_number < settings.max_pages_to_scrape
                    and not await _go_next(page, page_number + 1)
                ):
                    log.warning("无法进入第 %d 页，提前结束", page_number + 1)
                    break
            await context.storage_state(path=settings.auth_file)
        finally:
            await context.close()
            await browser.close()
    return stats


async def initialize_auth() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await _goto_with_retry(page, settings.notices_url)
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
