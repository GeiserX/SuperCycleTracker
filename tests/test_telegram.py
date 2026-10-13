import io
import json
import traceback
import urllib.error

import pytest

from supercycletracker.telegram import Telegram, TelegramError, safe

TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeHTTP:
    """Stands in for urlopen: replays scripted answers and records each request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
        return Response(body)


def client(*answers):
    http, sleeps = FakeHTTP(*answers), []
    return Telegram(TOKEN, urlopen=http, sleep=sleeps.append), http, sleeps


def too_many(retry_after):
    return (
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": f"Too Many Requests: retry after {retry_after}",
            "parameters": {"retry_after": retry_after},
        },
    )


def assert_clean(err: BaseException):
    texts = [str(err), repr(err), safe(err), "".join(traceback.format_exception(err))]
    for text in texts:
        assert TOKEN not in text
        assert "api.telegram.org" not in text


def test_call_posts_json_and_returns_the_result():
    tg, http, _ = client((200, {"ok": True, "result": {"message_id": 7}}))
    assert tg.call("sendMessage", chat_id=-100123, text="hola", reply_markup=None) == {
        "message_id": 7
    }
    request, _ = http.requests[0]
    assert request.full_url.endswith("/sendMessage")
    assert json.loads(request.data) == {"chat_id": -100123, "text": "hola"}
    assert request.get_header("Content-type") == "application/json"
    assert TOKEN not in repr(tg)


def test_a_long_poll_gets_a_longer_http_timeout():
    tg, http, _ = client((200, {"ok": True, "result": []}))
    tg.call("getUpdates", timeout=20, allowed_updates=["message", "callback_query"])
    assert http.requests[0][1] > 20


def test_429_honours_retry_after():
    tg, http, sleeps = client(too_many(7), (200, {"ok": True, "result": True}))
    assert tg.call("answerCallbackQuery", callback_query_id="1") is True
    assert sleeps == [7]
    assert len(http.requests) == 2


def test_429_wait_is_capped_and_retried_twice_at_most():
    tg, http, sleeps = client(too_many(500), too_many(3), too_many(3))
    with pytest.raises(TelegramError) as info:
        tg.call("sendMessage", chat_id=1, text="x")
    assert sleeps == [60, 3]
    assert len(http.requests) == 3
    assert info.value.code == 429 and info.value.retry_after == 3


def test_429_in_a_200_body_is_handled_the_same():
    _, payload = too_many(2)
    tg, _, sleeps = client((200, payload), (200, {"ok": True, "result": 1}))
    assert tg.call("getMe") == 1
    assert sleeps == [2]


def test_errors_keep_code_description_and_parameters():
    tg, _, sleeps = client(
        (
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"},
        ),
        (
            400,
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -1009876},
            },
        ),
        (
            409,
            {
                "ok": False,
                "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request",
            },
        ),
    )
    with pytest.raises(TelegramError) as info:
        tg.call("editMessageText", chat_id=1, message_id=2, text="x")
    assert info.value.code == 400 and "message is not modified" in info.value.description
    with pytest.raises(TelegramError) as info:
        tg.call("sendMessage", chat_id=1, text="x")
    assert info.value.migrate_to_chat_id == -1009876
    with pytest.raises(TelegramError) as info:
        tg.call("getUpdates", timeout=20)
    assert info.value.code == 409
    assert sleeps == []


@pytest.mark.parametrize(
    "answer",
    [
        (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
        (502, b"<html>Bad Gateway</html>"),
        urllib.error.URLError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getMe"),
        TimeoutError(f"timed out on https://api.telegram.org/bot{TOKEN}/getMe"),
        ConnectionResetError(f"reset by https://api.telegram.org/bot{TOKEN}/getMe"),
    ],
)
def test_error_text_never_holds_the_token(answer):
    tg, _, _ = client(answer)
    with pytest.raises(TelegramError) as info:
        tg.call("getMe")
    assert_clean(info.value)


def test_error_codes_for_network_and_gateway_failures():
    tg, _, _ = client(TimeoutError("x"), (502, b"<html>Bad Gateway</html>"))
    with pytest.raises(TelegramError) as info:
        tg.call("getUpdates", timeout=20)
    assert info.value.code == 0 and info.value.description == "TimeoutError"
    with pytest.raises(TelegramError) as info:
        tg.call("getUpdates", timeout=20)
    assert info.value.code == 502


def test_safe_strips_the_token_and_foreign_messages():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    http_error = urllib.error.HTTPError(url, 500, "boom", {}, None)
    assert safe(http_error) == "HTTPError 500"
    assert safe(ValueError("me ha bajado hoy")) == "ValueError"  # a foreign message may quote her
    assert safe(RuntimeError(url)) == "RuntimeError"
    assert TOKEN not in safe(TelegramError("getMe", 0, f"echo {TOKEN}"))
    assert (
        safe(TelegramError("getMe", 401, "Unauthorized"))
        == "TelegramError: getMe: 401 Unauthorized"
    )


def test_safe_check_can_fail():
    """Positive control: the assertion helper above does catch a token in an error."""
    with pytest.raises(AssertionError):
        assert_clean(RuntimeError(f"https://api.telegram.org/bot{TOKEN}/getMe"))
