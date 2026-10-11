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


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(f"{name} 必须为正整数") from None


def _attachment_hosts() -> tuple[str, ...]:
    hosts = tuple(
        h.strip().lower()
        for h in os.getenv("ATTACHMENT_ALLOWED_HOSTS", "portal.chd.edu.cn").split("||")
        if h.strip()
    )
    if not hosts or any("/" in h or ":" in h or "*" in h for h in hosts):
        raise ValueError("ATTACHMENT_ALLOWED_HOSTS 必须是精确主机名，以 || 分隔")
    return hosts


@dataclass(frozen=True)
class Settings:
    login_url: str = "https://ids.chd.edu.cn/authserver/login"
    notices_url: str = "https://portal.chd.edu.cn/qljfwapp/sys/lwPsTzggApp/*default/index.do#/tzgg"
    list_api_url: str = (
        "https://portal.chd.edu.cn/qljfwapp/sys/lwPsTzggApp/bulletinController/getGGInfo.do"
    )
    list_page_size: int = 12
    auth_file: Path = BASE_DIR / "auth.json"
    database_file: Path = BASE_DIR / "notices.db"
    attachment_dir: Path = BASE_DIR / "downloads"
    log_dir: Path = BASE_DIR / "logs"
    api_diagnostics_file: Path = BASE_DIR / "portal-api-diagnostics.json"
    timezone: ZoneInfo = ZoneInfo("Asia/Shanghai")
    # 动态翻页：连续遇到若干个全部已知的页面后停止，并设置最大页数兜底。
    max_pages_to_scrape: int = 20
    known_pages_to_stop: int = 3
    history_pages_per_run: int = field(
        default_factory=lambda: _positive_int_env("HISTORY_PAGES_PER_RUN", 2)
    )
    spa_wait_ms: int = 2_500
    spa_load_timeout_ms: int = 30_000
    action_timeout_ms: int = 20_000
    # 门户偶尔长时间不触发 DOMContentLoaded；导航只等待响应开始，再单独等 SPA 元素。
    navigation_timeout_ms: int = 60_000
    retry_attempts: int = 3
    retry_backoff_seconds: int = 3
    alert_cooldown_seconds: int = 3600
    max_attachment_bytes: int = 20 * 1024 * 1024
    max_single_attachment_bytes: int = field(
        default_factory=lambda: _positive_int_env("MAX_SINGLE_ATTACHMENT_BYTES", 20 * 1024 * 1024)
    )
    # 本项目的保守邮件预算，不代表 QQ 服务端公布的上限。
    max_message_bytes: int = field(
        default_factory=lambda: _positive_int_env("MAX_MESSAGE_BYTES", 20 * 1024 * 1024)
    )
    attachment_allowed_hosts: tuple[str, ...] = field(default_factory=_attachment_hosts)
    max_attachment_redirects: int = 3
    schedule_hour: int = 21
    schedule_minute: int = 0
    deepseek_url: str = "https://api.deepseek.com/chat/completions"
    deepseek_model: str = "deepseek-chat"
    summary_prompt_version: str = "important-information-v1"
    max_notices_per_digest: int = field(
        default_factory=lambda: _positive_int_env("MAX_NOTICES_PER_DIGEST", 20)
    )
    max_digest_messages_per_day: int = field(
        default_factory=lambda: _positive_int_env("MAX_DIGEST_MESSAGES_PER_DAY", 1)
    )
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    # 授权范围及上线验收未完成前不得开放正式多人发送；不提供环境变量绕过。
    multi_user_delivery_ready: bool = False

    # 多个候选选择器用 || 分隔，可在 .env 中覆盖。
    list_item_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "LIST_ITEM_SELECTORS",
            [
                ".bh-border-color-gray-5.bh-bg-color-light",
                ".news-list li",
                ".notice-list li",
                ".bh-list li",
                ".list-group-item",
                "tbody tr",
            ],
        )
    )
    title_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "TITLE_SELECTORS",
            ["#readClass", ".title", ".news-title", ".notice-title", "td:nth-child(1)", "a"],
        )
    )
    time_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "TIME_SELECTORS",
            [".time_class", ".time", ".date", ".publish-time", "td:nth-last-child(2)"],
        )
    )
    department_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "DEPARTMENT_SELECTORS",
            [".lmmc_class", ".department", ".dept", ".publisher", "td:nth-last-child(1)"],
        )
    )
    detail_link_selectors: list[str] = field(
        default_factory=lambda: _selectors("DETAIL_LINK_SELECTORS", ["a[href]"])
    )
    detail_content_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "DETAIL_CONTENT_SELECTORS",
            [
                ".ggnrclass",
                ".article-content",
                ".news-content",
                ".notice-content",
                ".bh-article-content",
                ".content",
            ],
        )
    )
    attachment_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "ATTACHMENT_SELECTORS",
            [
                ".attachment a[href]",
                ".attachments a[href]",
                "a[href*='download']",
                "a[href*='file']",
            ],
        )
    )
    next_page_selectors: list[str] = field(
        default_factory=lambda: _selectors(
            "NEXT_PAGE_SELECTORS",
            [
                ".pagination .next:not(.disabled)",
                ".el-pagination .btn-next:not([disabled])",
                "a[title='下一页']",
            ],
        )
    )
    scroll_container_selectors: list[str] = field(
        default_factory=lambda: _selectors("SCROLL_CONTAINER_SELECTORS", ["#app"])
    )

    @property
    def history_scan_enabled(self) -> bool:
        value = os.getenv("HISTORY_SCAN_ENABLED", "true").strip().lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("HISTORY_SCAN_ENABLED 必须为 true/false/1/0")
        return value in {"true", "1"}

    @property
    def ai_summary_enabled(self) -> bool:
        value = os.getenv("AI_SUMMARY_ENABLED", "true").strip().lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("AI_SUMMARY_ENABLED 必须为 true/false/1/0")
        return value in {"true", "1"}

    @property
    def ai_excluded_keywords(self) -> tuple[str, ...]:
        value = os.getenv("AI_EXCLUDED_KEYWORDS", "内部资料||禁止转发||保密||不得外传")
        keywords = tuple(item.strip() for item in value.split("||") if item.strip())
        if not keywords:
            raise ValueError("AI_EXCLUDED_KEYWORDS 不能为空；多个标记用 || 分隔")
        return keywords

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
