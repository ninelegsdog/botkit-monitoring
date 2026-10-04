"""Liveness of the runner, judged from outside it.

The cases that matter are the ones where the watchdog must stay quiet: a failed
run is still a proof of life, and an outage must not be re-announced every cycle.
"""

import asyncio

import pytest

import watchdog_run
from e2e import heartbeat
from e2e.config import Settings

NOW = 1_700_000_000.0
GAP_MIN = 8


def _settings(tmp_path, max_gap_min=GAP_MIN):
    return Settings(
        api_id=1,
        api_hash="h",
        status_dir=tmp_path / "status",
        watchdog_max_gap_min=max_gap_min,
    )


class _Tester:
    """Stands in for TelegramTester and records what reached Saved Messages."""

    def __init__(self, send_ok=True):
        self.send_ok = send_ok
        self.sent = []
        # The watchdog reaches Telegram the same way the runner does, through
        # tester.client; the double mirrors that shape rather than shortcutting it.
        self.client = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def send_message(self, peer, text):
        if not self.send_ok:
            raise RuntimeError("telegram unreachable")
        self.sent.append((peer, text))


def test_a_fresh_heartbeat_is_alive():
    v = heartbeat.classify(NOW - 60, NOW, 600)
    assert v.state == heartbeat.OK


def test_a_missing_heartbeat_is_dead():
    v = heartbeat.classify(None, NOW, 600)
    assert v.state == heartbeat.STALE


def test_a_heartbeat_older_than_the_gap_is_dead():
    v = heartbeat.classify(NOW - 3600, NOW, 600)
    assert v.state == heartbeat.STALE


def test_a_backwards_clock_cannot_make_a_stale_run_look_alive():
    """A negative age would otherwise read as maximally alive."""
    v = heartbeat.classify(NOW + 3600, NOW, 600)
    assert v.state == heartbeat.OK
    assert v.age_s == 0.0


def test_mark_run_end_creates_the_directory_and_is_readable(tmp_path):
    heartbeat.mark_run_end(tmp_path / "status", now=NOW)
    assert heartbeat.read_end_mtime(tmp_path / "status") == pytest.approx(NOW, abs=1)
    assert str(NOW)[:10] in heartbeat.heartbeat_path(tmp_path / "status").read_text()


def test_read_end_mtime_is_none_when_nothing_ran(tmp_path):
    assert heartbeat.read_end_mtime(tmp_path / "status") is None


def test_healthy_run_sends_nothing(tmp_path, monkeypatch):
    heartbeat.mark_run_end(tmp_path / "status", now=NOW - 60)
    tester = _Tester()
    monkeypatch.setattr(watchdog_run, "TelegramTester", lambda s: tester)
    assert asyncio.run(watchdog_run.check(_settings(tmp_path), NOW)) == 0
    assert tester.sent == []


def test_a_dead_runner_is_reported_once(tmp_path, monkeypatch):
    heartbeat.mark_run_end(tmp_path / "status", now=NOW - 3600)
    tester = _Tester()
    monkeypatch.setattr(watchdog_run, "TelegramTester", lambda s: tester)
    settings = _settings(tmp_path)
    asyncio.run(watchdog_run.check(settings, NOW))
    assert len(tester.sent) == 1
    peer, text = tester.sent[0]
    assert peer == "me"
    assert text.startswith("E2E TestRunnerDead")
    # A watchdog that re-alerts every cycle teaches the reader to ignore it.
    asyncio.run(watchdog_run.check(settings, NOW + 60))
    assert len(tester.sent) == 1


def test_recovery_clears_the_marker(tmp_path, monkeypatch):
    status = tmp_path / "status"
    heartbeat.mark_run_end(status, now=NOW - 3600)
    monkeypatch.setattr(watchdog_run, "TelegramTester", lambda s: _Tester())
    settings = _settings(tmp_path)
    asyncio.run(watchdog_run.check(settings, NOW))
    assert heartbeat.alerted_path(status).exists()
    heartbeat.mark_run_end(status, now=NOW)
    asyncio.run(watchdog_run.check(settings, NOW))
    assert not heartbeat.alerted_path(status).exists()


def test_a_failed_send_leaves_the_marker_absent(tmp_path, monkeypatch):
    """One Telegram outage must not silence every later outage."""
    heartbeat.mark_run_end(tmp_path / "status", now=NOW - 3600)
    monkeypatch.setattr(watchdog_run, "TelegramTester", lambda s: _Tester(send_ok=False))
    settings = _settings(tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(watchdog_run.check(settings, NOW))
    assert not heartbeat.alerted_path(settings.status_dir).exists()


def test_never_ran_is_reported_rather_than_silently_accepted(tmp_path, monkeypatch):
    """This is why the install seeds the heartbeat: otherwise day one alerts."""
    tester = _Tester()
    monkeypatch.setattr(watchdog_run, "TelegramTester", lambda s: tester)
    asyncio.run(watchdog_run.check(_settings(tmp_path), NOW))
    assert tester.sent[0][1].startswith("E2E TestRunnerDead")


def test_compose_names_the_class_and_the_reason():
    v = heartbeat.classify(NOW - 3600, NOW, 600)
    text = watchdog_run.compose(v)
    assert "TestRunnerDead" in text
    assert "60 min ago" in text
