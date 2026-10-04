"""Liveness of the runner, judged from outside the runner.

The runner reports its own summary, but a runner that dies mid-run reports
nothing - and that silence is byte-for-byte what a healthy run also looks like.
`TestRunnerDead` is therefore the one failure class the runner cannot raise about
itself: a dead process does not get to announce its own death. It has to be
inferred from the last moment the runner proved it was alive.

Liveness is measured from run *completion*, not from a passing verdict. A run
that finished with nine broken bots is still nine proofs that the runner works.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path

RUN_END = ".run.end"
ALERTED = ".alerted.watchdog"

OK = "ok"
STALE = "stale"


@dataclass(frozen=True)
class Verdict:
    state: str
    age_s: float
    reason: str


def classify(end_mtime: float | None, now: float, max_gap_s: float) -> Verdict:
    """Decide whether the runner is still reporting."""
    if end_mtime is None:
        return Verdict(STALE, float("inf"), "no run has ever recorded a heartbeat")
    # A clock that jumped backwards would otherwise yield a negative age, and
    # "reports every -40 minutes" would read as maximally alive.
    age = max(0.0, now - end_mtime)
    reason = f"last completed run {age / 60:.0f} min ago"
    return Verdict(STALE if age > max_gap_s else OK, age, reason)


def heartbeat_path(status_dir: Path) -> Path:
    return status_dir / RUN_END


def alerted_path(status_dir: Path) -> Path:
    return status_dir / ALERTED


def read_end_mtime(status_dir: Path) -> float | None:
    """Read the recorded moment out of the file's contents, not its mtime.

    mtime would work on this host, but the deploy scripts move these trees with
    cp -a, and any copy can restamp a heartbeat into the future - which would
    report a dead runner as freshly alive for hours. The number inside the file
    is what the runner actually claimed, so that is what is believed.
    """
    try:
        return float(heartbeat_path(status_dir).read_text().split()[0])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def mark_run_end(status_dir: Path, now: float | None = None) -> None:
    status_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time() if now is None else now
    # Human-readable on purpose: when someone asks "when did this last run",
    # they should be able to cat the file instead of subtracting from an epoch.
    # No trailing newline - nothing parses this file, it is read by eye.
    moment = dt.datetime.fromtimestamp(stamp).isoformat(timespec="seconds")
    heartbeat_path(status_dir).write_text(f"{stamp:.0f} {moment}")
