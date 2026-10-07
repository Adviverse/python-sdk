"""Hand-written runtime core for the Adviverse flagship client."""
from __future__ import annotations
import json, socket, time, urllib.request, urllib.error
from typing import Any, Iterator

class AdviverseError(Exception): ...
class AdviverseAuthError(AdviverseError): ...
class AdviverseRateLimitError(AdviverseError): ...
class AdviverseApiError(AdviverseError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message, self.body = status, message, body

_RETRY_STATUS = {429, 500, 502, 503, 504}
_IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

class Core:
    def __init__(self, api_key: str, base_url: str = "https://adviverse.com",
                 timeout: float = 30.0, max_retries: int = 3, bearer: bool = False):
        if not api_key:
            raise AdviverseAuthError("api_key is required")
        self._key, self._base = api_key, base_url.rstrip("/")
        self._timeout, self._max_retries, self._bearer = timeout, max_retries, bearer

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json", "user-agent": "adviverse-python/0.1.0"}
        if self._bearer: h["authorization"] = f"Bearer {self._key}"
        else: h["x-api-key"] = self._key
        return h

    def _request(self, method: str, path: str, *, query: dict | None = None, body: Any = None) -> Any:
        url = self._base + path
        if query:
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        if data is not None: headers["content-type"] = "application/json"
        last: Exception | None = None
        can_retry = method.upper() in _IDEMPOTENT
        for attempt in range(self._max_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode() or "null"
                    try: return json.loads(raw)
                    except json.JSONDecodeError as je:
                        raise AdviverseError(f"invalid JSON in response: {je}") from je
            except urllib.error.HTTPError as e:
                raw = e.read().decode() or ""
                try: parsed = json.loads(raw)
                except Exception: parsed = {"message": raw}
                msg = parsed.get("message") or parsed.get("error") or e.reason
                if can_retry and e.code in _RETRY_STATUS and attempt < self._max_retries:
                    delay = min(2 ** attempt * 0.2, 5)
                    if e.code == 429:
                        ra = e.headers.get("Retry-After") if e.headers else None
                        try: delay = min(float(ra), 10.0) if ra is not None else delay
                        except ValueError: pass
                    last = e; time.sleep(delay); continue
                if e.code in (401, 403): raise AdviverseAuthError(msg) from e
                if e.code == 429: raise AdviverseRateLimitError(msg) from e
                raise AdviverseApiError(e.code, msg, parsed) from e
            except socket.timeout as e:
                if can_retry and attempt < self._max_retries: last = e; time.sleep(min(2 ** attempt * 0.2, 5)); continue
                raise AdviverseError(f"request timed out: {e}") from e
            except urllib.error.URLError as e:
                if can_retry and attempt < self._max_retries: last = e; time.sleep(min(2 ** attempt * 0.2, 5)); continue
                raise AdviverseError(str(e)) from e
        raise AdviverseError(str(last))

    def paginate(self, method: str, path: str, *, query: dict | None = None) -> Iterator[Any]:
        q = dict(query or {}); seen_cursor = None
        while True:
            page = self._request(method, path, query=q)
            items = page.get("data") if isinstance(page, dict) else page
            if not items: return
            for it in items: yield it
            cursor = page.get("cursor") or page.get("next") if isinstance(page, dict) else None
            if not cursor or cursor == seen_cursor: return   # terminates on empty/absent/repeat cursor
            seen_cursor = cursor; q["cursor"] = cursor
