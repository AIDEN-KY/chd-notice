from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Response,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from config import settings
from db import (
    finish_scrape_run,
    get_existing_notice_urls,
    get_history_scan_cursor,
    get_notice_by_url,
    get_notice_hash,
    get_pending_scrape_failures,
    init_db,
    record_scan_page,
    record_scrape_failure,
    resolve_scrape_failure,
    save_notice,
    start_scrape_run,
    update_notice_hash_baseline,
)

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
    failed: int = 0

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


def notice_content_hash(
    notice: dict, *, include_attachment_hash: bool = True, include_body_resources: bool = True
) -> str:
    """对用户可见文本和附件元数据生成稳定哈希，忽略 HTML 样式变化。"""
    soup = BeautifulSoup(notice.get("content_html", ""), "html.parser")
    content_text = soup.get_text(" ")

    def normalized(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    attachments = sorted(
        (
            {
                "name": normalized(item.get("name")),
                "url": normalized(item.get("url")),
                **(
                    {"sha256": item["sha256"]}
                    if include_attachment_hash and item.get("sha256")
                    else {}
                ),
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
    if include_body_resources:
        base = notice.get("detail_url", "")

        def target(value):
            return urljoin(base, str(value or "").strip())

        def srcset(value):
            raw = str(value or "").strip()
            # data URL 的逗号不是候选分隔符：保守保留原值，不尝试下载资源。
            if "data:" in raw:
                return [("opaque", raw)]
            candidates = []
            for candidate in raw.split(","):
                parts = candidate.strip().split()
                if parts:
                    candidates.append((target(parts[0]), " ".join(parts[1:])))
            return candidates

        resources = []
        for node in soup.select("a[href], img, picture source[srcset]"):
            if node.name == "a":
                resources.append({"kind": "link", "target": target(node.get("href"))})
            else:
                resources.append(
                    {
                        "kind": node.name,
                        "src": target(node.get("src")) if node.get("src") else "",
                        "srcset": srcset(node.get("srcset")),
                        "alt": normalized(node.get("alt")),
                    }
                )
        if resources:
            payload["resources"] = resources
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _notice_candidates(payload: object, limit: int = 50) -> list[dict]:
    """从列表接口 JSON 中提取可能包含标题和 dm 的标量对象，不保存整份响应。"""
    results: list[dict] = []

    def walk(value: object, path: str) -> None:
        if len(results) >= limit:
            return
        if isinstance(value, dict):
            scalar = {
                str(key): item[:500] if isinstance(item, str) else item
                for key, item in value.items()
                if item is None or isinstance(item, (str, int, float, bool))
            }
            lower_keys = {key.lower() for key in scalar}
            text_values = " ".join(item for item in scalar.values() if isinstance(item, str))
            has_identifier = any(key == "dm" or key.endswith("dm") for key in lower_keys)
            has_title = (
                bool(lower_keys & {"bt", "title", "ggbt", "tzbt", "mc", "ggmc"})
                or "通知" in text_values
            )
            if scalar and has_identifier and has_title:
                results.append({"path": path, "fields": scalar})
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "$")
    return results


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
                attempt,
                settings.retry_attempts,
                exc,
                delay,
            )
            await page.wait_for_timeout(delay * 1000)


async def _first_text(root: Locator, selectors: list[str]) -> str:
    for selector in selectors:
        node = root.locator(selector).first
        if await node.count():
            text = (await node.inner_text()).strip()
            if text:
                return re.sub(r"\s+", " ", text)
    return ""


async def _row_title(root: Locator) -> str:
    """提取列表标题；已读状态导致标题标记变化时退回行文本首行。"""
    title = await _first_text(root, settings.title_selectors)
    if title:
        return title
    row_text = await root.inner_text()
    for line in row_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            return normalized
    return ""


async def _matching_selector(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        if await page.locator(selector).count():
            return selector
    return None


async def _wait_for_list(page: Page, timeout_ms: int = 30_000) -> str | None:
    """等待 SPA 至少渲染出一种配置中的列表选择器。"""
    try:
        await page.wait_for_function(
            "selectors => selectors.some(selector => document.querySelector(selector))",
            arg=settings.list_item_selectors,
            timeout=timeout_ms,
        )
    except Exception:
        # 元素可能恰好在超时边界完成渲染，最后再同步检查一次，避免误报警。
        return await _matching_selector(page, settings.list_item_selectors)
    return await _matching_selector(page, settings.list_item_selectors)


async def _open_notice_list(page: Page) -> str:
    """打开通知列表，并对“导航成功但 SPA 空白”的情况做有限重试。"""
    for attempt in range(1, settings.retry_attempts + 1):
        await _goto_with_retry(page, settings.notices_url)
        await page.wait_for_timeout(settings.spa_wait_ms)
        if await _is_login_page(page):
            raise SessionExpiredError("会话已过期，请重新手动登录")

        list_selector = await _wait_for_list(
            page,
            timeout_ms=settings.spa_load_timeout_ms,
        )
        if list_selector:
            return list_selector
        if attempt == settings.retry_attempts:
            break

        delay = settings.retry_backoff_seconds * attempt
        log.warning(
            "SPA 未渲染通知列表（第 %d/%d 次）；%d 秒后重新进入页面",
            attempt,
            settings.retry_attempts,
            delay,
        )
        await page.goto(
            "about:blank",
            wait_until="commit",
            timeout=settings.action_timeout_ms,
        )
        await page.wait_for_timeout(delay * 1000)

    raise RuntimeError(
        f"SPA 连续 {settings.retry_attempts} 次未渲染通知列表；请检查网络、CAS 会话或列表选择器"
    )


async def _scroll_list_containers(page: Page, list_selector: str) -> list[dict]:
    """滚动已配置容器及列表祖先，兼容门户的无限滚动/懒加载。"""
    return await page.locator(list_selector).first.evaluate(
        """
        (firstItem, configuredSelectors) => {
          const targets = [];
          for (const selector of configuredSelectors) {
            const target = document.querySelector(selector);
            if (target && target.scrollHeight > target.clientHeight + 10 &&
                !targets.includes(target)) targets.push(target);
          }
          let current = firstItem.parentElement;
          while (current) {
            if (current.scrollHeight > current.clientHeight + 10 &&
                !targets.includes(current)) targets.push(current);
            current = current.parentElement;
          }
          const scrollingElement = document.scrollingElement;
          if (scrollingElement && !targets.includes(scrollingElement)) {
            targets.push(scrollingElement);
          }
          for (const target of targets) target.scrollTop = target.scrollHeight;
          window.scrollTo(0, document.body.scrollHeight);
          return targets.map((target) => ({
            tag: target.tagName,
            id: target.id || '',
            className: typeof target.className === 'string' ? target.className : '',
          }));
        }
        """,
        settings.scroll_container_selectors,
    )


async def _restore_list_count(
    page: Page,
    list_selector: str,
    expected_count: int,
) -> bool:
    """从详情返回后恢复此前已加载的列表批次。"""
    deadline = asyncio.get_running_loop().time() + settings.action_timeout_ms / 1000
    while True:
        current_count = await page.locator(list_selector).count()
        if current_count >= expected_count:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await _scroll_list_containers(page, list_selector)
        await page.wait_for_timeout(min(settings.spa_wait_ms, 1_000))


async def _first_page_text(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count():
            text = re.sub(r"\s+", " ", (await locator.inner_text()).strip())
            if text:
                return text
    return ""


async def _list_item_by_title(page: Page, item_selector: str, title: str):
    """按标题跨滚动批次重定位，兼容已读后重新排序。"""
    seen_fingerprints: set[tuple[str, ...]] = set()
    last_titles: list[str] = []
    for _ in range(settings.max_pages_to_scrape):
        items = page.locator(item_selector)
        current_titles = []
        for index in range(await items.count()):
            item = items.nth(index)
            item_title = await _row_title(item)
            current_titles.append(item_title)
            if item_title == title:
                return item

        fingerprint = tuple(current_titles)
        if fingerprint in seen_fingerprints:
            break
        seen_fingerprints.add(fingerprint)
        last_titles = current_titles
        if not current_titles:
            break
        await _scroll_list_containers(page, item_selector)
        await page.wait_for_timeout(settings.spa_wait_ms)

    preview = "、".join(item for item in last_titles[:3] if item)
    raise RuntimeError(f"滚动搜索后仍未找到通知：{title}；当前批次示例：{preview or '空'}")


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


def _validate_attachment_url(url: str) -> None:
    parsed = urlparse(url)
    login = urlparse(settings.login_url)
    if parsed.hostname == login.hostname or parsed.path.rstrip("/") == login.path.rstrip("/"):
        raise SessionExpiredError("附件目标为登录页，会话可能已过期")
    allowed = getattr(settings, "attachment_allowed_hosts", ("portal.chd.edu.cn",))
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("附件不允许直接使用 IP 地址目标")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("附件目标不在 HTTPS 可信主机范围内")


def _looks_like_cas_login(prefix: bytes) -> bool:
    text = prefix.decode("utf-8", errors="ignore").lower()
    return (
        "<form" in text
        and "password" in text
        and any(marker in text for marker in ("authserver", "cas login", "统一身份认证"))
    )


def _attachment_download_url(url: str) -> str:
    """只解包学校已知 PDF 预览路由，校验两端且不访问预览页面。"""
    _validate_attachment_url(url)
    parsed = urlparse(url)
    host = getattr(settings, "pdf_preview_host", "portal.chd.edu.cn")
    preview_path = getattr(settings, "pdf_preview_path", "/qljfwapp/sys/emapcomponent/pdf/index.do")
    if parsed.hostname != host or parsed.path != preview_path:
        return url
    values = parse_qs(parsed.query, keep_blank_values=True).get("file", [])
    if len(values) != 1 or not values[0].strip():
        raise ValueError("PDF 预览链接必须包含单个非空 file 参数")
    source = values[0]
    if any(character in source for character in ("\\", "\r", "\n", "\t")):
        raise ValueError("PDF 下载地址包含非法字符")
    # parse_qs 已解码一次，不重复 unquote，不把任意 file 参数当作可信 URL。
    target = urljoin(url, source)
    _validate_attachment_url(target)
    download = urlparse(target)
    prefix = getattr(
        settings,
        "attachment_download_path_prefix",
        "/qljfwapp/sys/emapcomponent/file/getAttachmentFile/",
    )
    if (
        download.hostname != parsed.hostname
        or download.fragment
        or not re.fullmatch(re.escape(prefix) + r"[0-9a-fA-F]+\.do", download.path)
    ):
        raise ValueError("PDF 预览参数不是同站学校附件下载地址")
    return target


async def _download_attachment(context: BrowserContext, url: str, name: str) -> dict:
    """独立流式传输：复用浏览器匹配 URL 的 Cookie，不使用已缓冲的 APIResponse.body。"""
    item = {"name": name, "url": url, "path": "", "size": 0}
    temporary_path = None
    try:
        limit = getattr(settings, "max_single_attachment_bytes", 20 * 1024 * 1024)
        if limit <= 0:
            raise ValueError("单文件大小预算必须大于零")
        current = _attachment_download_url(url)
        redirects = getattr(settings, "max_attachment_redirects", 3)
        _validate_attachment_url(current)
        async with httpx.AsyncClient(
            timeout=settings.action_timeout_ms / 1000, follow_redirects=False, trust_env=False
        ) as client:
            for hop in range(redirects + 1):
                _validate_attachment_url(current)
                cookies = await context.cookies([current])
                # 每跳只发送 Playwright 为当前地址选择的 Cookie，不跨主机沿用。
                client.cookies.clear()
                for cookie in cookies:
                    client.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=cookie["domain"],
                        path=cookie["path"],
                    )
                async with client.stream(
                    "GET", current, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if hop == redirects or not response.headers.get("location"):
                            raise ValueError("附件重定向超限或缺少目标")
                        current = urljoin(current, response.headers["location"])
                        _validate_attachment_url(current)
                        continue
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").lower() not in {
                        "identity",
                        "",
                    }:
                        raise ValueError("附件响应忽略 identity 编码要求，不能保证解压内存预算")
                    _validate_attachment_url(str(response.url))
                    login = urlparse(settings.login_url)
                    if urlparse(str(response.url)).path.rstrip("/") == login.path.rstrip("/"):
                        raise SessionExpiredError("附件返回登录页")
                    length = response.headers.get("content-length")
                    if length is not None and (int(length) < 0 or int(length) > limit):
                        raise ValueError("附件声明大小超过预算")
                    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
                    hasher = hashlib.sha256()
                    size = 0
                    prefix = bytearray()
                    with tempfile.NamedTemporaryFile(
                        dir=settings.attachment_dir,
                        prefix=".attachment-",
                        suffix=".tmp",
                        delete=False,
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise ValueError("附件实际大小超过预算")
                            prefix.extend(chunk[: max(0, 64 * 1024 - len(prefix))])
                            hasher.update(chunk)
                            temporary.write(chunk)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    if _looks_like_cas_login(bytes(prefix)):
                        raise SessionExpiredError("附件内容为 CAS 登录表单")
                    content_hash = hasher.hexdigest()
                    filename = Path(_safe_filename(name, url))
                    target = (
                        settings.attachment_dir / f"{filename.stem}_{content_hash}{filename.suffix}"
                    )
                    os.replace(temporary_path, target)
                    item.update(path=str(target.resolve()), size=size, sha256=content_hash)
                    break
    except Exception as exc:
        item["error_type"] = type(exc).__name__
        log.warning("附件下载失败，仅保留原链接；错误类型=%s", type(exc).__name__)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return item


async def _scrape_detail(context: BrowserContext, url: str) -> tuple[str, list[dict]]:
    page = await context.new_page()
    try:
        await _goto_with_retry(page, url)
        await page.wait_for_timeout(settings.spa_wait_ms)
        if await _is_login_page(page):
            raise SessionExpiredError("会话已过期，请重新手动登录")
        try:
            await page.wait_for_function(
                "selectors => selectors.some(selector => document.querySelector(selector))",
                arg=settings.detail_content_selectors,
                timeout=settings.spa_load_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            if await _is_login_page(page):
                raise SessionExpiredError("会话已过期，请重新手动登录") from exc
            raise RuntimeError(
                f"详情页正文区域在 {settings.spa_load_timeout_ms / 1000:g} 秒内未渲染：{url}"
            ) from exc
        content_selector = await _matching_selector(page, settings.detail_content_selectors)
        if not content_selector:
            raise RuntimeError(f"详情页未找到正文区域：{url}")
        # SPA 可能先创建空容器，再异步填入正文或附件，不能把空壳存成新版本。
        await page.wait_for_function(
            """selectors => {
                const body = document.querySelector(selectors.content);
                return Boolean(body && body.innerHTML.trim()) ||
                    selectors.attachments.some(selector =>
                        [...document.querySelectorAll(selector)].some(link =>
                            link.getAttribute('href')));
            }""",
            arg={"content": content_selector, "attachments": settings.attachment_selectors},
            timeout=settings.action_timeout_ms,
        )
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
                attachments.append({"name": name, "url": absolute, "path": "", "size": 0})
        return content_html, attachments
    finally:
        await page.close()


async def _extract_list_page(page: Page) -> list[dict]:
    selector = await _matching_selector(page, settings.list_item_selectors)
    if not selector:
        raise RuntimeError("未找到通知列表项，请按 README 调整 LIST_ITEM_SELECTORS")

    # 1) 先读取所有条目的静态信息（标题/时间/栏目）
    items = page.locator(selector)
    raw: list[dict] = []
    for index in range(await items.count()):
        item = items.nth(index)
        title = await _row_title(item)
        if not title:
            continue
        raw.append(
            {
                "title": title,
                "publish_time": await _first_text(item, settings.time_selectors),
                "department": await _first_text(item, settings.department_selectors),
            }
        )

    if not raw:
        raise RuntimeError("找到了列表容器，但未提取到有效通知标题")

    # 2) 本系统列表项是 <div>，没有 <a href>，必须逐条点击抓 hash 路由 URL
    result: list[dict] = []
    for i, info in enumerate(raw):
        log.info("正在解析详情链接 %d/%d：%s", i + 1, len(raw), info["title"])
        target = await _list_item_by_title(page, selector, info["title"])
        await target.scroll_into_view_if_needed()
        list_url = page.url
        await target.click()
        try:
            await page.wait_for_function(
                "url => window.location.href !== url",
                arg=list_url,
                timeout=settings.action_timeout_ms,
            )
        except Exception:
            await page.wait_for_timeout(settings.spa_wait_ms)
        if page.url == list_url:
            raise RuntimeError(f"点击通知后详情 URL 未变化：{info['title']}")
        await page.wait_for_timeout(settings.spa_wait_ms)
        detail_title = await _first_page_text(page, settings.title_selectors)
        if detail_title and detail_title != info["title"]:
            raise RuntimeError(f"详情标题校验失败：期望“{info['title']}”，实际“{detail_title}”")
        info["detail_url"] = page.url
        result.append(info)
        await page.go_back()
        # 等列表重新渲染，并恢复此前已加载的完整批次；只等第一行会导致后续行缺失。
        try:
            await page.locator(selector).first.wait_for(timeout=settings.action_timeout_ms)
        except Exception:
            await page.wait_for_timeout(settings.spa_wait_ms)
        if not await _restore_list_count(page, selector, len(raw)):
            restored_count = await page.locator(selector).count()
            raise RuntimeError(
                f"返回列表后仅恢复 {restored_count}/{len(raw)} 条通知，无法安全继续匹配详情"
            )

    return result


async def _go_next(page: Page, page_number: int) -> bool:
    # 优先点击明确页码/下一页，最后再尝试无限滚动或虚拟列表加载。
    selectors = [
        f".pagination a:text-is('{page_number}')",
        f".el-pager li:text-is('{page_number}')",
        f".bh-pagination li:text-is('{page_number}')",
        f".bh-pager li:text-is('{page_number}')",
        f"[class*='page'] a:text-is('{page_number}')",
        "button:text-is('下一页')",
        "a:text-is('下一页')",
        "li:text-is('下一页')",
        "[aria-label*='next' i]",
        "[aria-label*='下一页']",
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

    # 部分 SPA 不显示分页按钮，而是在窗口或列表祖先容器滚到底时加载下一批。
    scroll_result = await _scroll_list_containers(page, list_selector)
    try:
        await page.wait_for_function(
            """
            ({selector, oldItems}) => {
              const current = [...document.querySelectorAll(selector)]
                .map((item) => item.innerText);
              return JSON.stringify(current) !== JSON.stringify(oldItems);
            }
            """,
            arg={"selector": list_selector, "oldItems": old_items},
            timeout=settings.action_timeout_ms,
        )
        log.info("滚动加载了列表第 %d 批", page_number)
        return True
    except Exception:
        log.info("滚动后列表未变化：%s", scroll_result)

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


async def _collect_pagination_diagnostics(page: Page) -> dict:
    list_selector = await _matching_selector(page, settings.list_item_selectors)
    before_count = await page.locator(list_selector).count() if list_selector else 0
    await page.evaluate(
        """
        () => {
          window.scrollTo(0, document.body?.scrollHeight || 0);
          for (const el of document.querySelectorAll('*')) {
            if (el.scrollHeight > el.clientHeight + 10) {
              el.scrollTop = el.scrollHeight;
            }
          }
        }
        """
    )
    await page.wait_for_timeout(settings.spa_wait_ms)
    after_count = await page.locator(list_selector).count() if list_selector else 0
    diagnostics = await page.evaluate(
        """
        ({beforeCount, afterCount, configuredListSelectors, matchedListSelector}) => {
          const visible = (el) => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0;
          };
          const pattern = /(page|pager|pagination|next|more|load|下一|更多|加载)/i;
          const candidates = [...document.querySelectorAll('*')]
            .filter((el) => {
              const text = (el.innerText || '').trim();
              const identity = [el.id, el.className, el.getAttribute('title'),
                el.getAttribute('aria-label')].filter(Boolean).join(' ');
              return visible(el) && (pattern.test(identity) ||
                (text.length <= 30 && pattern.test(text)) || /^\\d+$/.test(text));
            })
            .slice(0, 40)
            .map((el) => ({
              tag: el.tagName,
              id: el.id || '',
              className: typeof el.className === 'string' ? el.className : '',
              text: (el.innerText || '').trim().slice(0, 80),
              title: el.getAttribute('title') || '',
              aria: el.getAttribute('aria-label') || '',
              html: el.outerHTML.slice(0, 500),
            }));
          const scrollables = [...document.querySelectorAll('*')]
            .filter((el) => visible(el) && el.clientHeight > 100 &&
              el.scrollHeight > el.clientHeight + 10)
            .slice(0, 20)
            .map((el) => ({
              tag: el.tagName,
              id: el.id || '',
              className: typeof el.className === 'string' ? el.className : '',
              clientHeight: el.clientHeight,
              scrollHeight: el.scrollHeight,
            }));
          const repeated = new Map();
          for (const el of document.querySelectorAll('*')) {
            if (!visible(el)) continue;
            const className = typeof el.className === 'string'
              ? el.className.trim().replace(/\\s+/g, '.') : '';
            if (!className) continue;
            const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
            if (text.length < 4 || text.length > 400) continue;
            const signature = `${el.tagName.toLowerCase()}.${className}`;
            const group = repeated.get(signature) || {
              selector: signature,
              count: 0,
              samples: [],
              html: el.outerHTML.slice(0, 500),
            };
            group.count += 1;
            if (group.samples.length < 3) group.samples.push(text.slice(0, 160));
            repeated.set(signature, group);
          }
          const likelyRows = [...repeated.values()]
            .filter((group) => group.count >= 3)
            .sort((left, right) => right.count - left.count)
            .slice(0, 30);
          return {
            beforeCount,
            afterCount,
            configuredListSelectors,
            matchedListSelector,
            url: location.href,
            bodyTextEnd: (document.body?.innerText || '').slice(-1000),
            candidates,
            scrollables,
            likelyRows,
          };
        }
        """,
        {
            "beforeCount": before_count,
            "afterCount": after_count,
            "configuredListSelectors": settings.list_item_selectors,
            "matchedListSelector": list_selector,
        },
    )
    frames = []
    for index, frame in enumerate(page.frames):
        try:
            frame_info = await frame.evaluate(
                """
                (selectors) => ({
                  title: document.title || '',
                  bodyTextStart: (document.body?.innerText || '').slice(0, 500),
                  selectorCounts: Object.fromEntries(selectors.map((selector) =>
                    [selector, document.querySelectorAll(selector).length])),
                })
                """,
                settings.list_item_selectors,
            )
        except Exception as exc:
            frame_info = {"error": str(exc)}
        frames.append({"index": index, "url": frame.url, **frame_info})
    diagnostics["frames"] = frames
    return diagnostics


async def diagnose_pagination(
    headless: bool = True,
    wait_for_user: bool = False,
) -> dict:
    """收集分页/滚动容器信息，不点击通知、不写数据库、不发邮件。

    交互诊断使用有头浏览器，并在开发者确认列表可见后再读取 DOM，避免
    校园门户偶发慢加载导致空白诊断结果。
    """
    if not settings.auth_file.exists():
        raise SessionExpiredError("未找到 auth.json，请先运行 python main.py init-auth")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=settings.auth_file)
        page = await context.new_page()
        try:
            try:
                await _goto_with_retry(page, settings.notices_url)
            except Exception:
                if not wait_for_user:
                    raise
                log.exception("自动打开门户失败；请在已打开的浏览器中手动刷新或访问通知页")
            if wait_for_user:
                print("请在 Chromium 中确认通知列表已经完整显示；如未显示，可手动刷新或登录。")
                await asyncio.to_thread(
                    input,
                    "确认列表可见后，回到此终端按 Enter 采集分页信息：",
                )
            await page.wait_for_timeout(settings.spa_wait_ms)
            if await _is_login_page(page):
                raise SessionExpiredError("会话已过期，请重新手动登录")
            list_selector = await _wait_for_list(
                page,
                timeout_ms=10_000 if wait_for_user else 45_000,
            )
            if not list_selector:
                log.warning("配置的列表选择器未匹配；将采集页面候选结构用于修复选择器")
            return await _collect_pagination_diagnostics(page)
        finally:
            await context.close()
            await browser.close()


async def diagnose_list_api() -> list[dict]:
    """在开发者打开门户时采集列表应用的 JSON 接口摘要。"""
    if not settings.auth_file.exists():
        raise SessionExpiredError("未找到 auth.json，请先运行 python main.py init-auth")
    captured: list[dict] = []
    pending: set[asyncio.Task] = set()

    async def capture(response: Response) -> None:
        if "lwPsTzggApp" not in response.url:
            return
        if response.request.resource_type not in {"xhr", "fetch"}:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if isinstance(payload, dict):
            top_level = list(payload)[:50]
        elif isinstance(payload, list):
            top_level = [f"list[{len(payload)}]"]
        else:
            top_level = [type(payload).__name__]
        captured.append(
            {
                "url": response.url,
                "status": response.status,
                "method": response.request.method,
                "postData": (response.request.post_data or "")[:2000],
                "topLevel": top_level,
                "noticeCandidates": _notice_candidates(payload),
            }
        )

    def schedule_capture(response: Response) -> None:
        task = asyncio.create_task(capture(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=settings.auth_file)
        page = await context.new_page()
        page.on("response", schedule_capture)
        try:
            try:
                await _goto_with_retry(page, settings.notices_url)
            except Exception:
                log.exception("自动打开门户失败；请在已打开的浏览器中手动刷新或访问通知页")
            print("请在 Chromium 中确认通知列表已经显示；必要时手动刷新或登录。")
            await asyncio.to_thread(
                input,
                "列表可见后回到终端按 Enter，程序将保存接口摘要：",
            )
            await page.wait_for_timeout(settings.spa_wait_ms)
            if await _is_login_page(page):
                raise SessionExpiredError("会话已过期，请重新手动登录")
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
            return captured
        finally:
            await context.close()
            await browser.close()


def parse_api_notices(payload: object, existing_urls: dict[str, str]) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("通知列表接口返回格式异常，不能将其视为列表结束")
    result = []
    seen_ids = set()
    for row in payload["data"]:
        if not isinstance(row, dict) or not row.get("GGDM") or not row.get("GGBT"):
            raise RuntimeError("通知列表接口缺少 GGDM 或 GGBT")
        identifier = str(row["GGDM"])
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        title = re.sub(r"\s+", " ", str(row["GGBT"])).strip()
        detail_url = existing_urls.get(identifier) or (
            settings.notices_url.partition("#")[0]
            + "#/show?"
            + urlencode({"dm": identifier, "lm": str(row.get("LMMC") or "")})
        )
        result.append(
            {
                "title": title,
                "publish_time": str(row.get("FBSJ") or ""),
                "department": str(row.get("FBBM") or row.get("LMMC") or ""),
                "detail_url": detail_url,
            }
        )
    return result


async def _fetch_api_list(
    context: BrowserContext, page_number: int, existing_urls: dict[str, str]
) -> list[dict]:
    for attempt in range(1, settings.retry_attempts + 1):
        try:
            response = await context.request.post(
                settings.list_api_url,
                form={
                    "data": json.dumps(
                        {
                            "PAGENUM": page_number,
                            "PAGESIZE": settings.list_page_size,
                        }
                    )
                },
                headers={"Referer": settings.notices_url},
                timeout=settings.action_timeout_ms,
            )
            if "ids.chd.edu.cn" in response.url or response.status in {401, 403}:
                raise SessionExpiredError("会话已过期，请重新手动登录")
            if not response.ok:
                raise RuntimeError(f"通知列表接口 HTTP {response.status}")
            return parse_api_notices(await response.json(), existing_urls)
        except SessionExpiredError:
            raise
        except Exception as exc:
            if attempt == settings.retry_attempts:
                raise
            log.warning(
                "列表接口第 %d 页请求失败（%d/%d）：%s",
                page_number,
                attempt,
                settings.retry_attempts,
                exc,
            )
            await asyncio.sleep(settings.retry_backoff_seconds * attempt)
    raise RuntimeError("列表接口重试结束")


class AttachmentDownloadError(RuntimeError):
    pass


async def _collect_notice(context, notice: dict, stats: ScrapeStats) -> None:
    attempts = getattr(settings, "retry_attempts", 3)
    for attempt in range(1, attempts + 1):
        try:
            content, attachments = await _scrape_detail(context, notice["detail_url"])
            candidate = {**notice, "content_html": content, "attachments": []}
            for attachment in attachments:
                downloaded = await _download_attachment(
                    context, attachment["url"], attachment["name"]
                )
                if not downloaded.get("path"):
                    if downloaded.get("error_type") == "SessionExpiredError":
                        raise SessionExpiredError("附件会话已过期，请重新手动登录")
                    raise AttachmentDownloadError("附件未完整下载，本条保留旧版本待补偿")
                candidate["attachments"].append(downloaded)
            content_hash = notice_content_hash(candidate)
            existing = get_notice_by_url(notice["detail_url"])
            # 兼容此前不包含附件 SHA256 的算法；内容相同时只升级基线，不重发。
            if existing and existing["content_hash"] != content_hash:
                old_algorithm_matches = existing["content_hash"] in {
                    notice_content_hash(
                        existing,
                        include_attachment_hash=attachment_hash,
                        include_body_resources=resources,
                    )
                    for attachment_hash, resources in ((False, False), (True, False), (False, True))
                }
                old_attachments = existing.get("attachments", [])
                if old_algorithm_matches and (
                    notice_content_hash(existing) == content_hash
                    or (
                        not any(a.get("sha256") for a in old_attachments)
                        and notice_content_hash(existing, include_attachment_hash=False)
                        == notice_content_hash(candidate, include_attachment_hash=False)
                    )
                ):
                    update_notice_hash_baseline(
                        notice["detail_url"],
                        existing["content_hash"],
                        content_hash,
                        candidate["attachments"],
                    )
                    stats.baselined += 1
                    resolve_scrape_failure(notice["detail_url"])
                    return
            status = save_notice(candidate, content_hash)
            if status == "new":
                stats.new += 1
            elif status == "updated":
                stats.updated += 1
            elif status == "baseline":
                stats.baselined += 1
            else:
                stats.unchanged += 1
            resolve_scrape_failure(notice["detail_url"])
            log.info("详情处理完成：%s", status)
            return
        except (PlaywrightTimeoutError, httpx.TransportError, AttachmentDownloadError) as exc:
            if attempt < attempts:
                await asyncio.sleep(getattr(settings, "retry_backoff_seconds", 3) * attempt)
                continue
            record_scrape_failure(notice, type(exc).__name__)
            stats.failed += 1
            log.warning("单条暂时失败，已入补偿队列；错误类型=%s", type(exc).__name__)


async def scrape_notices(headless: bool = True) -> ScrapeStats:
    init_db()
    stats = ScrapeStats()
    run_id = start_scrape_run()
    try:
        await _scrape_notices(stats, headless, run_id)
    except BaseException as exc:
        finish_scrape_run(run_id, stats, error_type=type(exc).__name__)
        raise
    finish_scrape_run(run_id, stats)
    return stats


async def _scrape_notices(stats: ScrapeStats, headless: bool, run_id: int) -> None:
    if not settings.auth_file.exists():
        raise SessionExpiredError("未找到 auth.json，请先运行 python main.py init-auth")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=settings.auth_file)
        page = await context.new_page()
        try:
            log.info("正在打开通知列表")
            await _open_notice_list(page)
            log.info("通知列表已加载：%s", page.url)
            seen: set[str] = set()
            existing_urls = get_existing_notice_urls()
            total_budget = settings.max_pages_to_scrape
            if total_budget < 1:
                raise ValueError("列表页预算必须大于零")
            history_budget = (
                min(getattr(settings, "history_pages_per_run", 2), total_budget - 1)
                if getattr(settings, "history_scan_enabled", False)
                else 0
            )
            recent_budget = total_budget - history_budget
            last_recent_page = 0
            empty_latest = False

            async def scan_phase(start_page: int, budget: int, phase: str):
                nonlocal last_recent_page, empty_latest
                fingerprints = set()
                consecutive_known = 0
                for offset in range(budget):
                    page_number = start_page + offset
                    stats.pages += 1
                    summaries = []
                    try:
                        summaries = await _fetch_api_list(context, page_number, existing_urls)
                        reason = "budget" if offset == budget - 1 else "continue"
                        if not summaries:
                            reason = "empty"
                        else:
                            fingerprint = tuple(item["detail_url"] for item in summaries)
                            if fingerprint in fingerprints:
                                reason = "repeated"
                            else:
                                fingerprints.add(fingerprint)
                                all_known = all(
                                    get_notice_hash(item["detail_url"]) is not None
                                    for item in summaries
                                )
                                consecutive_known = consecutive_known + 1 if all_known else 0
                                for notice in summaries:
                                    if notice["detail_url"] not in seen:
                                        seen.add(notice["detail_url"])
                                        await _collect_notice(context, notice, stats)
                                if len(summaries) < settings.list_page_size:
                                    reason = "end"
                                elif (
                                    phase == "latest"
                                    and consecutive_known >= settings.known_pages_to_stop
                                ):
                                    reason = "known-pages"
                        if phase == "latest":
                            last_recent_page = page_number
                            empty_latest = page_number == 1 and reason == "empty"
                        next_page = (
                            (2 if reason in {"empty", "end"} else page_number + 1)
                            if phase == "history"
                            else None
                        )
                        if phase == "history" and reason == "budget" and budget > 1:
                            # 每轮保留一页边界重叠，缓解页码因新通知插入而位移。
                            next_page = page_number
                        record_scan_page(
                            run_id, phase, page_number, len(summaries), reason, next_page=next_page
                        )
                        log.info(
                            "扫描范围：%s 第 %d 页，%d 条，原因=%s",
                            phase,
                            page_number,
                            len(summaries),
                            reason,
                        )
                        if reason != "continue":
                            break
                    except BaseException:
                        record_scan_page(
                            run_id, phase, page_number, len(summaries), "system-failure"
                        )
                        raise

            await scan_phase(1, recent_budget, "latest")
            if history_budget and not empty_latest:
                start_page = max(get_history_scan_cursor(), last_recent_page + 1)
                await scan_phase(
                    start_page, min(history_budget, total_budget - stats.pages), "history"
                )
            await _save_auth_state_atomic(context)
            # 有界补偿：列表窗口外的失败也会再次检查；本次已检查的 URL 不重复请求。
            for failure in get_pending_scrape_failures():
                if failure["detail_url"] not in seen:
                    seen.add(failure["detail_url"])
                    await _collect_notice(context, json.loads(failure["snapshot_json"]), stats)
        finally:
            await context.close()
            await browser.close()
    return None


async def _save_auth_state_atomic(context: BrowserContext) -> None:
    """先完整获取状态，再同目录写入并原子替换；失败不覆盖旧会话。"""
    state = await context.storage_state()
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("cookies"), list)
        or not isinstance(state.get("origins"), list)
    ):
        raise ValueError("浏览器返回了无效的登录状态")
    payload = json.dumps(state, ensure_ascii=False)
    target = settings.auth_file
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


async def initialize_auth() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = None
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await _goto_with_retry(page, settings.notices_url)
            print("请在打开的浏览器中完成 CAS 登录，并确认已进入通知页面。")
            await asyncio.to_thread(input, "完成后回到终端按 Enter 保存会话：")
            if await _is_login_page(page):
                raise RuntimeError("仍停留在登录页，未保存会话")
            if not await _wait_for_list(
                page, timeout_ms=getattr(settings, "spa_load_timeout_ms", 30000)
            ):
                raise RuntimeError("未验证到通知列表，未保存会话；请确认登录和选择器")
            await _save_auth_state_atomic(context)
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()
        print(f"会话已保存到 {settings.auth_file}")
