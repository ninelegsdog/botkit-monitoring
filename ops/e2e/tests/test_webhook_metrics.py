"""Tests for the Prometheus textfile export of the webhook verdict.

Two properties are being defended. The first is honest reporting: a gauge reading 0 has to
mean "the contract failed", never "nobody looked", so an unknown value is -1 and a missing
series is impossible for a bot in the fleet. The second is liveness: the run timestamp must
stop advancing when the checker stops running, because a frozen gauge is indistinguishable
from a healthy one - which is the failure the whole incident was about.

The main() tests stub the network, not the exporter, so they fail if the publishing call
site is deleted. That is the mutation which would leave every test here green while Grafana
quietly displayed nothing at all.
"""

from __future__ import annotations

import json

import pytest

import webhook_check
import webhook_metrics
from webhook_metrics import UNKNOWN

DOMAIN = "ninelegsbots.duckdns.org"
IP = "2.27.204.95"
BOOKING = "bookingbot"
LEADGEN = "leadgenbot"


def series(document: str) -> dict[str, str]:
    """Map every full series line in a document to its value, ignoring HELP/TYPE."""
    out: dict[str, str] = {}
    for line in document.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            out[name] = value
    return out


def body(bot: str, *, pending: int = 0, last_error: str | None = None) -> str:
    result = {
        "url": f"https://{DOMAIN}/webhook/{bot}",
        "has_custom_certificate": False,
        "pending_update_count": pending,
        "last_error_message": last_error,
        "last_error_date": None,
        "ip_address": IP,
    }
    return json.dumps({"ok": True, "result": result})


def install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    bots: tuple[str, ...],
    pending: int = 0,
    tls_problem: str | None = None,
    unhealthy: tuple[str, ...] = (),
):
    """Point the checker at a temporary tree and at a fake Bot API.

    Only the network, the alert path and the absolute paths are replaced. The contract
    evaluation, the verdict aggregation and the publishing all run for real, because those
    are exactly the parts a test must not stand in for.
    """
    monkeypatch.setattr(webhook_check, "LOG_PATH", tmp_path / "check.log")
    monkeypatch.setattr(webhook_check, "METRICS_DIR", tmp_path / "metrics")
    monkeypatch.setattr(webhook_check, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(webhook_check, "ALERT_ROOT", tmp_path / "alerts")
    monkeypatch.setattr(webhook_check, "read_token", lambda bot: f"{bot}:TOKEN")
    monkeypatch.setattr(webhook_check, "read_secret", lambda bot: "s3cret")
    monkeypatch.setattr(webhook_check, "check_public_tls", lambda domain, timeout=10: tls_problem)
    monkeypatch.setattr(webhook_check, "read_pending", lambda bot: pending)
    monkeypatch.setattr(webhook_check, "write_pending", lambda bot, value: None)
    monkeypatch.setattr(webhook_check, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(webhook_check, "resolve_alert", lambda *a, **k: None)

    def fake_fetch(token: str, timeout: int | None = None) -> str:
        name = token.split(":", maxsplit=1)[0]
        if name in unhealthy:
            return body(name, pending=pending, last_error="last update failed")
        return body(name, pending=pending)

    monkeypatch.setattr(webhook_check, "fetch_webhook_info", fake_fetch)

    fleet_raw = " ".join(f"{b}:8443" for b in bots)
    fleet_file = tmp_path / "fleet.env"
    fleet_file.write_text(f'WEBHOOK_DOMAIN={DOMAIN}\nWEBHOOK_IP={IP}\nFLEET="{fleet_raw}"\n')
    return webhook_check.Path(tmp_path / "metrics"), fleet_file


def read(metrics_dir, name: str) -> dict[str, str]:
    return series((metrics_dir / name).read_text(encoding="utf-8"))


# --- rendering ----------------------------------------------------------------------


def test_bot_file_declares_each_family_exactly_once():
    document = webhook_metrics.render_bot(BOOKING, True, 0)
    assert document.count("# TYPE botkit_webhook_contract gauge") == 1
    assert document.count("# TYPE botkit_webhook_pending gauge") == 1


def test_run_file_declares_each_family_exactly_once():
    document = webhook_metrics.render_run(True, 0, 1758943210)
    assert document.count("# TYPE botkit_webhook_tls gauge") == 1
    assert document.count("# TYPE botkit_webhook_check_rc gauge") == 1
    assert document.count("# TYPE botkit_webhook_check_timestamp gauge") == 1


def test_sample_document_declares_every_family_exactly_once():
    # Joining two rendered per-bot files used to emit a second HELP for the same metric
    # name, which promtool rejects outright. The CI gate caught it; this keeps it caught
    # without needing promtool in the unit test run.
    document = webhook_metrics.sample_document()
    helps = [line for line in document.splitlines() if line.startswith("# HELP ")]
    assert len(helps) == len(set(helps))


def test_pending_is_exported_when_known():
    assert series(webhook_metrics.render_bot(BOOKING, True, 7))[
        f'botkit_webhook_pending{{bot="{BOOKING}"}}'
    ] == "7"


def test_pending_is_minus_one_when_unknown():
    values = series(webhook_metrics.render_bot(BOOKING, False, None))
    assert values[f'botkit_webhook_pending{{bot="{BOOKING}"}}'] == str(UNKNOWN)
    assert values[f'botkit_webhook_contract{{bot="{BOOKING}"}}'] == "0"


def test_tls_is_minus_one_when_the_check_was_skipped():
    assert "botkit_webhook_tls -1" in webhook_metrics.render_run(None, 0, 1)


def test_tls_is_one_and_zero_as_verdicts():
    assert "botkit_webhook_tls 1" in webhook_metrics.render_run(True, 0, 1)
    assert "botkit_webhook_tls 0" in webhook_metrics.render_run(False, 1, 1)


def test_escape_label_escapes_backslash_and_double_quote():
    assert webhook_metrics.escape_label('a\\b"c') == 'a\\\\b\\"c'
    assert webhook_metrics.escape_label("a\nb") == "a\\nb"


@pytest.mark.parametrize("bot", ["../../etc/passwd", "bot name", 'bot"x', "", "a/b", "a\\b"])
def test_bot_names_that_are_not_plain_identifiers_are_refused(bot):
    # These become filenames. Rewriting a hostile name into a safe one would hide the input
    # problem, so the exporter refuses it and lets the staleness alert speak instead.
    with pytest.raises(ValueError):
        webhook_metrics.render_bot(bot, True, 0)


# --- writing ------------------------------------------------------------------------


def test_publish_writes_the_expected_series(tmp_path):
    webhook_metrics.publish_bot(BOOKING, True, 0, tmp_path)
    values = read(tmp_path, f"botkit_webhook_{BOOKING}.prom")
    assert values[f'botkit_webhook_contract{{bot="{BOOKING}"}}'] == "1"
    assert values[f'botkit_webhook_pending{{bot="{BOOKING}"}}'] == "0"


def test_publish_replaces_a_previous_verdict(tmp_path):
    webhook_metrics.publish_bot(BOOKING, True, 0, tmp_path)
    webhook_metrics.publish_bot(BOOKING, False, 3, tmp_path)
    values = read(tmp_path, f"botkit_webhook_{BOOKING}.prom")
    # Both series must be present: an omitted "0" is read by the alert as a missing bot.
    assert values[f'botkit_webhook_contract{{bot="{BOOKING}"}}'] == "0"
    assert values[f'botkit_webhook_pending{{bot="{BOOKING}"}}'] == "3"


def test_publish_leaves_no_temporary_file_behind(tmp_path):
    webhook_metrics.publish_bot(BOOKING, True, 0, tmp_path)
    webhook_metrics.publish_run(True, 0, 1, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"botkit_webhook_{BOOKING}.prom",
        "botkit_webhook_run.prom",
    ]


def test_a_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    webhook_metrics.publish_bot(BOOKING, True, 0, tmp_path)
    before = (tmp_path / f"botkit_webhook_{BOOKING}.prom").read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(webhook_metrics.os, "replace", boom)
    with pytest.raises(OSError):
        webhook_metrics.publish_bot(BOOKING, False, 9, tmp_path)

    # Stale, not empty. An empty file would look like every bot had disappeared.
    after = (tmp_path / f"botkit_webhook_{BOOKING}.prom").read_text(encoding="utf-8")
    assert after == before
    assert not list(tmp_path.glob("*.tmp"))


# --- pruning ------------------------------------------------------------------------


def test_prune_drops_a_bot_that_left_the_fleet(tmp_path):
    webhook_metrics.publish_bot(BOOKING, True, 0, tmp_path)
    webhook_metrics.publish_bot(LEADGEN, True, 0, tmp_path)
    webhook_metrics.publish_run(True, 0, 1, tmp_path)

    assert webhook_metrics.prune([BOOKING], tmp_path) == [LEADGEN]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"botkit_webhook_{BOOKING}.prom",
        "botkit_webhook_run.prom",
    ]


def test_prune_keeps_the_run_file_even_if_a_bot_shared_its_name(tmp_path):
    webhook_metrics.publish_run(True, 0, 1, tmp_path)
    assert webhook_metrics.prune([], tmp_path) == []
    assert (tmp_path / "botkit_webhook_run.prom").exists()


def test_prune_on_an_absent_directory_is_harmless(tmp_path):
    assert webhook_metrics.prune([BOOKING], tmp_path / "never-created") == []


# --- integration through main() ------------------------------------------------------


def test_main_publishes_every_bot_and_the_run(tmp_path, monkeypatch):
    metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING, LEADGEN))

    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 0

    for bot in (BOOKING, LEADGEN):
        values = read(metrics_dir, f"botkit_webhook_{bot}.prom")
        assert values[f'botkit_webhook_contract{{bot="{bot}"}}'] == "1"
    run = read(metrics_dir, "botkit_webhook_run.prom")
    assert run["botkit_webhook_tls"] == "1"
    assert run["botkit_webhook_check_rc"] == "0"
    assert int(run["botkit_webhook_check_timestamp"]) > 0


def test_main_publishes_the_failure_verdict(tmp_path, monkeypatch):
    metrics_dir, fleet_file = install_stubs(
        monkeypatch, tmp_path, bots=(BOOKING, LEADGEN), unhealthy=(LEADGEN,)
    )

    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 1

    assert read(metrics_dir, f"botkit_webhook_{BOOKING}.prom")[
        f'botkit_webhook_contract{{bot="{BOOKING}"}}'
    ] == "1"
    assert read(metrics_dir, f"botkit_webhook_{LEADGEN}.prom")[
        f'botkit_webhook_contract{{bot="{LEADGEN}"}}'
    ] == "0"
    assert read(metrics_dir, "botkit_webhook_run.prom")["botkit_webhook_check_rc"] == "1"


def test_a_broken_public_certificate_is_exported_even_though_the_exit_code_stays_zero(
    tmp_path, monkeypatch
):
    metrics_dir, fleet_file = install_stubs(
        monkeypatch, tmp_path, bots=(BOOKING,), tls_problem="certificate verify failed"
    )
    # A C7 failure raises a per-bot Alertmanager alert but has never touched the exit code,
    # so the oneshot unit reports success. Asserted here rather than left implicit: it is a
    # real asymmetry, and the reason it is still safe to leave is that the value is not
    # hidden - botkit_webhook_tls reads 0, and the alerts fire. Changing rc would alter what
    # systemd reports, so it belongs in its own change with its own decision, not smuggled
    # in beside the metrics work.
    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 0
    run = read(metrics_dir, "botkit_webhook_run.prom")
    assert run["botkit_webhook_tls"] == "0"
    assert run["botkit_webhook_check_rc"] == "0"


def test_quick_mode_publishes_nothing(tmp_path, monkeypatch):
    # A quick run checks neither TLS nor pending state, so its numbers are not a verdict on
    # delivery. Exporting them would let a manual --quick overwrite the fleet's own answer.
    metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING,))
    assert webhook_check.main(["--fleet-file", str(fleet_file), "--quick"]) == 0
    assert not metrics_dir.exists() or not list(metrics_dir.glob("*.prom"))


def test_a_fault_run_publishes_nothing(tmp_path, monkeypatch):
    # --fault deliberately lies about the fleet. Exporting it would poison the very verdict
    # BotWebhookCheckStale is there to protect.
    metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING,))
    assert webhook_check.main(["--fleet-file", str(fleet_file), "--fault", "stale_error"]) == 1
    assert not metrics_dir.exists() or not list(metrics_dir.glob("*.prom"))


def test_a_single_bot_run_does_not_prune_the_others(tmp_path, monkeypatch):
    metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING, LEADGEN))
    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 0

    # A second run scoped to one bot must not mistake the other eight for a departed fleet.
    assert webhook_check.main(["--fleet-file", str(fleet_file), "--bot", BOOKING]) == 0
    assert (metrics_dir / f"botkit_webhook_{LEADGEN}.prom").exists()
    assert (metrics_dir / f"botkit_webhook_{BOOKING}.prom").exists()


def test_a_single_bot_run_does_not_move_the_staleness_timestamp(tmp_path, monkeypatch):
    metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING, LEADGEN))
    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 0
    first = int(read(metrics_dir, "botkit_webhook_run.prom")["botkit_webhook_check_timestamp"])

    ticks = iter([first + 10**6])
    real_time = webhook_check.time.time
    monkeypatch.setattr(webhook_check.time, "time", lambda: next(ticks))
    assert webhook_check.main(["--fleet-file", str(fleet_file), "--bot", BOOKING]) == 0
    monkeypatch.setattr(webhook_check.time, "time", real_time)

    # The freshness guard belongs to the scheduled full run only.
    assert read(metrics_dir, "botkit_webhook_run.prom")["botkit_webhook_check_timestamp"] == str(
        first
    )


def test_a_full_run_prunes_a_bot_that_left_the_fleet(tmp_path, monkeypatch):
    metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING, LEADGEN))
    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 0
    assert (metrics_dir / f"botkit_webhook_{LEADGEN}.prom").exists()

    smaller = tmp_path / "fleet-small.env"
    smaller.write_text(f'FLEET="{BOOKING}:8443"\n')
    assert webhook_check.main(["--fleet-file", str(smaller)]) == 0
    assert not (metrics_dir / f"botkit_webhook_{LEADGEN}.prom").exists()
    assert (metrics_dir / f"botkit_webhook_{BOOKING}.prom").exists()


def test_an_unwritable_metrics_directory_does_not_change_the_exit_code(tmp_path, monkeypatch):
    _metrics_dir, fleet_file = install_stubs(monkeypatch, tmp_path, bots=(BOOKING,))
    monkeypatch.setattr(webhook_check, "METRICS_DIR", tmp_path / "file-in-the-way" / "metrics")
    (tmp_path / "file-in-the-way").write_text("not a directory", encoding="utf-8")

    # The verdict was healthy, and a broken metrics directory is a different fault: saying
    # "delivery broken" here would send the on-call to the wrong place entirely.
    assert webhook_check.main(["--fleet-file", str(fleet_file)]) == 0
    assert "ERROR metrics not published" in (tmp_path / "check.log").read_text(encoding="utf-8")
