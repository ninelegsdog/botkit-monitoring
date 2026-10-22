from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from e2e.client import (
    KIND_NO_BUTTON,
    KIND_NO_EDIT,
    KIND_NO_REPLY,
    KIND_TOAST,
    NO_BUTTON_FACT,
    NO_EDIT_FACT,
    NO_REPLY_FACT,
    StepError,
    StepResult,
    TelegramTester,
)
from e2e.config import Settings, load_bots, load_scenarios, load_settings
from e2e.heartbeat import mark_run_end

ALERT_THROTTLE_S = 3600  # не чаще 1 алерта на бота за 1ч
ALERT_DESCRIPTION_LIMIT = 200
# Держим алерт открытым дольше до следующего прогона (6ч + 30м jitter): раннер
# только пере-POST'ит его на прогоне, поэтому более короткое окно заставило бы
# Alertmanager самому отчитаться "resolved", пока бот всё ещё сломан.
ALERT_HOLD_H = 7
HTTP_OK = 200
HTTP_MULTI_STATUS = 300
# Distinct codes on purpose: an operator reading the journal or `systemctl`
# has to be able to tell "the fleet failed" from "the tests never ran".
EXIT_TESTS_FAILED = 1
EXIT_NO_ALERT_TRANSPORT = 2

# Failure classes for R6.3. They stay distinct because they demand different
# responses: a silent bot gets restarted, a broken runner is not a bot problem
# at all, and a dead runner can only be reported by something outside the runner.
BOT_NOT_ANSWERING = "BotNotAnswering"
UNEXPECTED_REPLY = "UnexpectedReply"
RUNNER_ERROR = "E2ERunnerError"
WEBHOOK_NOT_DELIVERED = "WebhookNotDelivered"
TEST_RUNNER_DEAD = "TestRunnerDead"
# The rest of the verdict vocabulary. It is a closed set on purpose: the reason
# on the line is read by a person on duty and by a machine, so it is a name and
# not a sentence.
EDIT_MISSING = "EditMissing"
TOAST_INSTEAD_OF_EDIT = "ToastInsteadOfEdit"
BUTTON_NOT_FOUND = "ButtonNotFound"
SCENARIO_DEFECT = "ScenarioDefect"
ALL_GOOD = "AllGood"

# Facts that are shown as they are, without quotes: they are not something the
# bot said, and quoting them would read as a quote.
BARE_FACTS = frozenset({NO_EDIT_FACT, NO_REPLY_FACT, NO_BUTTON_FACT})

# How much of the fact a verdict line carries. A reply can be a whole screen of
# text; the verdict has to stay one line and still show what the bot said.
FACT_LIMIT = 100

# The reason for a step that produced no text to compare, by what it produced.
# The client reports the facts; the mapping to a reason is the runner's job.
KIND_REASONS = {
    KIND_NO_REPLY: BOT_NOT_ANSWERING,
    KIND_NO_EDIT: EDIT_MISSING,
    KIND_TOAST: TOAST_INSTEAD_OF_EDIT,
    KIND_NO_BUTTON: BUTTON_NOT_FOUND,
}


@dataclass
class Verdict:
    """One step's verdict: what the bot did, what was expected, and why.

    `step` is None when the failure happened outside a step (a broken session,
    a bot missing from bots.yml): the verdict says "?" rather than inventing a
    step number.
    """

    ok: bool
    reason: str
    step: int | None
    expected: str
    actual: str


def judge_scenario(scenario, results: list[StepResult]) -> list[Verdict]:
    """Compare what the steps produced with what the scenario expected.

    The comparison lives here and not in the client. The client used to search
    the message for the expected substring while waiting for it, which made the
    condition of the check the condition of the search: a press step could not
    fail on content, and a bot that edited its message to something else looked
    exactly like a bot that said nothing. Here the facts are graded, and the
    three ways a press step can go wrong are told apart.
    """
    return [
        _judge_step(number, step, result)
        for number, (step, result) in enumerate(zip(scenario.steps, results, strict=False), start=1)
    ]


def _judge_step(number: int, step: Any, result: StepResult) -> Verdict:
    reason = KIND_REASONS.get(result.kind)
    if reason is not None:
        # The bot produced no comparable text: the fact says which of the three
        # ways it happened, and no amount of reading the expectation would help.
        return Verdict(
            ok=False, reason=reason, step=number, expected=step.expect, actual=result.actual
        )
    if not step.expect:
        # An empty expectation is a substring of everything, so the step would be
        # green because nothing was asked of the bot. That is a defect in the
        # scenario, not a pass.
        return Verdict(
            ok=False,
            reason=SCENARIO_DEFECT,
            step=number,
            expected=step.expect,
            actual=result.actual,
        )
    if step.expect not in result.actual:
        return Verdict(
            ok=False,
            reason=UNEXPECTED_REPLY,
            step=number,
            expected=step.expect,
            actual=result.actual,
        )
    return Verdict(ok=True, reason=ALL_GOOD, step=number, expected=step.expect, actual=result.actual)


def _quote(text: str, limit: int = FACT_LIMIT) -> str:
    """A verdict value on one line: whitespace flattened, long values cut."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return f'"{flat}"'


def _fact(text: str) -> str:
    """The fact as the verdict shows it: a bare marker or a quoted text."""
    return text if text in BARE_FACTS else _quote(text)


def format_verdict(bot: str, verdict: Verdict, alert: str | None = None) -> str:
    """The verdict line: bot, reason, step, expectation, fact - in that order.

    Everything an on-call responder needs in order to act without opening the
    database, the session or the bot's code. It is printed for every step of
    every bot, pass or fail: a verdict that only appears on failure cannot
    prove that the passing steps were checked.
    """
    tag = "OK" if verdict.ok else "FAIL"
    step = verdict.step if verdict.step is not None else "?"
    line = (
        f"{tag} {bot}[{verdict.reason}] step={step} "
        f"expected={_quote(verdict.expected)} actual={_fact(verdict.actual)}"
    )
    if alert is not None:
        line += f" alert={alert}"
    return line


def verdict_detail(verdict: Verdict) -> str:
    """The failure as the page carries it: reason, step, expectation, fact.

    The bot's name is left out - the page already carries it - and the rest is
    the same string the journal shows, so the two can be read together.
    """
    step = verdict.step if verdict.step is not None else "?"
    return (
        f"{verdict.reason} step={step} "
        f"expected={_quote(verdict.expected)} actual={_fact(verdict.actual)}"
    )


def post_alert(url: str, payload: list[dict], timeout: int = 5) -> bool:
    """POST to Alertmanager. Returns False instead of raising.

    A failed alert must not mask the test result that triggered it, so the
    reason is reported and the caller keeps going.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return HTTP_OK <= resp.status < HTTP_MULTI_STATUS


def build_alert(bot: str, reason: str, alertname: str = UNEXPECTED_REPLY) -> list[dict]:
    """One alertname per failure mode.

    A single catch-all "E2E упал" is useless on duty: one silent bot and a
    broken runner look identical, so the responder cannot tell whether to
    restart a bot or the test rig.
    """
    severity = "critical" if alertname in (RUNNER_ERROR, TEST_RUNNER_DEAD, SCENARIO_DEFECT) else "warning"
    now = datetime.now(timezone.utc)
    return [
        {
            "labels": {
                "alertname": alertname,
                "severity": severity,  # E2: не critical by default
                "bot": bot,
                "service": "botkit-e2e",
            },
            "annotations": {
                "summary": f"E2E {alertname} {bot}",
                "description": reason[:ALERT_DESCRIPTION_LIMIT],
            },
            # Без endsAt Alertmanager закрывает алерт через свой resolve_timeout,
            # так что «ещё сломано» и «давно пропало» неразличимы. Гасить обязан
            # наш срок — так же, как webhook_check.send_alert().
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(hours=ALERT_HOLD_H)).isoformat(),
        }
    ]


def _alert_marker(settings: Settings, bot: str, channel: str):
    return settings.status_dir / f".alerted.{channel}.{bot}"


def _alert_throttled(settings: Settings, bot: str, channel: str) -> bool:
    """True when this channel already alerted about this bot inside the window.

    Each channel keeps its own marker on purpose. A shared marker would let a
    successful Telegram send suppress the Alertmanager post, or the reverse,
    for the rest of the hour.
    """
    last = _alert_marker(settings, bot, channel)
    return last.exists() and time.time() - last.stat().st_mtime < ALERT_THROTTLE_S


def _mark_alerted(settings: Settings, bot: str, channel: str) -> None:
    """Record the throttle marker; a failed write must not mask the verdict.

    The marker lives in the service's own state dir, but a stale file left by a
    manual run under another user once turned this write into a PermissionError
    that propagated out of the alert path, replacing the real failure with a
    traceback (#457). A missed marker only costs one extra page, so the write is
    best-effort: reported loudly, then swallowed.
    """
    try:
        _alert_marker(settings, bot, channel).write_text(str(time.time()))
    except OSError as exc:
        print(f"WARN could not write alert marker for {bot}/{channel}: {exc}")


def send_alert(
    settings: Settings, bot: str, reason: str, alertname: str = UNEXPECTED_REPLY
) -> bool:
    """POST to Alertmanager. An unconfigured URL is a loud no-op, not a silent success."""
    if not settings.alert_url:
        print(f"FATAL no E2E_ALERT_URL configured; cannot alert on {bot} failure (fail-loud)")
        return False
    if _alert_throttled(settings, bot, "am"):
        return False
    try:
        ok = post_alert(settings.alert_url, build_alert(bot, reason, alertname))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"WARN alert send failed: {exc}")
        return False
    if ok:
        _mark_alerted(settings, bot, "am")
    return ok


async def notify_saved_messages(
    tester, settings: Settings, bot: str, reason: str, alertname: str = UNEXPECTED_REPLY
) -> bool:
    """Page the owner through the E2E session's own Saved Messages.

    Alertmanager listens only on the prod loopback and prod forbids TCP
    forwarding (AllowTcpForwarding no), so this host has no route to it. The
    user session is already authorized and is the only channel that needs no
    new credential, so the alert is written to "me": no bot token, no new
    inbound port, nothing to store.
    """
    if _alert_throttled(settings, bot, "tg"):
        return False
    text = (
        f"E2E {alertname}\n"
        f"bot: {bot}\n"
        f"{reason[:ALERT_DESCRIPTION_LIMIT]}\n"
        "runner: botkit-e2e (channel: Saved Messages)"
    )
    try:
        await tester.client.send_message("me", text)
    except Exception as exc:  # a failed page must not mask the test result
        print(f"WARN Saved Messages notify failed for {bot}: {exc}")
        return False
    _mark_alerted(settings, bot, "tg")
    return True


async def alert_on_failure(
    tester, settings: Settings, bot: str, reason: str, alertname: str = UNEXPECTED_REPLY
) -> str:
    """Try every enabled channel; return which ones actually delivered."""
    used = []
    if settings.alert_url and send_alert(settings, bot, reason, alertname):
        used.append("alertmanager")
    if settings.alert_telegram and await notify_saved_messages(
        tester, settings, bot, reason, alertname
    ):
        used.append("telegram")
    return "+".join(used) if used else "NONE"


async def notify_run_summary(tester, settings: Settings, problems: int, total: int) -> bool:
    """Report the run's own outcome. This is the watchdog half of alerting.

    A per-bot failure already pages by itself, so silence used to be ambiguous:
    a runner that died halfway and a fleet that is perfectly healthy both said
    nothing. One message per run makes the absence of a message meaningful.
    """
    verdict = "all passed" if problems == 0 else f"{problems} of {total} FAILED"
    text = (
        f"E2E run finished: {total - problems}/{total} {verdict}\n"
        "runner: botkit-e2e (channel: Saved Messages)"
    )
    try:
        await tester.client.send_message("me", text)
    except Exception as exc:  # a missed heartbeat must not rewrite the verdict
        print(f"WARN run summary not delivered: {exc}")
        return False
    print(f"watchdog: summary delivered ({verdict})")
    return True


def _step_error_fact(exc: BaseException) -> str:
    text = str(exc)
    return text or exc.__class__.__name__


async def run_all(scenarios, settings: Settings) -> int:  # noqa: PLR0912, PLR0915
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    bots = load_bots(settings.bots_file)
    problems = 0
    async with TelegramTester(settings) as t:
        t.prime_usernames(bots)
        for bot, sc in scenarios.items():
            failure = UNEXPECTED_REPLY
            try:
                username = t.username_for(bot)
                results = await t.run_scenario(username, sc.steps, settings.timeout)
                verdicts = judge_scenario(sc, results)
                ok = all(v.ok for v in verdicts)
                if ok:
                    failure = ALL_GOOD
                    first_bad = None
                else:
                    first_bad = next((v for v in verdicts if not v.ok), None)
                    if first_bad is not None:
                        failure = first_bad.reason
                        if failure in (BOT_NOT_ANSWERING, EDIT_MISSING):
                            failure = BOT_NOT_ANSWERING
                        elif failure == BUTTON_NOT_FOUND:
                            failure = UNEXPECTED_REPLY
            except StepError as e:
                ok = False
                failure = RUNNER_ERROR
                verdicts = [
                    Verdict(
                        ok=False,
                        reason=RUNNER_ERROR,
                        step=e.number,
                        expected=e.expect,
                        actual=_step_error_fact(e.cause),
                    )
                ]
            except TimeoutError as e:
                ok = False
                err = str(e)
                failure = BOT_NOT_ANSWERING
                verdicts = [
                    Verdict(
                        ok=False,
                        reason=BOT_NOT_ANSWERING,
                        step=None,
                        expected="",
                        actual=err,
                    )
                ]
            except Exception as e:  # a per-bot failure must not abort the fleet
                ok = False
                err = str(e)
                failure = RUNNER_ERROR
                verdicts = [
                    Verdict(
                        ok=False,
                        reason=RUNNER_ERROR,
                        step=None,
                        expected="",
                        actual=err,
                    )
                ]
            if ok:
                (settings.status_dir / f"{bot}.ok").write_text("ok")
                (settings.status_dir / f"{bot}.fail").unlink(missing_ok=True)
                with contextlib.suppress(Exception):
                    (settings.status_dir / f".alerted.{bot}").unlink(missing_ok=True)
                for v in verdicts:
                    print(format_verdict(bot, v))
            else:
                (settings.status_dir / f"{bot}.fail").write_text("fail")
                (settings.status_dir / f"{bot}.ok").unlink(missing_ok=True)
                # The failing step, not the first step: a scenario whose first
                # step passed and whose third failed must not page the responder
                # with a verdict about step one. `verdicts[0]` would do exactly
                # that, and the responder would be sent to the wrong place.
                bad = next((v for v in verdicts if not v.ok), None)
                why = verdict_detail(bad) if bad is not None else "unexpected reply"
                sent = await alert_on_failure(t, settings, bot, why, failure)
                for v in verdicts:
                    print(format_verdict(bot, v, alert=sent if not v.ok else None))
                problems += 1
        if settings.watchdog:
            await notify_run_summary(t, settings, problems, len(scenarios))
    # Only a run that reached this line counts as proof of life. If the runner
    # dies above, the heartbeat stays stale and the external watchdog speaks.
    mark_run_end(settings.status_dir)
    return problems


def main() -> None:
    settings = load_settings()
    if not (settings.alert_url or settings.alert_telegram):
        if not settings.allow_no_alert:
            print(
                "FATAL no alert channel: E2E_ALERT_URL is empty and "
                "E2E_ALERT_TELEGRAM is off, so a failure would page nobody and\n"
                "a pass would prove nothing. Refusing to run. Enable one, or\n"
                "set E2E_ALLOW_NO_ALERT=1 to run deliberately unactioned.",
                file=sys.stderr,
            )
            sys.exit(EXIT_NO_ALERT_TRANSPORT)
        print(
            "WARNING running with every alert channel disabled because "
            "E2E_ALLOW_NO_ALERT=1: failures will not page anyone.",
            file=sys.stderr,
        )
    scenarios = load_scenarios(settings.scenarios_file)
    problems = asyncio.run(run_all(scenarios, settings))
    sys.exit(EXIT_TESTS_FAILED if problems else 0)


if __name__ == "__main__":
    main()
