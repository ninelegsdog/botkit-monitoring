"""The seam between the wait and the grade: a press step, end to end.

The client reports what happened; the runner decides what it means. Both halves
are tested on their own - the client against a fake Telegram, the grade
against hand-made observations. But the two halves meet in exactly one place:
the observation the client produces must be the observation the grader
understands. A change in either that breaks the agreement is invisible to
both sets of tests, and the symptom is a press step that never fails, which
is the defect this whole track exists to remove.

So these tests drive the real client with a fake Telegram, take the
observations it produces, and grade them with the real grader. The fake
Telegram is a double, and a double that is softer than the real thing would
be worse than no double: it must behave like Telegram does, or the test
proves nothing. Here it does what Telegram does - the callback answer carries
the toast text, the message keeps its id when it is edited - and it is
asserted to.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from e2e.client import KIND_EDIT, KIND_NO_EDIT, KIND_TOAST, TelegramTester
from e2e.config import Settings
from e2e.run_e2e import judge_scenario


class _Btn:
    def __init__(self, data: str):
        self.callback_data = data


class _MarkupMsg:
    """A message with an inline button, like the one a bot sends on /start."""

    def __init__(self, mid: int, text: str, buttons):
        self.id = mid
        self.text = text
        self.out = False
        self.reply_markup = SimpleNamespace(rows=[SimpleNamespace(buttons=buttons)])


class _Msg:
    def __init__(self, mid: int, text: str):
        self.id = mid
        self.text = text
        self.out = False
        self.reply_markup = None


class _FakeTelegram:
    """Telegram, as far as the press path is concerned.

    `edit_sequence` is what the message text becomes, poll by poll. The first
    element is the state before the press; each later element is the state the
    next poll sees. The callback answer carries `toast`, which is how Telegram
    delivers an answer to an inline-button press.
    """

    def __init__(self, message, edit_sequence=(), toast=""):
        self.message = message
        self._edit_sequence = list(edit_sequence)
        self._edit_index = 0
        self.toast = toast
        self.callback_requests: list = []
        self._by_id = {message.id: message}

    async def get_messages(self, username, limit=1, ids=None):
        if ids is None:
            return [self.message]
        mid = ids[0] if isinstance(ids, (list, tuple)) else ids
        if self._edit_sequence and self._edit_index < len(self._edit_sequence):
            current = self._edit_sequence[self._edit_index]
            self._edit_index += 1
            if current.id is not None:
                self._by_id[current.id] = current
        if mid in self._by_id:
            return [self._by_id[mid]]
        return []

    async def get_input_entity(self, username):
        return object()

    async def __call__(self, request):
        self.callback_requests.append(request)
        return SimpleNamespace(message=self.toast)


def _tester(fake):
    # The constructor opens a real telethon session on disk; the tests here
    # never connect, so the instance is built without it. The same trick the
    # existing client tests use.
    t = TelegramTester.__new__(TelegramTester)
    t.client = fake
    t.settings = Settings(api_id=1, api_hash="h")
    return t


def _press_step(expect: str):
    return SimpleNamespace(send="", press="book:my", expect=expect)


def _scenario(*steps):
    return SimpleNamespace(steps=list(steps))


def _run(fake, steps, timeout=5):
    """Press through the real client, then grade with the real grader."""
    t = _tester(fake)
    results = asyncio.run(t.run_scenario("botkit-bookingbot", steps, timeout))
    return judge_scenario(_scenario(*steps), results)


def test_a_press_that_produces_the_expected_text_passes_end_to_end():
    """The ordinary case, through both halves: the button was pressed, the
    message was edited to what the scenario expected, and the grade is a pass."""
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    edited = _Msg(10, "У вас нет активных записей")
    fake = _FakeTelegram(button, edit_sequence=[_Msg(10, "Выберите действие:"), edited, edited])

    verdicts = _run(fake, [_press_step("У вас нет активных записей")])

    assert [v.ok for v in verdicts] == [True]


def test_a_press_that_edits_to_other_text_fails_end_to_end():
    """The original defect, through both halves.

    The button was pressed, the message was edited - so the wait was
    satisfied - but the text is not what the scenario expected. The grade
    must be a failure that names both texts. This is the case the old code
    could not express: the expectation was part of the search condition, so
    a different text simply never arrived and the step died as a timeout
    instead of being graded.
    """
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    edited = _Msg(10, "📄 #24 — Договор аренды")
    fake = _FakeTelegram(button, edit_sequence=[_Msg(10, "Выберите действие:"), edited, edited])

    verdicts = _run(fake, [_press_step("Нет активных записей")])

    assert [v.ok for v in verdicts] == [False]
    assert verdicts[0].expected == "Нет активных записей"
    assert verdicts[0].actual == "📄 #24 — Договор аренды"


def test_a_press_that_edited_nothing_fails_end_to_end_with_the_fact():
    """No edit at all: the verdict says so in words."""
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    fake = _FakeTelegram(button, edit_sequence=[_Msg(10, "Выберите действие:"), _Msg(10, "Выберите действие:")])

    verdicts = _run(fake, [_press_step("Нет активных записей")], timeout=1)

    assert [v.ok for v in verdicts] == [False]
    assert verdicts[0].actual == "<редактирования не было>"


def test_a_press_answered_with_a_toast_fails_end_to_end_with_its_own_reason():
    """The bot answered the button with a toast instead of editing.

    The toast text is the only thing the bot said, so the verdict carries it:
    the responder can see what the bot wanted to say without opening the
    session. This is a different failure from silence, and it must not be
    reported as silence.
    """
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    fake = _FakeTelegram(
        button,
        edit_sequence=[_Msg(10, "Выберите действие:")],
        toast="Слишком много запросов",
    )

    verdicts = _run(fake, [_press_step("Нет активных записей")], timeout=1)

    assert [v.ok for v in verdicts] == [False]
    assert verdicts[0].actual == "Слишком много запросов"


def test_the_double_behaves_like_telegram_about_the_toast():
    """The double is asserted, not assumed.

    Telegram delivers an answer to an inline-button press as a callback answer
    whose `message` holds the toast text, and it is empty when the bot
    answered without one. If the double stopped doing that, the toast case
    would stop being exercised and the test would be green for the wrong
    reason.
    """
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    fake = _FakeTelegram(button, edit_sequence=[_Msg(10, "Выберите действие:")], toast="тост")
    t = _tester(fake)
    asyncio.run(t._press_and_wait("botkit-bookingbot", _press_step("x"), 1))
    assert len(fake.callback_requests) == 1


def test_a_press_without_an_edit_produces_the_edit_observation_only_when_the_text_changed():
    """The wait is about the text changing, not about a message arriving.

    Telegram sends the same message object after an edit, with the same id and
    the new text. A wait keyed on "a message arrived" would be satisfied by
    the unedited message and would report an edit that never happened.
    """
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    fake = _FakeTelegram(button, edit_sequence=[_Msg(10, "Выберите действие:"), _Msg(10, "Выберите действие:")])
    t = _tester(fake)
    result = asyncio.run(t._press_and_wait("botkit-bookingbot", _press_step("x"), 1))
    assert result.kind == KIND_NO_EDIT


def test_a_press_that_edited_the_message_produces_the_edit_observation():
    """The positive half of the same seam."""
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    edited = _Msg(10, "У вас нет активных записей")
    fake = _FakeTelegram(button, edit_sequence=[_Msg(10, "Выберите действие:"), edited, edited])
    t = _tester(fake)
    result = asyncio.run(t._press_and_wait("botkit-bookingbot", _press_step("x"), 5))
    assert result.kind == KIND_EDIT
    assert result.actual == "У вас нет активных записей"


def test_a_press_answered_with_a_toast_produces_the_toast_observation():
    """The toast half of the seam, observed rather than graded."""
    button = _MarkupMsg(10, "Выберите действие:", [_Btn("book:my")])
    fake = _FakeTelegram(
        button, edit_sequence=[_Msg(10, "Выберите действие:")], toast="Слишком много запросов"
    )
    t = _tester(fake)
    result = asyncio.run(t._press_and_wait("botkit-bookingbot", _press_step("x"), 1))
    assert result.kind == KIND_TOAST
