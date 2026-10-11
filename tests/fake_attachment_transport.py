"""旧附件回归用例的 HTTP 流替身；绝不发出网络请求。"""

import httpx


def install_fake_attachment_transport(monkeypatch, scraper, context, get):
    async def cookies(_urls):
        return []

    context.cookies = cookies

    class Stream:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            self.response = await get(self.url, timeout=scraper.settings.action_timeout_ms)
            self.status_code = getattr(self.response, "status", 200)
            self.url = self.response.url
            self.headers = getattr(self.response, "headers", {})
            return self

        async def __aexit__(self, *_args):
            pass

        def raise_for_status(self):
            assert 200 <= self.status_code < 300

        async def aiter_raw(self, chunk_size):
            if hasattr(self.response, "aiter_bytes"):
                async for chunk in self.response.aiter_bytes(chunk_size):
                    yield chunk
            else:
                payload = await self.response.body()
                for offset in range(0, len(payload), chunk_size):
                    yield payload[offset : offset + chunk_size]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            self.cookies = httpx.Cookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def stream(self, method, url, **_kwargs):
            assert method == "GET"
            return Stream(url)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", Client)
