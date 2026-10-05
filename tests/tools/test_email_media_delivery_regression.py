"""Regression tests: send_message MEDIA attachments reach the email platform.

The defect these pin: email was only in the text-only sender table, so a
``MEDIA:`` tag was silently dropped with a "media delivery is currently only
supported for ..." warning. Both the ROUTING and the standalone SMTP attach step
are exercised through the real ``_send_to_platform`` dispatch with the descriptor
shape send_message actually produces — ``(path, is_voice)`` tuples from
``BasePlatformAdapter.extract_media``. A route-table shape check alone would stay
green while every attachment was skipped (the tuple-vs-str bug).
"""

import base64

import pytest

from gateway.config import Platform
from tools.send_message_tool import _send_to_platform


class _FakeSMTP:
    """Captures the MIME message the adapter would hand to SMTP."""

    def __init__(self):
        self.msg = None

    def login(self, *a, **k):
        pass

    def send_message(self, msg):
        self.msg = msg

    def quit(self):
        pass


class _PConfig:
    """Minimal PlatformConfig stand-in (the adapter reads ``extra`` + secrets)."""

    def __init__(self):
        self.extra = {
            "address": "me@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
        }


@pytest.fixture
def _email_smtp_capture(monkeypatch):
    """Route email standalone sends into a capturing fake SMTP server."""
    from plugins.platforms.email import adapter as email_adapter

    server = _FakeSMTP()
    monkeypatch.setattr(email_adapter, "_open_smtp", lambda *a, **k: server)
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    return server


@pytest.mark.asyncio
async def test_media_descriptor_reaches_the_email_sender(tmp_path, monkeypatch):
    """A real ``(path, is_voice)`` descriptor must reach the email sender
    through ``_send_to_platform`` instead of being dropped on the text path."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins

    discover_plugins()
    entry = platform_registry.get("email")
    assert entry is not None, "email platform plugin must register a registry entry"

    attachment = tmp_path / "note.txt"
    attachment.write_bytes(b"hello attachment")

    received = {}

    async def _spy(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
        received["media_files"] = media_files
        return {"success": True, "platform": "email", "chat_id": chat_id, "media_delivered": True}

    original = entry.standalone_sender_fn
    entry.standalone_sender_fn = _spy
    # No live gateway in-process -> the standalone (cron/out-of-process) route.
    monkeypatch.setattr("tools.send_message_tool._live_adapter", lambda platform, **kw: (None, None))
    try:
        result = await _send_to_platform(
            Platform.EMAIL, _PConfig(), "you@example.com", "here is the file",
            media_files=[(str(attachment), False)],
        )
    finally:
        entry.standalone_sender_fn = original

    assert received.get("media_files") == [(str(attachment), False)], received
    assert "omitted" not in str(result), result


@pytest.mark.asyncio
async def test_standalone_send_attaches_real_descriptor(tmp_path, _email_smtp_capture):
    """The standalone SMTP sender must unpack the ``(path, is_voice)`` descriptor
    send_message actually passes and attach the file."""
    from plugins.platforms.email import adapter as email_adapter

    attachment = tmp_path / "note.txt"
    attachment.write_bytes(b"hello attachment")

    result = await email_adapter._standalone_send(
        _PConfig(), "you@example.com", "here is the file", media_files=[(str(attachment), False)]
    )

    assert result.get("success") is True, result
    payload = _email_smtp_capture.msg.as_string()
    assert "note.txt" in payload
    assert base64.b64encode(b"hello attachment").decode() in payload


@pytest.mark.asyncio
async def test_standalone_send_skips_unattachable_media(tmp_path, _email_smtp_capture):
    """A missing file is skipped-and-logged, never crashing the whole send."""
    from plugins.platforms.email import adapter as email_adapter

    result = await email_adapter._standalone_send(
        _PConfig(), "you@example.com", "body text",
        media_files=[(str(tmp_path / "missing.bin"), False)],
    )

    assert result.get("success") is True, result
    assert "missing.bin" not in _email_smtp_capture.msg.as_string()
