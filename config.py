from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(f"缺少环境变量：{name}")
    if "\r" in value or "\n" in value:
        raise RuntimeError(f"{name} 不得包含换行符")
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
    pdf_preview_host: str = "portal.chd.edu.cn"
    pdf_preview_path: str = "/qljfwapp/sys/emapcomponent/pdf/index.do"
    attachment_download_path_prefix: str = "/qljfwapp/sys/emapcomponent/file/getAttachmentFile/"
    summary_prompt_version: str = "important-information-v2-validated"
    task_time_budget_seconds: int = field(
        default_factory=lambda: _positive_int_env("TASK_TIME_BUDGET_SECONDS", 900)
    )
    summary_concurrency: int = field(
        default_factory=lambda: _positive_int_env("SUMMARY_CONCURRENCY", 2)
    )
    summary_min_chars: int = 50
    summary_max_chars: int = 400
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

    def __post_init__(self) -> None:
        ranges = {
            "schedule_hour": (0, 23),
            "schedule_minute": (0, 59),
            "smtp_port": (1, 65535),
            "summary_concurrency": (1, 8),
            "retry_attempts": (1, 10),
        }
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} 超出允许范围")
        for name in (
            "task_time_budget_seconds",
            "navigation_timeout_ms",
            "action_timeout_ms",
            "spa_load_timeout_ms",
            "max_notices_per_digest",
            "max_digest_messages_per_day",
            "max_message_bytes",
            "max_single_attachment_bytes",
            "max_pages_to_scrape",
            "max_attachment_bytes",
            "known_pages_to_stop",
            "history_pages_per_run",
            "list_page_size",
            "alert_cooldown_seconds",
            "summary_min_chars",
            "summary_max_chars",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正整数")
        if type(self.retry_backoff_seconds) is not int or self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds 必须为非负整数")
        if self.summary_min_chars > self.summary_max_chars:
            raise ValueError("摘要长度上下限无效")
        for name in ("login_url", "notices_url", "list_api_url", "deepseek_url"):
            from urllib.parse import urlparse

            parsed = urlparse(getattr(self, name))
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"{name} 必须为不含凭证的 HTTPS URL")

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
        raw = os.getenv("UNSUBSCRIBE_URL", "")
        value = raw.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            if (
                any(ord(c) < 32 or ord(c) == 127 for c in raw)
                or any(c.isspace() for c in value)
                or "\\" in value
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or (parsed.port is not None and parsed.port < 1)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("UNSUBSCRIBE_URL 必须为不含凭据或控制字符的有效 HTTPS URL") from None
        return value


settings = Settings()
