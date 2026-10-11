"""全文分发兜底；关键词不代表内容授权或完整敏感信息识别。"""

from bs4 import BeautifulSoup

DEFAULT_EXCLUDED_KEYWORDS = ("内部资料", "禁止转发", "保密", "不得外传")


def notice_is_excluded(notice: dict, keywords: tuple[str, ...]) -> bool:
    text = BeautifulSoup(notice.get("content_html", ""), "html.parser").get_text()
    names = "\n".join(a.get("name", "") for a in notice.get("attachments", []))
    searchable = f"{notice.get('title', '')}\n{text}\n{names}"
    compact = "".join(searchable.split())
    return any("".join(keyword.split()) in compact for keyword in keywords)
