"""SearXNG client: trims, dedups, caches, and reports an outage as SearchError."""
import httpx
import pytest

import backend.search.searxng as sx


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._data


def _client(calls, resp):
    class C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            calls.append((url, params))
            if isinstance(resp, Exception):
                raise resp
            return resp
    return C


@pytest.fixture(autouse=True)
def _clear():
    sx._CACHE.clear()
    yield
    sx._CACHE.clear()


async def test_search_trims_dedups_and_caches(monkeypatch):
    calls = []
    data = {"results": [
        {"title": "A", "url": "https://a", "content": "aa", "engines": ["bing"]},
        {"title": "A dup", "url": "https://a", "content": "x"},
        {"title": "B", "url": "https://b", "content": "bb", "engine": "yahoo"},
    ]}
    monkeypatch.setattr(sx.httpx, "AsyncClient", _client(calls, _Resp(data)))
    out = await sx.search("  hello   world ", n=5, base_url="http://s")
    assert [r["url"] for r in out] == ["https://a", "https://b"]
    assert out[1]["engines"] == ["yahoo"]
    assert calls[0] == ("http://s/search", {"q": "hello world", "format": "json"})
    await sx.search("hello world", n=5, base_url="http://s")
    assert len(calls) == 1                                       # cached


async def test_search_outage_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(sx.httpx, "AsyncClient", _client(calls, httpx.ConnectError("down")))
    async def fast_sleep(_):
        return None
    monkeypatch.setattr(sx.asyncio, "sleep", fast_sleep)
    with pytest.raises(sx.SearchError):
        await sx.search("q", base_url="http://s")
    assert len(calls) == 3


def test_format_results_caps_length():
    rs = [{"title": f"t{i}", "url": f"u{i}", "snippet": "s" * 100} for i in range(50)]
    assert len(sx.format_results(rs, 500)) <= 500
