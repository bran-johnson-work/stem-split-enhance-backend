"""Unit tests for the low-level Resend email transport.

No network: ``httpx.AsyncClient`` is replaced with a fake, and settings are
patched to toggle the "email enabled" state.
"""

import httpx
import pytest

from app.core import email as email_module
from app.core.config import Settings


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that records the POST and returns/raises."""

    last_post: dict = {}

    def __init__(self, *, response=None, exc=None):
        self._response = response
        self._exc = exc

    def __call__(self, *args, **kwargs):  # httpx.AsyncClient(timeout=...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_post = {"url": url, "json": json, "headers": headers}
        if self._exc is not None:
            raise self._exc
        return self._response


def _enable_email(monkeypatch, **overrides):
    settings = Settings(
        resend_api_key="re_test_key",
        email_from="from@example.com",
        email_from_name="Tester",
        **overrides,
    )
    monkeypatch.setattr(email_module, "get_settings", lambda: settings)
    return settings


async def test_send_email_disabled_is_noop(monkeypatch):
    # Explicit empty key => disabled. (An explicit kwarg overrides any .env value,
    # so this stays a no-op even when a real RESEND_API_KEY is configured.)
    monkeypatch.setattr(email_module, "get_settings", lambda: Settings(resend_api_key=""))
    result = await email_module.send_email(
        to="user@example.com", subject="Hi", html="<p>hi</p>"
    )
    assert result is None


async def test_send_email_success_returns_message_id(monkeypatch):
    _enable_email(monkeypatch)
    fake = _FakeAsyncClient(response=_FakeResponse(200, {"id": "msg_123"}))
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    result = await email_module.send_email(
        to="user@example.com",
        subject="Verify",
        html="<p>link</p>",
        text="link",
        reply_to="support@example.com",
    )

    assert result == "msg_123"
    sent = _FakeAsyncClient.last_post
    assert sent["url"].endswith("/emails")
    assert sent["json"]["from"] == "Tester <from@example.com>"
    assert sent["json"]["to"] == ["user@example.com"]
    assert sent["json"]["subject"] == "Verify"
    assert sent["json"]["text"] == "link"
    assert sent["json"]["reply_to"] == "support@example.com"
    assert sent["headers"]["Authorization"] == "Bearer re_test_key"


async def test_send_email_accepts_multiple_recipients(monkeypatch):
    _enable_email(monkeypatch)
    fake = _FakeAsyncClient(response=_FakeResponse(200, {"id": "msg_x"}))
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    await email_module.send_email(
        to=["a@example.com", "b@example.com"], subject="Hi", html="<p>hi</p>"
    )
    assert _FakeAsyncClient.last_post["json"]["to"] == ["a@example.com", "b@example.com"]


async def test_send_email_api_error_raises(monkeypatch):
    _enable_email(monkeypatch)
    fake = _FakeAsyncClient(response=_FakeResponse(422, text="invalid from"))
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    with pytest.raises(email_module.EmailError):
        await email_module.send_email(to="user@example.com", subject="Hi", html="<p>hi</p>")


async def test_send_email_transport_error_raises(monkeypatch):
    _enable_email(monkeypatch)
    fake = _FakeAsyncClient(exc=httpx.ConnectError("boom"))
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    with pytest.raises(email_module.EmailError):
        await email_module.send_email(to="user@example.com", subject="Hi", html="<p>hi</p>")
