#!/usr/bin/env python3
"""Publish the webhook delivery verdict as Prometheus textfile gauges.

Until 26.09.2026 the verdict of ``webhook_check.py`` existed only in a log file, so the
one question nobody could answer from Grafana was the only question that mattered: can
Telegram deliver an update to these bots right now. These gauges make it queryable, and
they are written from the same return value the alerting path already uses, so the number
in Grafana and the alert that fires cannot drift apart.

Two design choices carry the weight:

* One file per bot plus one run file. node-exporter's textfile collector merges every
  ``*.prom`` in the directory, so a ``--bot`` run cannot erase the other eight bots, and a
  bot dropped from the fleet stops being exported instead of exporting a verdict forever.
* The run timestamp. This is the part that catches the failure the incident was made of: if
  *this* checker stops running, the gauges keep their last values and the fleet looks
  healthy indefinitely. ``BotWebhookCheckStale`` reads the timestamp, so a silent checker
  becomes an alert instead of a comfortable green dashboard. Publishing is atomic
  (``os.replace``), so a crash mid-write leaves the previous file untouched and therefore
  stale rather than empty - empty would look like every bot had vanished.

A bot name that is not a plain ASCII identifier is rejected rather than sanitised: these
names become filenames, and quietly rewriting a hostile name into a safe one hides the
input problem that produced it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

# node-exporter only reads files ending in .prom, so the temporary file written before
# os.replace is invisible to the collector: a crash leaves a stray .tmp, never a half-parsed
# metric that would look like a failed bot.
PROM_SUFFIX = ".prom"
BOT_PREFIX = "botkit_webhook_"
RUN_FILE_NAME = "botkit_webhook_run.prom"

# Prometheus cannot represent "unknown" by omission for a gauge that alerts on a threshold:
# a missing series matches no comparison, so the alert stays silent exactly when the value
# stopped being knowable. -1 is below every threshold used here and is explicitly queryable.
UNKNOWN = -1

_BOT_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_BOT_HELP = (
    "# HELP botkit_webhook_contract Bot API delivery contract verdict, 1 when every "
    "contract check passed for this bot, 0 otherwise.\n"
    "# TYPE botkit_webhook_contract gauge\n"
)
_PENDING_HELP = (
    "# HELP botkit_webhook_pending Telegram pending_update_count for this bot, "
    f"{UNKNOWN} when the Bot API could not be reached.\n"
    "# TYPE botkit_webhook_pending gauge\n"
)
_TLS_HELP = (
    "# HELP botkit_webhook_tls Public certificate verifies for a third party, 1 yes, "
    f"0 no, {UNKNOWN} when the check was skipped.\n"
    "# TYPE botkit_webhook_tls gauge\n"
)
_RC_HELP = (
    "# HELP botkit_webhook_check_rc Exit code of the last full scheduled run.\n"
    "# TYPE botkit_webhook_check_rc gauge\n"
)
_TIMESTAMP_HELP = (
    "# HELP botkit_webhook_check_timestamp Unix time of the last full scheduled run, read "
    "by BotWebhookCheckStale to notice a checker that stopped running.\n"
    "# TYPE botkit_webhook_check_timestamp gauge\n"
)


def escape_label(value: str) -> str:
    """Escape a Prometheus label value: backslash, double quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def check_bot_name(bot: str) -> str:
    """Return ``bot`` unchanged, or refuse it: these names become filenames."""
    if not _BOT_NAME.match(bot):
        raise ValueError(f"unusable bot name for a metric filename: {bot!r}")
    return bot


def bot_path(directory: Path, bot: str) -> Path:
    return directory / f"{BOT_PREFIX}{check_bot_name(bot)}{PROM_SUFFIX}"


def run_path(directory: Path) -> Path:
    return directory / RUN_FILE_NAME


def bot_series(bot: str, contract_ok: bool, pending: int | None) -> str:
    """One bot's two series, carrying no HELP/TYPE of their own."""
    label = escape_label(check_bot_name(bot))
    verdict = 1 if contract_ok else 0
    waiting = UNKNOWN if pending is None else pending
    return (
        f'botkit_webhook_contract{{bot="{label}"}} {verdict}\n'
        f'botkit_webhook_pending{{bot="{label}"}} {waiting}\n'
    )


def run_series(tls_ok: bool | None, rc: int, timestamp: int) -> str:
    """Run-level series: public TLS, exit code, and the staleness guard."""
    tls = UNKNOWN if tls_ok is None else (1 if tls_ok else 0)
    return (
        f"botkit_webhook_tls {tls}\n"
        f"botkit_webhook_check_rc {rc}\n"
        f"botkit_webhook_check_timestamp {timestamp}\n"
    )


def render_bot(bot: str, contract_ok: bool, pending: int | None) -> str:
    """One bot's verdict as a standalone file.

    The HELP and TYPE lines are per file rather than per series: node-exporter merges every
    file in the directory into one scrape, and a directory written this way must also stay
    readable as a single document for `promtool check metrics`.
    """
    return _BOT_HELP + _PENDING_HELP + bot_series(bot, contract_ok, pending)


def render_run(tls_ok: bool | None, rc: int, timestamp: int) -> str:
    """Run-level facts as a standalone file: public TLS, exit code, staleness guard."""
    return _TLS_HELP + _RC_HELP + _TIMESTAMP_HELP + run_series(tls_ok, rc, timestamp)


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` so a reader sees either the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def publish_bot(bot: str, contract_ok: bool, pending: int | None, directory: Path) -> Path:
    path = bot_path(directory, bot)
    write_atomic(path, render_bot(bot, contract_ok, pending))
    return path


def publish_run(tls_ok: bool | None, rc: int, timestamp: int, directory: Path) -> Path:
    path = run_path(directory)
    write_atomic(path, render_run(tls_ok, rc, timestamp))
    return path


def prune(known_bots: Iterable[str], directory: Path) -> list[str]:
    """Drop exported files for bots that are no longer in the fleet.

    Without this a bot deleted from fleet.env keeps exporting its last verdict forever, and
    a dashboard keeps counting a bot that no longer exists. Only ever call this with the
    whole fleet: given a single-bot run it would delete the other eight.
    """
    if not directory.is_dir():
        return []
    known = set(known_bots)
    removed: list[str] = []
    for path in sorted(directory.glob(f"{BOT_PREFIX}*{PROM_SUFFIX}")):
        if path.name == RUN_FILE_NAME:
            continue
        bot = path.name[len(BOT_PREFIX) : -len(PROM_SUFFIX)]
        if bot not in known:
            path.unlink()
            removed.append(bot)
    return removed


def sample_document() -> str:
    """A valid single-file document, for `promtool check metrics` in CI.

    Generated from the same renderers the production path uses, so the check cannot rot into
    passing against a stale fixture that no longer matches the code. Each family declares
    HELP and TYPE exactly once, which is the part a naive concatenation gets wrong: joining
    two rendered per-bot files is a second HELP for the same metric name, and promtool
    rejects the whole document rather than the second half.
    """
    return (
        _BOT_HELP
        + _PENDING_HELP
        + _TLS_HELP
        + _RC_HELP
        + _TIMESTAMP_HELP
        + bot_series("bookingbot", True, 0)
        + bot_series("leadgen", False, 7)
        + run_series(True, 1, 1758943210)
    )


if __name__ == "__main__":
    print(sample_document(), end="")
