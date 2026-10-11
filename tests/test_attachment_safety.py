"""附件可信目标及登录伪装测试；假请求，不访问校园网。"""

from types import SimpleNamespace

import httpx
import pytest

import scraper
from tests.test_service import _run_immediate


@pytest.mark.parametrize(
    "url,body,expected_requests",
    [
        ("http://127.0.0.1/admin", b"not-an-attachment", 0),
        (
            "https://portal.chd.edu.cn/download/file",
            b'<html><form action="/authserver/login"><input type="password" name="password"><input name="username">CAS Login</form></html>',
            1,
        ),
    ],
)
def test_unsafe_attachment_is_never_saved(tmp_path, monkeypatch, url, body, expected_requests):
    requests = []

    async def response_body():
        return body

    response = SimpleNamespace(
        ok=True, status=200, url=url, headers={"content-type": "text/html"}, body=response_body
    )

    async def get(target, **_kwargs):
        requests.append(target)
        return response

    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            action_timeout_ms=1000,
            login_url="https://ids.chd.edu.cn/authserver/login",
            attachment_dir=tmp_path / "attachments",
            attachment_allowed_hosts=("portal.chd.edu.cn",),
            max_single_attachment_bytes=1024,
        ),
    )
    from tests.fake_attachment_transport import install_fake_attachment_transport

    context = SimpleNamespace()
    install_fake_attachment_transport(monkeypatch, scraper, context, get)
    item = _run_immediate(scraper._download_attachment(context, url, "report.html"))
    assert len(requests) == expected_requests
    assert item["path"] == ""
    assert list(tmp_path.rglob("report*")) == []


@pytest.mark.parametrize(
    "scenario", ["declared-large", "stream-large", "exact-limit", "compressed"]
)
def test_stream_budget_is_enforced_before_publishing(tmp_path, monkeypatch, scenario):
    reads = []
    client_type = httpx.AsyncClient
    limit = 64 * 1024

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(3 if scenario == "stream-large" else 1):
                reads.append(index)
                yield b"x" * limit

    def handler(request):
        headers = {"content-length": str(limit + 1)} if scenario == "declared-large" else {}
        if scenario == "compressed":
            headers["content-encoding"] = "gzip"
        return httpx.Response(200, headers=headers, stream=Bytes())

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    async def cookies(_urls):
        return []

    monkeypatch.setattr(scraper.httpx, "AsyncClient", client)
    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            action_timeout_ms=1000,
            attachment_dir=tmp_path / "attachments",
            login_url="https://ids.chd.edu.cn/authserver/login",
            attachment_allowed_hosts=("portal.chd.edu.cn",),
            max_single_attachment_bytes=limit,
        ),
    )
    item = _run_immediate(
        scraper._download_attachment(
            SimpleNamespace(cookies=cookies), "https://portal.chd.edu.cn/report", "report.bin"
        )
    )
    if scenario == "exact-limit":
        assert item["size"] == limit
        assert item["path"]
    else:
        assert item["path"] == ""
        assert list(tmp_path.rglob(".attachment-*.tmp")) == []
    assert reads == (
        [0, 1] if scenario == "stream-large" else [0] if scenario == "exact-limit" else []
    )


@pytest.mark.parametrize("trusted_redirect", [True, False])
def test_redirect_is_checked_and_cookies_are_selected_per_target(
    tmp_path, monkeypatch, trusted_redirect
):
    client_type = httpx.AsyncClient
    requests = []
    cookie_queries = []
    target = (
        "https://files.example.test/report"
        if trusted_redirect
        else "https://untrusted.example.test/report"
    )

    async def cookies(urls):
        cookie_queries.append(urls[0])
        return (
            [
                {
                    "name": "session",
                    "value": "fake-session",
                    "domain": "portal.chd.edu.cn",
                    "path": "/",
                }
            ]
            if "portal.chd.edu.cn" in urls[0]
            else []
        )

    def handler(request):
        requests.append(request)
        if request.url.host == "portal.chd.edu.cn":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, stream=httpx.ByteStream(b"report"))

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", client)
    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            action_timeout_ms=1000,
            attachment_dir=tmp_path / "attachments",
            login_url="https://ids.chd.edu.cn/authserver/login",
            attachment_allowed_hosts=("portal.chd.edu.cn", "files.example.test"),
            max_single_attachment_bytes=1024,
        ),
    )
    item = _run_immediate(
        scraper._download_attachment(
            SimpleNamespace(cookies=cookies), "https://portal.chd.edu.cn/report", "report.bin"
        )
    )
    assert requests[0].headers["cookie"] == "session=fake-session"
    assert len(requests) == (2 if trusted_redirect else 1)
    assert len(cookie_queries) == len(requests)
    if trusted_redirect:
        assert "cookie" not in requests[1].headers
        assert item["path"]
    else:
        assert item["path"] == ""
