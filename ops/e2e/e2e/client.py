from __future__ import annotations

import asyncio
import getpass
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

if TYPE_CHECKING:
    from e2e.config import Settings



def _button_callback_data(btn: Any) -> str | None:
    """Callback payload of an inline button, across telethon button shapes.

    Standard `KeyboardButtonCallback` carries it on `btn.callback_data`. The
    unified `KeyboardInlineButton` instead nests it: `btn.type` is an
    `InlineButtonTypeCallback` with `.data` (bytes). `btn.callback_data` is
    None there, so a flat getattr silently matches nothing.
    """
    data = getattr(btn, "callback_data", None)
    if data is None:
        data = getattr(getattr(btn, "type", None), "data", None)
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data) if data is not None else None

# Telethon prefers python-socks and falls back to PySocks, and warns about an
# "ignored" proxy whenever python-socks is missing even though the fallback then
# works. The host installs python-socks; either is enough to connect, so the
# check accepts both rather than pretending only one exists.
try:  # pragma: no cover - depends on the venv
    import python_socks  # noqa: F401

    _SOCKS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - depends on the venv
    try:
        import socks  # noqa: F401

        _SOCKS_AVAILABLE = True
    except ModuleNotFoundError:
        _SOCKS_AVAILABLE = False


def _proxy_tuple(spec: str) -> tuple[str, str, int] | None:
    """Translate "socks5h:127.0.0.1:11080" into telethon's (type, host, port).

    None for an empty spec, so connecting directly stays the default. A malformed
    spec raises here rather than falling back to a direct connection: a typo in the
    proxy setting must not silently produce an attempt that cannot work.
    """
    if not spec:
        return None
    kind, _, rest = spec.partition(":")
    host, _, port = rest.partition(":")
    # Only what telethon actually accepts. "socks5h" is a curl convention meaning
    # resolve-remotely; telethon has no such protocol and raises
    # "Unknown proxy protocol type" deep inside the connect path, which reads as
    # a network problem rather than a setting mistake.
    if kind != "socks5" or not host or not port.isdigit():
        msg = f"E2E_PROXY must look like socks5:host:port (telethon speaks only socks5), got {spec!r}"
        raise ValueError(msg)
    return kind, host, int(port)


# The slowest ThrottlingMiddleware in the fleet discards any message that arrives less
# than min_interval after the previous one: six bots run min_interval=2.0 (delivery,
# docuflow, leadgen, pricesentry, store, support) while three run rate_limit=0.5
# (bookingbot, membership, reminder). A step sent the moment the previous reply landed
# fell inside that window and the middleware returned None without calling a handler -
# aiogram still logs "is handled", which is why W18 looked like a /start defect and
# reported 3/9 with exactly the 2.0-second bots failing.
#
# SETTLE_S must exceed the slowest interval with margin. POLL_S is only how often the
# reply is looked for and must stay well under it, so a reply is caught long before the
# settle delay ends and the inter-step gap is decided by SETTLE_S, not by a race.
SETTLE_S = 2.5
POLL_S = 0.4

# How many polls in a row must show the same new text before the wait believes
# it. A handler may edit the message more than once (a placeholder first, the
# answer second); reporting the placeholder would be a false failure.
EDIT_SETTLE_POLLS = 2

# What a step produced. These are observations, not verdicts: the client says
# what happened, the runner decides what it means. The old `_wait_edit` did
# both at once - it searched the message for the expected substring - which is
# exactly why a press step could not fail on content: the condition of the
# check was the condition of the search.
KIND_REPLY = "reply"
KIND_EDIT = "edit"
KIND_TOAST = "toast"
KIND_NO_EDIT = "no_edit"
KIND_NO_REPLY = "no_reply"
KIND_NO_BUTTON = "no_button"

# The facts for the states that have no text to show. They are written as
# <...> so they cannot be read as something the bot said, and they are never an
# empty string: "the bot said nothing" and "we did not look" must not look the
# same in a verdict.
NO_EDIT_FACT = "<редактирования не было>"
NO_REPLY_FACT = "<ответа не было>"
NO_BUTTON_FACT = "<кнопка не найдена>"


@dataclass
class StepResult:
    """One step's observation: what the bot actually did, before any judging."""

    kind: str
    actual: str


@dataclass
class EditOutcome:
    """The pressed message before and after the press.

    The comparison with the scenario's `expect` is the runner's job, as it is
    for send steps: the client reports the difference, it does not grade it.
    """

    before: str
    after: str
    edited: bool


class StepError(Exception):
    """A step blew up before it could produce an observation.

    The step number travels with the error: a runner error that does not say
    which step it happened in sends the responder to the wrong place.
    """

    def __init__(self, number: int, expect: str, cause: BaseException) -> None:
        super().__init__(f"step {number} failed: {cause}")
        self.number = number
        self.expect = expect
        self.cause = cause


def _toast_text(answer: Any) -> str:
    """The text of a toast answer to a callback, if the bot sent one.

    Telegram delivers a bot's answer to an inline-button press as a
    `BotCallbackAnswer`; `message` holds the toast text and is empty when the
    bot answered without one. A bot that answers with a toast instead of
    editing the message is a different failure from a bot that said nothing,
    so the toast is read rather than guessed at.
    """
    text = getattr(answer, "message", None) or ""
    return str(text)


class TelegramTester:
    """Drives the fleet as a user account.

    Authentication is explicit and checked: `connect()` alone says nothing about
    whether the session is usable, and an unauthorized session would sit in
    `run_scenario` until the first timeout instead of failing at the reason.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        proxy = _proxy_tuple(settings.proxy)
        if proxy is not None and not _SOCKS_AVAILABLE:
            # Telethon speaks SOCKS only through PySocks. Checked here so the
            # failure names the missing package instead of arriving much later
            # as a connect timeout against the proxy port.
            msg = "E2E_PROXY is set but neither python-socks nor PySocks is in the venv"
            raise RuntimeError(msg)
        self.client = TelegramClient(
            str(settings.session_path),
            settings.api_id,
            settings.api_hash,
            device_model=settings.device_model,
            system_version=settings.system_version,
            app_version=settings.app_version,
            proxy=proxy,
        )
        self._usernames: dict[str, str] = {}

    async def __aenter__(self) -> TelegramTester:
        await self.connect()
        return self

    async def __aexit__(self, *_a: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Connect and refuse to continue on an unusable session.

        A missing phone is only acceptable for an already-authorized session;
        that is what lets the E2E host run without keeping TG_PHONE.
        """
        try:
            await self.client.connect()
        except ConnectionError as exc:
            # Telethon raises the same ConnectionError for "Telegram blocked our
            # egress" and "our proxy is down". Those need opposite responses -
            # one is unfixable from here, the other is a dead systemd unit - and
            # the bare message tells an operator neither.
            if self.settings.proxy:
                msg = (
                    f"cannot reach Telegram through {self.settings.proxy}: {exc}. "
                    "If the listener is absent, the tunnel unit is down: "
                    "systemctl status botkit-e2e-tunnel. A direct connection "
                    "from this host is blocked by Telegram, so the proxy is not optional."
                )
            else:
                msg = f"cannot reach Telegram directly: {exc}"
            raise RuntimeError(msg) from exc
        if await self.client.is_user_authorized():
            return
        if not self.settings.phone:
            msg = (
                f"session {self.settings.session_path} is not authorized and no phone is "
                "configured; run first_login.py on an admin machine instead of storing TG_PHONE here"
            )
            raise RuntimeError(msg)
        await self.client.send_code_request(self.settings.phone)
        # telethon 1.45 dropped the callable form of `code` in sign_in: the parameter is
        # typed Union[str, int] and the request is built with `str(code)`. Passing
        # `lambda: input(...)` therefore sent the callable's repr as the code, so
        # PhoneCodeInvalidError came back instantly, input() was never called, and the
        # prompt never appeared - which read as "the operator cannot enter the code".
        # The code is read here and handed over as a string, which is what 1.45 accepts.
        code = input("Telegram code: ")
        try:
            await self.client.sign_in(phone=self.settings.phone, code=code)
        except SessionPasswordNeededError:
            # Telethon raises this from sign_in, not from send_code_request, when the
            # account has 2FA. The old handler sat around send_code_request and was
            # marked no-cover, which is why nothing noticed: the exception never went
            # there.
            await self._enter_second_factor()
        if not await self.client.is_user_authorized():
            msg = "sign-in completed without producing an authorized session"
            raise RuntimeError(msg)

    async def _enter_second_factor(self, attempts: int = 3) -> None:
        """Enter the cloud password, and let a wrong one be corrected in place.

        Why this is a loop and not a single call. sign_in(password=...) re-reads the
        account's password hash with GetPasswordRequest on every invocation and never
        touches the sign-in code - the code was already accepted, which is why
        SessionPasswordNeededError was raised at all. So a rejected password consumes
        nothing: there is no code to re-request, no SMS to wait for, and no throttling to
        sit through. Retrying is free.

        What it used to do instead: the call was made once, uncaught, so
        PasswordHashInvalidError escaped the login entirely. One typo in the second
        factor ended the run, voided the code the operator had just entered correctly,
        and charged them a minute of Telegram throttling for the next one - which is how
        a five-character mistyped password cost a real login on 04.10. Worse, the code
        prompt's own retry loop treats every failure as "ask for a new code", so even a
        message that mentioned the password would have driven the operator to wait for
        an SMS that was never needed.

        The prompt says where the password comes from, because the three candidates are
        routinely confused and Telegram only answers "invalid" to all of them: it is the
        cloud two-step password from Settings -> Privacy -> Two-Step Verification. It is
        not the SMS code, not the account login password, and it is case-sensitive.
        """
        for attempt in range(1, attempts + 1):
            password = getpass.getpass("2FA password (cloud password, not the SMS code): ")
            try:
                await self.client.sign_in(password=password)
                return
            except SessionPasswordNeededError:
                # Telegram can still ask for the password after a partial exchange; treat
                # it as another wrong attempt rather than as a signal to give up.
                reason = "the account asked for the second factor again"
            except PasswordHashInvalidError:
                reason = "Telegram rejected it as invalid"
            print(
                f"  2FA attempt {attempt} of {attempts} failed: {reason}.",
                file=sys.stderr,
                flush=True,
            )
            if attempt < attempts:
                print(
                    "  Nothing is consumed by a wrong password - the code is still"
                    " good. Type it again, or check Settings -> Privacy ->"
                    " Two-Step Verification on the account for the cloud password.",
                    file=sys.stderr,
                    flush=True,
                )
        msg = (
            f"the cloud two-step password was rejected {attempts} times. The sign-in code was"
            " accepted, so this is not a code problem: check the account's Two-Step"
            " Verification password (case-sensitive, not the SMS code)."
        )
        raise RuntimeError(msg)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    def prime_usernames(self, mapping: dict[str, str]) -> None:
        """Cache bot directory name -> username for the duration of the run."""
        self._usernames = dict(mapping)

    def username_for(self, bot: str) -> str:
        if bot not in self._usernames:
            msg = f"no username known for {bot}; bots.yml must be loaded before the run"
            raise RuntimeError(msg)
        return self._usernames[bot]

    async def run_scenario(self, username: str, steps: list[Any], timeout: int) -> list[StepResult]:
        """Run the steps and report what each of them produced.

        Every step yields an observation, even a failed one: the runner compares
        the observations with the scenario's expectations and writes the verdict.
        The client never grades an answer - that is what makes a press step able
        to fail on content.
        """
        out: list[StepResult] = []
        for i, st in enumerate(steps):
            try:
                if getattr(st, "press", ""):
                    # A press step does not send anything, so it does not consume a
                    # fresh-message id: it taps a button that is already on screen.
                    result = await self._press_and_wait(username, st, timeout)
                else:
                    top = await self.client.get_messages(username, limit=1)
                    seen = top[0].id if top else 0
                    await self.client.send_message(username, st.send)
                    result = await self._wait_reply(username, timeout, seen)
            except Exception as exc:
                raise StepError(i + 1, getattr(st, "expect", ""), exc) from exc
            out.append(result)
            if result.kind in (KIND_NO_REPLY, KIND_NO_BUTTON):
                # The bot said nothing at all: the next step has nothing to react
                # to, and every further step would only spend another timeout to
                # learn the same thing.
                break
            if i < len(steps) - 1:
                await asyncio.sleep(SETTLE_S)
        return out

    async def _press_and_wait(self, username: str, step: Any, timeout: int) -> StepResult:
        """Press an inline button by callback_data and observe what happened.

        The allowlisted handlers answer with `callback.message.edit_text(...)`,
        which rewrites the SAME message id. `_wait_reply` only looks for ids
        newer than the last one it saw, so it would time out on an edit. This
        waits for the target message's text to change instead.

        What comes back is an observation, not a verdict: the text the message
        ended up with, the toast the bot answered with, or the fact that
        nothing happened. Comparing that with the scenario's `expect` is the
        runner's job - a press step that grades itself cannot fail.
        """
        msg = await self._find_button_message(username, step.press)
        if msg is None:
            return StepResult(kind=KIND_NO_BUTTON, actual=NO_BUTTON_FACT)
        peer = await self.client.get_input_entity(username)
        before_text = getattr(msg, "text", "") or ""
        answer = await self.client(
            GetBotCallbackAnswerRequest(peer=peer, msg_id=msg.id, data=step.press.encode())
        )
        toast = _toast_text(answer)
        outcome = await self._wait_edit(username, msg.id, before_text, timeout)
        if outcome.edited:
            return StepResult(kind=KIND_EDIT, actual=outcome.after)
        if toast:
            # The bot answered the press with a toast instead of editing the
            # message. That is a different failure from silence, and the toast
            # is the only fact we have about what the bot wanted to say.
            return StepResult(kind=KIND_TOAST, actual=toast)
        return StepResult(kind=KIND_NO_EDIT, actual=NO_EDIT_FACT)

    async def _find_button_message(self, username: str, callback_data: str) -> Any:
        for m in await self.client.get_messages(username, limit=5):
            markup = getattr(m, "reply_markup", None)
            if not markup:
                continue
            for row in getattr(markup, "rows", []) or []:
                for btn in getattr(row, "buttons", []) or []:
                    if _button_callback_data(btn) == callback_data:
                        return m
        return None

    async def _wait_edit(
        self, username: str, message_id: int, before_text: str, timeout: int
    ) -> EditOutcome:
        """Wait for the message's text to change and report what it became.

        The wait looks for a CHANGE OF TEXT. It does not look for the expected
        substring: that was the defect - the condition of the check was the
        condition of the search, so a press step could not fail on content, and
        a bot that edited the message to something else looked exactly like a
        bot that said nothing. It does not guess either: the old message offered
        "a toast, or the button was not tapped" as if it were a fact, and the
        three states that are now told apart are the three ways it can really go.

        The new text has to settle before it is reported: a handler may edit the
        message twice (a placeholder first, the answer second), and reporting
        the placeholder would be a false failure.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = ""
        repeats = 0
        while loop.time() < deadline:
            m = await self.client.get_messages(username, ids=message_id)
            if isinstance(m, (list, tuple)):
                m = m[0] if m else None
            text = (getattr(m, "text", "") or "") if m else ""
            if text and text != before_text:
                if text == last:
                    repeats += 1
                    if repeats >= EDIT_SETTLE_POLLS:
                        break
                else:
                    last = text
                    repeats = 1
            await asyncio.sleep(POLL_S)
        return EditOutcome(before=before_text, after=last, edited=bool(last))

    async def _wait_reply(self, username: str, timeout: int, after_id: int) -> StepResult:
        """Wait for a fresh message from the bot.

        "No answer" is an observation, not an exception: the verdict has to be
        able to name the step that went quiet, and a bare TimeoutError says
        only that the bot was silent somewhere.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for m in await self.client.get_messages(username, limit=5):
                if m.id <= after_id:
                    continue
                if not m.out and m.text:
                    return StepResult(kind=KIND_REPLY, actual=m.text)
            await asyncio.sleep(POLL_S)
        return StepResult(kind=KIND_NO_REPLY, actual=NO_REPLY_FACT)
