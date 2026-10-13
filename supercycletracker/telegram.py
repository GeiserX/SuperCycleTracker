"""A Telegram Bot API client on the standard library.

The token sits in every request URL, so nothing here ever puts the URL, or an exception that may
quote it, into an error: `TelegramError` carries the method, Telegram's error code and its
description, and `safe()` turns any exception into text that is fit for the service log.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

API = "https://api.telegram.org"
RETRIES_429 = 2  # a 429 is retried this many times, after Telegram's own retry_after
MAX_RETRY_AFTER = 60
HTTP_MARGIN_S = 15  # on top of a long poll's own timeout
DOWNLOAD_TIMEOUT_S = 60  # a file download (a voice note): at most 20 MiB from Telegram

_TOKEN = re.compile(r"\d{3,}:[A-Za-z0-9_-]{20,}")


class TelegramError(Exception):
    """A failed Bot API call. `code` is Telegram's error code (0 for a network failure)."""

    def __init__(self, method: str, code: int, description: str, parameters: dict | None = None):
        super().__init__(f"{method}: {code} {_TOKEN.sub('<token>', description)}")
        self.method = method
        self.code = code
        self.description = _TOKEN.sub("<token>", description)
        self.parameters = parameters or {}

    @property
    def retry_after(self) -> int | None:
        value = self.parameters.get("retry_after")
        return int(value) if isinstance(value, int | float) else None

    @property
    def migrate_to_chat_id(self) -> int | None:
        value = self.parameters.get("migrate_to_chat_id")
        return int(value) if isinstance(value, int) else None


def safe(e: BaseException) -> str:
    """An exception as text for the service log. The app's own exceptions carry reason codes and
    keep their text; anything else keeps only its class and HTTP status, because a library's or
    a plugin's message may quote a URL with the token, her words or the assistant's."""
    name, module = type(e).__name__, type(e).__module__
    if module == "supercycletracker" or module.startswith("supercycletracker."):
        text = f"{name}: {e}"
    else:
        status = getattr(e, "code", None)
        if not isinstance(status, int):
            status = getattr(e, "status", None)
        text = f"{name} {status}" if isinstance(status, int) else name
    return _TOKEN.sub("<token>", text)


class Telegram:
    def __init__(
        self,
        token: str,
        base: str = API,
        urlopen: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._url = f"{base}/bot{token.strip()}/"  # a secrets file often ends in a newline
        self._file_url = f"{base}/file/bot{token.strip()}/"
        self._urlopen = urlopen
        self._sleep = sleep

    def __repr__(self) -> str:
        return "Telegram()"

    def call(self, method: str, **params):
        """Call `method` with JSON params (None values dropped) and return its `result`.

        A 429 waits Telegram's `retry_after` (at most 60 s) and tries again, twice; any other
        failure raises `TelegramError` at once."""
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        timeout = int(params.get("timeout") or 0) + HTTP_MARGIN_S
        for attempt in range(RETRIES_429 + 1):
            payload = self._post(method, body, timeout)
            if payload.get("ok"):
                return payload.get("result")
            err = TelegramError(
                method,
                int(payload.get("error_code") or 0),
                str(payload.get("description") or ""),
                payload.get("parameters") if isinstance(payload.get("parameters"), dict) else None,
            )
            if err.code == 429 and attempt < RETRIES_429:
                self._sleep(min(err.retry_after or 1, MAX_RETRY_AFTER))
                continue
            raise err
        raise AssertionError("unreachable")  # pragma: no cover

    def download(self, file_path: str, limit: int | None = None) -> bytes:
        """The bytes of a file `getFile` returned, from Telegram's file URL (the token is in it,
        so its errors carry no URL, like `call`). More than `limit` bytes raises
        `TelegramError("download", 413, "file too large")` without reading the rest."""
        failure, status = None, 0
        try:
            request = urllib.request.Request(self._file_url + file_path)
            with self._urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
                raw = response.read() if limit is None else response.read(limit + 1)
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
            failure = f"HTTP {status}"
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            failure = type(e).__name__  # ValueError: a malformed URL, quoted in full
        if failure is not None:
            raise TelegramError("download", status, failure)
        if limit is not None and len(raw) > limit:
            raise TelegramError("download", 413, "file too large")
        return raw

    def _post(self, method: str, body: bytes, timeout: float) -> dict:
        """The response body as a dict. A body that is not a JSON object (an HTML page from a
        proxy) becomes `{ok: false, error_code: <status>}`, so `call` treats every 429 alike.

        No exception raised here carries another one: the original may quote the URL with the
        token (an `InvalidURL` does, when the token has a stray character), and `from None`
        would still leave it on `__context__`. So the failure is noted inside the `except` and
        raised after it."""
        status, failure = 0, None
        try:
            request = urllib.request.Request(
                self._url + method, data=body, headers={"Content-Type": "application/json"}
            )
            with self._urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            # Telegram answers errors with an HTTP status and a JSON body; read the body.
            status = e.code
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                e.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            failure = type(e).__name__  # ValueError: a malformed URL, quoted in full
        if failure is not None:
            raise TelegramError(method, 0, failure)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            description = f"HTTP {status}" if status else "bad response"
            return {"ok": False, "error_code": status, "description": description}
        return payload
