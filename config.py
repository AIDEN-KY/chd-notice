from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(f"缺少环境变量：{name}")
    return value.strip()


def _selectors(env_name: str, defaults: list[str]) -> list[str]:
    value = os.getenv(env_name)
    return [item.strip() for item in value.split("||") if item.strip()] if value else defaults


@dataclass(frozen=True)
class Settings:
    login_url: str = "https://ids.chd.edu.cn/authserver/login"
    notices_url: str = (
        "https://portal.chd.edu.cn/qljfwapp/sys/lwPsTzggApp/"
        "*default/index.do#/tzgg"
    )
    auth_file: Path = BASE_DIR / "auth.json"
    database_file: Path = BASE_DIR / "notices.db"
    attachment_dir: Path = BASE_DIR / "downloads"
    log_dir: Path = BASE_DIR / "logs"
    timezone: ZoneInfo = ZoneInfo("Asia/Shanghai")
    # 动态翻页：连续遇到若干个全部已知的页面后停止，并设置最大页数兜底。
    max_pages_to_scrape: int = 20
    known_pages_to_stop: int = 3
    spa_wait_ms: int = 2_500
    action_timeout_ms: int = 20_000
    # 门户偶尔长时间不触发 DOMContentLoaded；导航只等待响应开始，再单独等 SPA 元素。
    navigation_timeout_ms: int = 60_000
    retry_attempts: int = 3
    retry_backoff_seconds: int = 3
    max_attachment_bytes: int = 20 * 1024 * 1024
    schedule_hour: int = 21
    schedule_minute: int = 0
    deepseek_url: str = "https://api.deepseek.com/chat/completions"
    deepseek_model: str = "deepseek-chat"
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465

    # 多个候选选择器用 || 分隔，可在 .env 中覆盖。
    list_item_selectors: list[str] = field(default_factory=lambda: _selectors(
        "LIST_ITEM_SELECTORS",
        [
            ".bh-border-color-gray-5.bh-bg-color-light",
            ".news-list li",
            ".notice-list li",
            ".bh-list li",
            ".list-group-item",
            "tbody tr",
        ],
    ))
    title_selectors: list[str] = field(default_factory=lambda: _selectors(
        "TITLE_SELECTORS",
        ["#readClass", ".title", ".news-title", ".notice-title", "td:nth-child(1)", "a"],
    ))
    time_selectors: list[str] = field(default_factory=lambda: _selectors(
        "TIME_SELECTORS",
        [".time_class", ".time", ".date", ".publish-time", "td:nth-last-child(2)"],
    ))
    department_selectors: list[str] = field(default_factory=lambda: _selectors(
        "DEPARTMENT_SELECTORS",
        [".lmmc_class", ".department", ".dept", ".publisher", "td:nth-last-child(1)"],
    ))
    detail_link_selectors: list[str] = field(default_factory=lambda: _selectors(
        "DETAIL_LINK_SELECTORS", ["a[href]"]
    ))
    detail_content_selectors: list[str] = field(default_factory=lambda: _selectors(
        "DETAIL_CONTENT_SELECTORS",
        [
            ".ggnrclass",
            ".article-content",
            ".news-content",
            ".notice-content",
            ".bh-article-content",
            ".content",
        ],
    ))
    attachment_selectors: list[str] = field(default_factory=lambda: _selectors(
        "ATTACHMENT_SELECTORS",
        [".attachment a[href]", ".attachments a[href]", "a[href*='download']", "a[href*='file']"],
    ))
    next_page_selectors: list[str] = field(default_factory=lambda: _selectors(
        "NEXT_PAGE_SELECTORS",
        [".pagination .next:not(.disabled)", ".el-pagination .btn-next:not([disabled])", "a[title='下一页']"],
    ))
    scroll_container_selectors: list[str] = field(default_factory=lambda: _selectors(
        "SCROLL_CONTAINER_SELECTORS", ["#app"]
    ))

    @property
    def deepseek_api_key(self) -> str:
        return _env("DEEPSEEK_API_KEY")

    @property
    def qq_email(self) -> str:
        return _env("QQ_EMAIL")

    @property
    def qq_smtp_auth_code(self) -> str:
        return _env("QQ_SMTP_AUTH_CODE")

    @property
    def my_email(self) -> str:
        return _env("MY_EMAIL")

    @property
    def unsubscribe_url(self) -> str:
        """可选的真实退订页；未配置时由邮件模块降级为 mailto 链接。"""
        return os.getenv("UNSUBSCRIBE_URL", "").strip()


settings = Settings()
