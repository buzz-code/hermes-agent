"""RTL HTML alternative body part for outgoing email.

The gateway historically sent plain text only; Hebrew/Arabic readers got
left-to-right rendering in clients that do not auto-detect paragraph
direction. ``_attach_body`` now adds an HTML alternative part with
``dir="rtl"`` whenever the body contains RTL-script characters, across
all four outgoing send paths (reply, single/multi attachment, standalone).
"""

import os
from email.mime.multipart import MIMEMultipart
from unittest.mock import patch, MagicMock

from plugins.platforms.email.adapter import (
    _attach_body,
    _rtl_html_body_part,
)


class TestRtlHtmlBodyPart:
    def test_none_for_english_only_body(self):
        assert _rtl_html_body_part("Hello world") is None

    def test_none_for_empty_body(self):
        assert _rtl_html_body_part("") is None

    def test_html_part_for_hebrew_body(self):
        part = _rtl_html_body_part("שלום עולם")
        assert part is not None
        assert part.get_content_type() == "text/html"
        payload = part.get_payload(decode=True).decode("utf-8")
        assert 'dir="rtl"' in payload
        assert "שלום עולם" in payload

    def test_arabic_body_also_gets_rtl(self):
        assert _rtl_html_body_part("مرحبا") is not None

    def test_mixed_hebrew_english_gets_rtl(self):
        assert _rtl_html_body_part("Deploy finished, בדוק את הלוגים") is not None

    def test_body_is_html_escaped(self):
        part = _rtl_html_body_part("שלום <script>alert(1)</script>")
        payload = part.get_payload(decode=True).decode("utf-8")
        assert "<script>" not in payload
        assert "&lt;script&gt;" in payload


class TestAttachBody:
    def _content_types(self, body):
        msg = MIMEMultipart()
        _attach_body(msg, body)
        return [p.get_content_type() for p in msg.get_payload()]

    def test_plain_only_for_english(self):
        assert self._content_types("Hello") == ["text/plain"]

    def test_alternative_group_for_hebrew(self):
        assert self._content_types("שלום") == ["multipart/alternative"]

    def test_alternative_contains_plain_then_html(self):
        msg = MIMEMultipart()
        _attach_body(msg, "שלום עולם")
        alt = msg.get_payload()[0]
        assert alt.get_content_type() == "multipart/alternative"
        parts = alt.get_payload()
        assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
        plain_text = parts[0].get_payload(decode=True).decode("utf-8")
        assert plain_text == "שלום עולם"


class TestStandaloneSendRtl:
    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_SMTP_HOST": "smtp.test.com",
        "EMAIL_SMTP_PORT": "587",
    })
    def test_hebrew_message_gets_rtl_html_part(self):
        import asyncio
        from types import SimpleNamespace

        from plugins.platforms.email.adapter import _standalone_send

        with patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server

            async def run():
                return await _standalone_send(
                    SimpleNamespace(
                        token=None,
                        api_key=None,
                        extra={"address": "hermes@test.com",
                               "smtp_host": "smtp.test.com"},
                    ),
                    "user@test.com",
                    "שלום ותודה על הפרטים",
                )

            result = asyncio.run(run())
            assert result["success"] is True
            sent = mock_server.send_message.call_args[0][0]
            assert sent.is_multipart()
            outer = sent.get_payload()
            assert [p.get_content_type() for p in outer] == [
                "multipart/alternative",
            ]
            alt = outer[0]
            parts = alt.get_payload()
            assert [p.get_content_type() for p in parts] == [
                "text/plain", "text/html",
            ]
            html = parts[1].get_payload(decode=True).decode("utf-8")
            assert 'dir="rtl"' in html