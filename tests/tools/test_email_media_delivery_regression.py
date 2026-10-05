"""Regression tests: send_message MEDIA attachments are delivered to email."""

import base64
import io

import pytest


def test_chunked_routes_include_email():
    from tools.send_message_tool import _CHUNKED_ROUTES

    assert "email" in _CHUNKED_ROUTES
    media_required, sentinel, sender = _CHUNKED_ROUTES["email"]
    assert media_required is True
    assert callable(sender)


@pytest.mark.asyncio
async def test_standalone_send_attaches_media_files(tmp_path, monkeypatch):
    from plugins.platforms.email import adapter as email_adapter

    class _PConfig:
        extra = {"address": "me@example.com", "smtp_host": "smtp.example.com", "smtp_port": 587}

    attachment = tmp_path / "note.txt"
    attachment.write_bytes(b"hello attachment")

    captured = {}

    class _FakeServer:
        def login(self, *a):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

        def quit(self):
            pass

    monkeypatch.setattr(email_adapter, "_open_smtp", lambda *a, **k: _FakeServer())
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    result = await email_adapter._standalone_send(
        _PConfig(), "you@example.com", "here is the file", media_files=[str(attachment)]
    )

    assert result.get("success") is True
    msg = captured["msg"]
    payload = msg.as_string()
    assert "note.txt" in payload
    assert base64.b64encode(b"hello attachment").decode() in payload


@pytest.mark.asyncio
async def test_standalone_send_skips_unattachable_media(tmp_path, monkeypatch):
    from plugins.platforms.email import adapter as email_adapter

    class _PConfig:
        extra = {"address": "me@example.com", "smtp_host": "smtp.example.com", "smtp_port": 587}

    captured = {}

    class _FakeServer:
        def login(self, *a):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

        def quit(self):
            pass

    monkeypatch.setattr(email_adapter, "_open_smtp", lambda *a, **k: _FakeServer())
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    result = await email_adapter._standalone_send(
        _PConfig(), "you@example.com", "body text", media_files=[str(tmp_path / "missing.bin")]
    )

    assert result.get("success") is True
    assert "missing.bin" not in captured["msg"].as_string()