"""Per-job email Subject: store contract, delivery plumbing, and the adapter.

The capability under test: a cron job may carry its own ``subject``, and every
job of one family can be given the SAME title so a recurring routine is
recognisable in the inbox instead of arriving under the thread's generic
subject. Contract, in the order the value travels:

- Job store (``cron/jobs.py``): ``subject`` is a plain optional text field —
  created, updated, and cleared by an empty string. A job without one keeps a
  record byte-identical to a pre-feature job (no key at all).
- Title resolution (``cron/scheduler_delivery.py::_job_subject``): an explicit
  ``subject`` wins outright; otherwise the job NAME is used while
  ``cron.subject_from_name`` is on (default on); a malformed config section
  falls back to the default instead of killing the run. Every string is
  redacted like the delivery content, because it lands in a mail header.
- Transport (``_live_route_metadata`` / the standalone lane): the title rides
  ``metadata["subject"]`` on BOTH the text and the media route, and on the
  standalone lane, so a job's title never depends on which lane delivered it.
- Adapter (``plugins/platforms/email/adapter.py``): ``metadata["subject"]``
  replaces the thread-derived subject while KEEPING the ``Re:`` prefix and
  leaving the threading headers (In-Reply-To / References) alone, so the mail
  lands under the new title inside the same thread. Non-ASCII titles are
  RFC 2047 encoded rather than written raw into the header.

A per-job title that silently dropped the mail out of its thread, or that
rendered as mojibake for a Hebrew title, would be worse than no title at all.
"""

import asyncio
import base64
import logging
import os
from concurrent.futures import Future
from email.header import decode_header
from unittest.mock import MagicMock, patch

import pytest

from cron.jobs import create_job, load_jobs, update_job
from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate the cron store (same pattern as tests/cron/test_cron_reasoning_effort.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path / "cron"


def _create(**kw):
    kw.setdefault("prompt", "say hi")
    kw.setdefault("schedule", "every 1h")
    return create_job(**kw)


class TestJobStoreSubject:

    def test_subject_stored_and_round_trips(self, tmp_cron_dir):
        job = _create(subject="NightCafe Daily Credits")
        assert job["subject"] == "NightCafe Daily Credits"
        assert load_jobs()[0]["subject"] == "NightCafe Daily Credits"

    def test_absent_subject_writes_no_key(self, tmp_cron_dir):
        """An untouched job record must stay byte-identical to a pre-feature one."""
        _create()
        assert "subject" not in load_jobs()[0]

    @pytest.mark.parametrize("empty", [None, "", "   "])
    def test_blank_subject_means_unset(self, tmp_cron_dir, empty):
        job = _create(subject=empty)
        assert job.get("subject") is None

    def test_subject_is_stripped(self, tmp_cron_dir):
        job = _create(subject="  Digests  ")
        assert job["subject"] == "Digests"

    def test_hebrew_subject_is_stored_verbatim(self, tmp_cron_dir):
        job = _create(subject="יצירת תמונות")
        assert job["subject"] == "יצירת תמונות"
        assert load_jobs()[0]["subject"] == "יצירת תמונות"

    def test_update_sets_subject(self, tmp_cron_dir):
        job = _create()
        updated = update_job(job["id"], {"subject": "Job Market"})
        assert updated["subject"] == "Job Market"
        assert load_jobs()[0]["subject"] == "Job Market"

    def test_update_empty_string_clears(self, tmp_cron_dir):
        job = _create(subject="Job Market")
        updated = update_job(job["id"], {"subject": ""})
        assert updated.get("subject") is None

    def test_subject_is_transferable_across_stores(self, tmp_cron_dir):
        """``job_definition`` carries authored fields between profiles: the new
        field must be in JOB_DEFINITION_FIELDS or it is silently dropped."""
        from cron.job_definition import JOB_DEFINITION_FIELDS

        assert "subject" in JOB_DEFINITION_FIELDS


# ---------------------------------------------------------------------------
# Title resolution
# ---------------------------------------------------------------------------


class TestJobSubjectResolution:

    JOB = {"id": "abc123", "name": "Yad2 printer watch"}

    def _resolve(self, job, cron_cfg=None):
        from cron import scheduler_delivery as sched_delivery

        cfg = {"cron": cron_cfg} if cron_cfg is not None else {"cron": {}}
        with patch("cron.scheduler.load_config", return_value=cfg):
            return sched_delivery._job_subject(job)

    def test_explicit_subject_wins_over_name(self):
        assert self._resolve({**self.JOB, "subject": "Printer watch"}) == "Printer watch"

    def test_falls_back_to_job_name_by_default(self):
        assert self._resolve(self.JOB) == "Yad2 printer watch"

    def test_job_name_fallback_is_on_with_no_cron_section(self):
        from cron import scheduler_delivery as sched_delivery

        with patch("cron.scheduler.load_config", return_value={}):
            assert sched_delivery._job_subject(self.JOB) == "Yad2 printer watch"

    def test_name_fallback_can_be_disabled(self):
        assert self._resolve(self.JOB, {"subject_from_name": False}) == ""

    def test_malformed_section_keeps_the_default(self):
        """`cron.subject_from_name` with no value / a bad shape must not kill the run."""
        from cron import scheduler_delivery as sched_delivery

        for bad in ({"subject_from_name": None}, {"subject_from_name": "yes"}):
            with patch("cron.scheduler.load_config", return_value={"cron": bad}):
                assert sched_delivery._job_subject(self.JOB) == "Yad2 printer watch"

    def test_config_load_failure_keeps_the_default(self):
        from cron import scheduler_delivery as sched_delivery

        with patch("cron.scheduler.load_config", side_effect=RuntimeError("boom")):
            assert sched_delivery._job_subject(self.JOB) == "Yad2 printer watch"

    def test_nameless_job_yields_nothing(self):
        assert self._resolve({"id": "abc123"}) == ""

    def test_explicit_subject_survives_name_fallback_disabled(self):
        job = {**self.JOB, "subject": "Printer watch"}
        assert self._resolve(job, {"subject_from_name": False}) == "Printer watch"

    def test_subject_is_redacted(self):
        """The title lands in a mail HEADER: a job that smuggled a credential
        into its subject must not put it on the wire."""
        out = self._resolve({**self.JOB, "subject": "key sk-abcdefghijklmnopqrstuvwxyz012345"})
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out

    def test_name_fallback_is_redacted_too(self):
        out = self._resolve({"id": "abc123", "name": "sk-abcdefghijklmnopqrstuvwxyz012345"})
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out


# ---------------------------------------------------------------------------
# Delivery plumbing: live route metadata + the standalone lane
# ---------------------------------------------------------------------------

CHAT_ID = "-1001234567890"


def _job(subject=None, name="Yad2 printer watch"):
    job = {
        "id": "92e639af907f",
        "name": name,
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": CHAT_ID},
    }
    if subject is not None:
        job["subject"] = subject
    return job


def _gateway_config():
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: None
    return config


def _run(job, content, send_result, media=None):
    """Drive ``_deliver_result`` over the live lane with a stubbed router.

    Returns ``(router_calls, media_metadata)``. Mirrors the harness in
    tests/cron/test_cron_live_delivery_confirmation.py.
    """
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        return future

    router_calls = []
    media_calls = []

    router = MagicMock()

    async def _deliver_to_platform(target, text, metadata, transport=None):
        router_calls.append({"target": target, "text": text, "metadata": metadata})
        return send_result

    router._deliver_to_platform = _deliver_to_platform

    def fake_send_media(adapter, chat_id, media_files, metadata, loop, job, platform=None):
        media_calls.append(metadata)
        return []

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config",
               return_value={"cron": {"wrap_response": False}}), \
         patch("cron.scheduler_delivery._record_delivery_verification"), \
         patch("gateway.delivery.DeliveryRouter", return_value=router), \
         patch("cron.scheduler_delivery._send_media_via_adapter", side_effect=fake_send_media), \
         patch("gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
               side_effect=lambda files: files), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        adapters = {Platform.TELEGRAM: MagicMock()}
        from cron.scheduler import _deliver_result
        _deliver_result(job, content, adapters=adapters, loop=loop)
    return router_calls, media_calls


class _SendResult:
    def __init__(self, success=True, message_id=1, **extra):
        self.success = success
        self.message_id = message_id
        for key, value in extra.items():
            setattr(self, key, value)


class TestLiveRouteMetadataCarriesSubject:

    def test_text_route_metadata_carries_the_job_subject(self):
        router_calls, _ = _run(_job(subject="Printer watch"), "found one", _SendResult())
        assert router_calls[0]["metadata"]["subject"] == "Printer watch"

    def test_text_route_falls_back_to_the_job_name(self):
        router_calls, _ = _run(_job(), "found one", _SendResult())
        assert router_calls[0]["metadata"]["subject"] == "Yad2 printer watch"

    def test_media_route_matches_the_text_route(self, tmp_path):
        media = tmp_path / "report.png"
        media.write_bytes(b"\x89PNG\r\n\x1a\n")
        router_calls, media_calls = _run(
            _job(subject="Printer watch"), f"found one\nMEDIA:{media}", _SendResult())
        assert router_calls[0]["metadata"]["subject"] == "Printer watch"
        assert media_calls[0]["subject"] == "Printer watch"

    def test_routing_keys_are_untouched(self):
        router_calls, _ = _run(_job(subject="Printer watch"), "found one", _SendResult())
        metadata = router_calls[0]["metadata"]
        assert metadata["job_id"] == "92e639af907f"
        assert metadata["notify"] is True


class TestStandaloneLaneCarriesSubject:

    @staticmethod
    def _deliver(job, sender):
        with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
             patch("cron.scheduler.load_config",
                   return_value={"cron": {"wrap_response": False}}), \
             patch("cron.scheduler_delivery._record_delivery_verification"), \
             patch("tools.send_message_tool._send_to_platform", sender):
            from cron.scheduler import _deliver_result
            return _deliver_result(job, "found one")

    def test_subject_reaches_the_standalone_sender(self):
        seen = {}

        async def _spy(platform, pconfig, chat_id, text, **kwargs):
            seen.update(kwargs)
            return {"success": True, "message_id": 7}

        error = self._deliver(_job(subject="Printer watch"), _spy)
        assert error is None
        assert seen["subject"] == "Printer watch"

    def test_name_fallback_reaches_the_standalone_sender(self):
        seen = {}

        async def _spy(platform, pconfig, chat_id, text, **kwargs):
            seen.update(kwargs)
            return {"success": True, "message_id": 7}

        self._deliver(_job(), _spy)
        assert seen["subject"] == "Yad2 printer watch"

    def test_subject_is_omitted_when_the_fallback_is_disabled(self):
        seen = {}

        async def _spy(platform, pconfig, chat_id, text, **kwargs):
            seen.update(kwargs)
            return {"success": True, "message_id": 7}

        with patch("cron.scheduler.load_config",
                   return_value={"cron": {"wrap_response": False, "subject_from_name": False}}):
            from cron.scheduler import _deliver_result
            _deliver_result(_job(), "found one")
        assert seen == {}  # the send never happened with the gate shut

    def test_standalone_sender_that_takes_no_subject_is_called_as_before(self):
        """The registry contract does not include `subject`; an older/other
        plugin's sender must be called exactly as it was.

        The signature filter lives in ``_call_standalone_sender`` — the router
        (``_send_to_platform``) legitimately receives ``subject=`` from the scheduler.
        """
        from tools.send_message_tool import _call_standalone_sender

        seen = {}

        async def _no_subject_sender(pconfig, chat_id, text, *, thread_id=None,
                                     media_files=None, force_document=False):
            seen["thread_id"] = thread_id
            return {"success": True}

        async def _with_subject_sender(pconfig, chat_id, text, *, subject=None, **kwargs):
            seen["subject"] = subject
            return {"success": True}

        # A sender whose signature predates the subject parameter is called without it.
        assert asyncio.run(_call_standalone_sender(
            _no_subject_sender, object(), "a@b.com", "body",
            thread_id="t1", subject="Printer watch")).get("success") is True
        assert "subject" not in seen and seen["thread_id"] == "t1"

        # A sender that declares it receives it.
        assert asyncio.run(_call_standalone_sender(
            _with_subject_sender, object(), "a@b.com", "body",
            subject="Printer watch")).get("success") is True
        assert seen["subject"] == "Printer watch"


# ---------------------------------------------------------------------------
# Email adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def _email_env(monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", "hermes@test.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.test.com")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.test.com")


def _adapter():
    from plugins.platforms.email.adapter import EmailAdapter

    return EmailAdapter(PlatformConfig(enabled=True))


def _sent_message(adapter, **send_kwargs):
    server = MagicMock()
    with patch("smtplib.SMTP", return_value=server):
        asyncio.run(adapter.send(**send_kwargs))
    return server.send_message.call_args[0][0]


class TestEmailAdapterSubjectOverride:

    def test_metadata_subject_replaces_the_thread_subject(self, _email_env):
        adapter = _adapter()
        adapter._thread_context["<root@x>"] = {
            "subject": "Project question", "message_id": "<root@x>", "references": "<root@x>"}
        sent = _sent_message(
            adapter, chat_id="a@b.com", content="body",
            metadata={"thread_id": "<root@x>", "subject": "Printer watch"})
        assert sent["Subject"] == "Re: Printer watch"

    def test_threading_headers_are_unaffected(self, _email_env):
        """A new title must not knock the mail out of its thread."""
        adapter = _adapter()
        adapter._thread_context["<root@x>"] = {
            "subject": "Project question", "message_id": "<root@x>", "references": "<root@x>"}
        sent = _sent_message(
            adapter, chat_id="a@b.com", content="body",
            metadata={"thread_id": "<root@x>", "subject": "Printer watch"})
        assert sent["In-Reply-To"] == "<root@x>"
        assert sent["References"] == "<root@x>"

    def test_absent_metadata_subject_keeps_the_thread_subject(self, _email_env):
        adapter = _adapter()
        adapter._thread_context["<root@x>"] = {
            "subject": "Project question", "message_id": "<root@x>", "references": "<root@x>"}
        sent = _sent_message(adapter, chat_id="a@b.com", content="body",
                             metadata={"thread_id": "<root@x>"})
        assert sent["Subject"] == "Re: Project question"

    def test_blank_metadata_subject_falls_back(self, _email_env):
        adapter = _adapter()
        adapter._thread_context["<root@x>"] = {
            "subject": "Project question", "message_id": "<root@x>", "references": "<root@x>"}
        sent = _sent_message(adapter, chat_id="a@b.com", content="body",
                             metadata={"thread_id": "<root@x>", "subject": "   "})
        assert sent["Subject"] == "Re: Project question"

    def test_hebrew_subject_is_rfc2047_encoded_not_raw(self, _email_env):
        """A raw Hebrew header byte-stream renders as mojibake (or drops the
        line). It must arrive as an encoded-word a client can decode."""
        adapter = _adapter()
        sent = _sent_message(adapter, chat_id="a@b.com", content="body",
                             metadata={"subject": "יצירת תמונות"})
        raw = sent["Subject"]
        assert raw.isascii(), raw
        parts = decode_header(raw)
        assert "".join(
            chunk.decode(enc or "ascii") if isinstance(chunk, bytes) else chunk
            for chunk, enc in parts) == "Re: יצירת תמונות"

    def test_document_send_carries_the_subject(self, _email_env, tmp_path):
        adapter = _adapter()
        attachment = tmp_path / "note.txt"
        attachment.write_bytes(b"hello")
        server = MagicMock()
        with patch("smtplib.SMTP", return_value=server):
            asyncio.run(adapter.send_document(
                "a@b.com", str(attachment), metadata={"subject": "Printer watch"}))
        assert server.send_message.call_args[0][0]["Subject"] == "Re: Printer watch"


class TestEmailStandaloneSenderSubject:

    def test_standalone_send_uses_the_subject_kwarg(self, _email_env):
        from plugins.platforms.email import adapter as email_adapter

        server = MagicMock()
        with patch.object(email_adapter, "_open_smtp", return_value=server):
            result = asyncio.run(email_adapter._standalone_send(
                PlatformConfig(enabled=True, extra={
                    "address": "me@example.com", "smtp_host": "smtp.example.com"}),
                "you@example.com", "body", subject="Printer watch"))
        assert result.get("success") is True, result
        assert server.send_message.call_args[0][0]["Subject"] == "Printer watch"

    def test_standalone_send_defaults_without_a_subject(self, _email_env):
        from plugins.platforms.email import adapter as email_adapter

        server = MagicMock()
        with patch.object(email_adapter, "_open_smtp", return_value=server):
            asyncio.run(email_adapter._standalone_send(
                PlatformConfig(enabled=True, extra={
                    "address": "me@example.com", "smtp_host": "smtp.example.com"}),
                "you@example.com", "body"))
        assert server.send_message.call_args[0][0]["Subject"] == "Hermes Agent"

    def test_standalone_send_encodes_a_hebrew_subject(self, _email_env):
        from plugins.platforms.email import adapter as email_adapter

        server = MagicMock()
        with patch.object(email_adapter, "_open_smtp", return_value=server):
            asyncio.run(email_adapter._standalone_send(
                PlatformConfig(enabled=True, extra={
                    "address": "me@example.com", "smtp_host": "smtp.example.com"}),
                "you@example.com", "body", subject="יצירת תמונות"))
        raw = server.send_message.call_args[0][0]["Subject"]
        assert raw.isascii(), raw
        assert base64.b64encode("יצירת תמונות".encode()).decode().rstrip("=") in raw


class TestHeaderValueHelper:

    def test_ascii_passes_through_unchanged(self):
        from plugins.platforms.email.adapter import _header_value

        assert _header_value("Re: Printer watch") == "Re: Printer watch"

    def test_non_ascii_becomes_a_decodable_encoded_word(self):
        from plugins.platforms.email.adapter import _header_value

        encoded = _header_value("שלום")
        assert encoded.isascii()
        chunk, charset = decode_header(encoded)[0]
        assert chunk.decode(charset) == "שלום"


# ---------------------------------------------------------------------------
# Agent-facing tool
# ---------------------------------------------------------------------------


class TestAgentToolSubject:

    def test_create_forwards_subject(self, tmp_cron_dir):
        import json

        from tools.cronjob_tools import cronjob

        out = json.loads(cronjob(action="create", prompt="digest", schedule="every 1h",
                                 subject="Printer watch"))
        assert out["success"] is True
        assert load_jobs()[0]["subject"] == "Printer watch"

    def test_update_forwards_subject(self, tmp_cron_dir):
        import json

        from tools.cronjob_tools import cronjob

        created = json.loads(cronjob(action="create", prompt="digest", schedule="every 1h"))
        json.loads(cronjob(action="update", job_id=created["job_id"], subject="Job Market"))
        assert load_jobs()[0]["subject"] == "Job Market"

    def test_update_empty_string_clears(self, tmp_cron_dir):
        import json

        from tools.cronjob_tools import cronjob

        created = json.loads(cronjob(action="create", prompt="digest", schedule="every 1h",
                                     subject="Job Market"))
        json.loads(cronjob(action="update", job_id=created["job_id"], subject=""))
        # '' clears the override (falls back to the job name); the key is normalised to None,
        # matching the reasoning_effort / failure_deliver clear contract — never a stale title.
        assert load_jobs()[0].get("subject") is None

    def test_list_surfaces_the_field_only_when_set(self, tmp_cron_dir):
        import json

        from tools.cronjob_tools import cronjob

        json.loads(cronjob(action="create", prompt="digest", schedule="every 1h",
                           subject="Printer watch"))
        json.loads(cronjob(action="create", prompt="other", schedule="every 1h"))
        jobs = json.loads(cronjob(action="list"))["jobs"]
        titled = [j for j in jobs if j.get("subject")]
        assert titled and titled[0]["subject"] == "Printer watch"
        assert any("subject" not in j for j in jobs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cron_parser():
    """The real ``hermes`` argparser with the ``cron`` subtree attached.

    ``build_cron_parser`` is the shipped builder (``hermes_cli/subcommands/cron.py``);
    it takes the parent subparsers and the dispatch callable, same as ``hermes_cli/main.py``.
    """
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    return parser


class TestCliSubjectFlag:

    def test_create_parser_accepts_subject(self):
        args = _cron_parser().parse_args(
            ["cron", "create", "every 1h", "digest", "--subject", "Printer watch"])
        assert args.subject == "Printer watch"

    def test_edit_parser_accepts_subject(self):
        args = _cron_parser().parse_args(
            ["cron", "edit", "abc123", "--subject", "Printer watch"])
        assert args.subject == "Printer watch"

    def test_subject_reaches_the_create_call(self):
        from hermes_cli import cron as cron_cli

        captured = {}

        def _fake_api(**kwargs):
            captured.update(kwargs)
            return {"success": True, "job_id": "j1", "name": "digest", "schedule": "every 1h",
                    "next_run_at": "2026-10-07T09:00:00+00:00"}

        args = _cron_parser().parse_args(
            ["cron", "create", "every 1h", "digest", "--subject", "Printer watch"])
        with patch.object(cron_cli, "_cron_api", _fake_api), \
             patch.object(cron_cli, "_warn_if_gateway_not_running"):
            assert cron_cli.cron_create(args) == 0
        assert captured["subject"] == "Printer watch"


class TestConfigDefaultDocumented:

    def test_subject_from_name_defaults_on(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["cron"]["subject_from_name"] is True
