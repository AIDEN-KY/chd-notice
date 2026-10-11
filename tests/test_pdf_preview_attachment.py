"""学校 PDF 预览包装链接的下载契约，全部请求均为替身。"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import scraper
from tests.fake_attachment_transport import install_fake_attachment_transport
from tests.test_service import _run_immediate


@pytest.mark.parametrize("unsafe", [False, True])
def test_pdf_preview_uses_only_validated_underlying_download(tmp_path, monkeypatch, unsafe):
    host = "https://portal.chd.edu.cn"
    path = "/qljfwapp/sys/emapcomponent/file/getAttachmentFile/0123456789abcdef.do?callerApp=lwPsTzggApp"
    source = "https://untrusted.example.test/private.pdf" if unsafe else path
    wrapper = host + "/qljfwapp/sys/emapcomponent/pdf/index.do?file=" + quote(source, safe="")
    download = host + path
    requests = []
    payload = b"%PDF-1.7\nfake-test-document"

    async def body():
        return payload

    async def get(url, **_kwargs):
        requests.append(url)
        if url == download:
            return SimpleNamespace(status=200, url=url, headers={}, body=body)
        # 预览包装页的重定向不等于附件正文，不应请求或信任此页面。
        return SimpleNamespace(
            status=302, url=url, headers={"location": "http://untrusted.example.test/viewer"}
        )

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
    context = SimpleNamespace()
    install_fake_attachment_transport(monkeypatch, scraper, context, get)
    cookie_targets = []

    async def cookies(urls):
        cookie_targets.extend(urls)
        return []

    context.cookies = cookies
    item = _run_immediate(scraper._download_attachment(context, wrapper, "通知附件.pdf"))
    if unsafe:
        assert requests == [], "预览参数的目标必须在发出请求前校验"
        assert cookie_targets == []
        assert not item["path"]
    else:
        assert requests == [download], "应下载 file 参数里的学校附件地址，而不是 PDF 预览页"
        assert item["path"]
        assert item["size"] == len(payload)
        assert Path(item["path"]).read_bytes() == payload
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()
        assert cookie_targets == [download]
        assert item["url"] == wrapper, "保留原链接，避免无意义的附件 URL 哈希变化"


@pytest.mark.parametrize(
    "source",
    [
        "",
        "https://untrusted.example.test/private.pdf",
        "http://portal.chd.edu.cn/private.pdf",
        "https://127.0.0.1/file",
        "https://ids.chd.edu.cn/authserver/login",
        "/qljfwapp/sys/emapcomponent/pdf/index.do?file=x",
        "/other/endpoint",
        "/qljfwapp/sys/emapcomponent/file/getAttachmentFile/abc.do#fragment",
        "/qljfwapp/sys/emapcomponent/file/getAttachmentFile/abc.do\n",
        "%2Fqljfwapp%2Fsys%2Femapcomponent%2Ffile%2FgetAttachmentFile%2Fabc.do",
    ],
)
def test_preview_rejects_invalid_embedded_download_before_http(monkeypatch, source):
    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            login_url="https://ids.chd.edu.cn/authserver/login",
            attachment_allowed_hosts=("portal.chd.edu.cn",),
        ),
    )
    wrapper = "https://portal.chd.edu.cn/qljfwapp/sys/emapcomponent/pdf/index.do?file="
    with pytest.raises((ValueError, scraper.SessionExpiredError)):
        scraper._attachment_download_url(wrapper + quote(source, safe=""))


@pytest.mark.parametrize("absolute", [False, True])
def test_preview_supports_relative_and_absolute_school_file(monkeypatch, absolute):
    monkeypatch.setattr(
        scraper,
        "settings",
        SimpleNamespace(
            login_url="https://ids.chd.edu.cn/authserver/login",
            attachment_allowed_hosts=("portal.chd.edu.cn",),
        ),
    )
    host = "https://portal.chd.edu.cn"
    path = "/qljfwapp/sys/emapcomponent/file/getAttachmentFile/fd3574d10764435b806eaae046c22b38.do?callerApp=lwPsTzggApp"
    source = host + path if absolute else path
    wrapper = host + "/qljfwapp/sys/emapcomponent/pdf/index.do?file=" + quote(source, safe="")
    assert scraper._attachment_download_url(wrapper) == host + path
    with pytest.raises(ValueError):
        scraper._attachment_download_url(wrapper + "&file=" + quote(source, safe=""))
