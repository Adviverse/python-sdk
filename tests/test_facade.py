import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from adviverse import Adviverse, AdviverseAuthError, AdviverseApiError, AdviverseRateLimitError

class _H(BaseHTTPRequestHandler):
    calls = 0
    def _send(self, code, body, hdrs=None):
        self.send_response(code)
        for k, v in (hdrs or {}).items(): self.send_header(k, v)
        self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    def do_GET(self):
        _H.calls += 1
        assert self.headers.get("x-api-key") == "k_test"   # auth header present
        if self.path.startswith("/v1/_retry"):
            if _H.calls < 3: return self._send(503, {"error": "transient"})
            return self._send(200, {"ok": True})
        if self.path.startswith("/v1/echo"):
            return self._send(200, {"path": self.path})
        if self.path.startswith("/v1/_err"):
            return self._send(404, {"message": "nope"})
        return self._send(200, {"data": [], "ok": True})
    def do_POST(self):
        _H.calls += 1
        n = int(self.headers.get("content-length") or 0); self.rfile.read(n)
        if self.path.startswith("/v1/_post503"):
            return self._send(503, {"error": "down"})
        return self._send(200, {"path": self.path})
    def log_message(self, *a): pass

@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _H); _H.calls = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()

def test_requires_api_key():
    with pytest.raises(AdviverseAuthError):
        Adviverse(api_key="")

def test_auth_header_and_namespace_call(server):
    c = Adviverse(api_key="k_test", base_url=server)
    assert hasattr(c, "advertiser")          # a namespace exists (spec has no "campaigns" tag)
    r = c._request("GET", "/v1/ok")          # core request sends x-api-key (asserted server-side)
    assert r["ok"] is True

def test_retries_then_succeeds(server):
    c = Adviverse(api_key="k_test", base_url=server, max_retries=3)
    assert c._request("GET", "/v1/_retry")["ok"] is True   # 503,503,200

def test_typed_api_error(server):
    c = Adviverse(api_key="k_test", base_url=server)
    with pytest.raises(AdviverseApiError) as ei:
        c._request("GET", "/v1/_err")
    assert ei.value.status == 404 and "nope" in str(ei.value)

def test_paginate_terminates_on_empty_page(server):
    # The default handler returns {"data": [], ...} with no cursor — paginate must
    # stop immediately rather than loop forever.
    c = Adviverse(api_key="k_test", base_url=server)
    assert list(c.paginate("GET", "/v1/list")) == []

def test_namespace_method_end_to_end(server):
    from adviverse._facade.routes import ROUTES
    ns, spec = next((n, m) for n, ms in ROUTES.items() for m in ms if m["httpMethod"] == "GET" and "{" not in m["path"])
    c = Adviverse(api_key="k_test", base_url=server)
    r = getattr(getattr(c, ns), spec["method"])()
    assert r.get("ok") is True and _H.calls == 1   # reached server via namespace.method binding
    assert spec["method"] == spec["method"].lower() and "controller" not in spec["method"]

def test_namespace_path_param_mapping(server):
    c = Adviverse(api_key="k_test", base_url=server)
    from adviverse.client import _Namespace
    ns = _Namespace(c, {"echo": {"method": "echo", "httpMethod": "GET", "path": "/v1/echo/{id}",
                                 "params": [{"name": "id", "in": "path", "required": True},
                                            {"name": "q", "in": "query", "required": False}]}})
    assert ns.echo(id="42", q="x")["path"] == "/v1/echo/42?q=x"

def test_finance_namespace_snake_methods():
    from adviverse._facade.routes import ROUTES
    assert "finance" in ROUTES
    assert any(m["method"] == "submit_tax_form" for m in ROUTES["finance"])

def test_post_503_not_retried(server):
    c = Adviverse(api_key="k_test", base_url=server, max_retries=3)
    with pytest.raises(AdviverseApiError):
        c._request("POST", "/v1/_post503", body={"a": 1})
    assert _H.calls == 1
