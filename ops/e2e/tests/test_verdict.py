"""The verdict must be readable on its own, and it must be able to fail.

Two things are checked here.

First, the format: a line that says which bot, why, which step, what was
expected and what actually happened. The person on duty reads that line and
acts on it - without opening the database, the session or the bot's code. A
verdict that only says "failed" sends them on a search, and a verdict that
only appears on failure cannot prove the passing steps were checked.

Second, the grades: a scenario is graded by what the steps produced, and the
grade is decided outside the wait. The wait used to search the message for the
expected substring while waiting for it, so the condition of the check was the
condition of the search - a press step could not fail on content, and a bot
that edited its message to something else looked exactly like a bot that said
nothing. The tests below pin the three ways a press step can go wrong as three
different verdicts, and they pin that the verdict names the step that failed.
"""

from __future__ import annotations

from types import SimpleNamespace

from e2e import run_e2e
from e2e.client import (
    KIND_EDIT,
    KIND_NO_BUTTON,
    KIND_NO_EDIT,
    KIND_NO_REPLY,
    KIND_REPLY,
    KIND_TOAST,
    NO_BUTTON_FACT,
    NO_EDIT_FACT,
    NO_REPLY_FACT,
    StepResult,
)
from e2e.run_e2e import judge_scenario


def _scenario(*steps):
    return SimpleNamespace(steps=list(steps))


def _step(expect="ожидаемое", press="", send="/start"):
    return SimpleNamespace(send=send, press=press, expect=expect)


# --- the grades ------------------------------------------------------------


def test_a_matching_reply_passes():
    """The ordinary case: the bot said what the scenario expected."""
    verdicts = judge_scenario(
        _scenario(_step(expect="Привет")),
        [StepResult(kind=KIND_REPLY, actual="Привет, чем помочь?")],
    )
    assert [v.ok for v in verdicts] == [True]
    assert verdicts[0].reason == run_e2e.ALL_GOOD


def test_a_reply_with_other_text_fails_and_shows_both_sides():
    """The bot answered, but not what was expected. Both texts must be there.

    The fact is what the bot said; the expectation is what the scenario asked
    for. A verdict with only one of them leaves the reader guessing which of
    the two is wrong.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет документов")),
        [StepResult(kind=KIND_REPLY, actual="📄 #24 — Договор аренды")],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.UNEXPECTED_REPLY
    assert verdicts[0].expected == "Нет документов"
    assert verdicts[0].actual == "📄 #24 — Договор аренды"
    assert verdicts[0].step == 1


def test_a_press_that_was_never_made_is_not_a_timeout_but_a_fact():
    """No edit at all: the fact says so, in words, not as an empty string.

    "The bot said nothing" and "we did not look" must not read the same in a
    verdict, so the fact is a marker rather than an empty text.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет активных записей", press="book:my")),
        [StepResult(kind=KIND_NO_EDIT, actual=NO_EDIT_FACT)],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.EDIT_MISSING
    assert verdicts[0].actual == "<редактирования не было>"
    assert verdicts[0].actual != ""


def test_a_toast_instead_of_an_edit_is_its_own_reason():
    """The bot answered the button with a toast rather than editing the message.

    That is a different failure from silence and from a wrong edit: the bot
    did answer, it just answered the wrong way. A responder must not be sent
    to restart a bot that was answering perfectly well.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет активных записей", press="book:my")),
        [StepResult(kind=KIND_TOAST, actual="Слишком много запросов")],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.TOAST_INSTEAD_OF_EDIT
    assert verdicts[0].actual == "Слишком много запросов"


def test_a_missing_button_is_not_the_same_as_a_silent_bot():
    """The button was not on screen. The bot was never asked anything."""
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет активных записей", press="book:my")),
        [StepResult(kind=KIND_NO_BUTTON, actual=NO_BUTTON_FACT)],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.BUTTON_NOT_FOUND
    assert verdicts[0].actual == "<кнопка не найдена>"


def test_silence_is_its_own_reason():
    """No reply at all from a send step."""
    verdicts = judge_scenario(
        _scenario(_step(expect="Привет")),
        [StepResult(kind=KIND_NO_REPLY, actual=NO_REPLY_FACT)],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.BOT_NOT_ANSWERING
    assert verdicts[0].actual == "<ответа не было>"


def test_an_edited_message_is_graded_by_content_not_by_the_fact_of_editing():
    """The original defect, pinned from the other side.

    The press step produced an edit - so the wait was satisfied - but the text
    is not what the scenario expected. The grade must be a failure, and the
    verdict must show both texts. A grade that only asked "was there an edit?"
    would pass this case, and the bot's wrong answer would look like a pass.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет активных записей", press="book:my")),
        [StepResult(kind=KIND_EDIT, actual="📄 #24 — Договор аренды")],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.UNEXPECTED_REPLY
    assert verdicts[0].expected == "Нет активных записей"
    assert verdicts[0].actual == "📄 #24 — Договор аренды"


def test_an_empty_expectation_is_a_defect_of_the_scenario_not_a_pass():
    """An empty expectation is a substring of everything, so it would pass.

    A scenario that asks for nothing must be reported as broken, not as a
    green step: otherwise the step is green because nothing was asked of the
    bot, and the reader of the log has no way to tell that from a real pass.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="")),
        [StepResult(kind=KIND_REPLY, actual="Любой текст")],
    )
    assert verdicts[0].ok is False
    assert verdicts[0].reason == run_e2e.SCENARIO_DEFECT


def test_the_failing_step_is_named_even_when_earlier_steps_passed():
    """The verdict points at the step that failed, not at the first step.

    A scenario whose first step passed and whose third failed must report the
    third step. Reporting the first would send the responder to a step that
    worked.
    """
    verdicts = judge_scenario(
        _scenario(
            _step(expect="первый"),
            _step(expect="второй"),
            _step(expect="третий"),
        ),
        [
            StepResult(kind=KIND_REPLY, actual="первый"),
            StepResult(kind=KIND_REPLY, actual="второй"),
            StepResult(kind=KIND_REPLY, actual="совсем другое"),
        ],
    )
    assert [v.ok for v in verdicts] == [True, True, False]
    assert verdicts[2].step == 3
    assert verdicts[2].expected == "третий"


# --- the format ------------------------------------------------------------


def test_the_verdict_line_carries_bot_reason_step_expectation_and_fact():
    """Everything the responder needs is on one line."""
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет документов")),
        [StepResult(kind=KIND_REPLY, actual="📄 #24 — Договор аренды")],
    )
    line = run_e2e.format_verdict("botkit-docuflow", verdicts[0])
    assert line.startswith("FAIL botkit-docuflow[UnexpectedReply] step=1 ")
    assert 'expected="Нет документов"' in line
    assert 'actual="📄 #24 — Договор аренды"' in line


def test_a_passing_step_is_printed_too():
    """The verdict is printed for every step, not only for the failing one.

    A verdict that appears only on failure cannot prove that the passing steps
    were checked: silence and success look the same in a log.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="Привет")),
        [StepResult(kind=KIND_REPLY, actual="Привет")],
    )
    line = run_e2e.format_verdict("botkit-x", verdicts[0])
    assert line.startswith("OK botkit-x[AllGood] step=1 ")


def test_a_fact_that_is_not_a_quote_is_shown_bare():
    """The markers are not something the bot said, so they are not quoted.

    Quoting them would read as a quote, and the reader would look for that
    text in the bot's source.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="Нет активных записей", press="book:my")),
        [StepResult(kind=KIND_NO_EDIT, actual=NO_EDIT_FACT)],
    )
    line = run_e2e.format_verdict("botkit-bookingbot", verdicts[0])
    assert "actual=<редактирования не было>" in line
    assert 'actual="<редактирования не было>"' not in line


def test_a_long_fact_is_cut_so_the_verdict_stays_one_line():
    """A reply can be a whole screen of text; the verdict must stay one line."""
    long_text = "х" * 500
    verdicts = judge_scenario(
        _scenario(_step(expect="ожидаемое")),
        [StepResult(kind=KIND_REPLY, actual=long_text)],
    )
    line = run_e2e.format_verdict("botkit-x", verdicts[0])
    assert "\n" not in line
    assert len(line) < 200


def test_the_reasons_are_a_closed_set():
    """The reason on the line is a name, not a sentence.

    A person on duty and a machine both read that field: the machine matches
    on it, and a sentence would match nothing. A new reason has to be added
    to the set deliberately.
    """
    known = {
        run_e2e.UNEXPECTED_REPLY,
        run_e2e.BOT_NOT_ANSWERING,
        run_e2e.EDIT_MISSING,
        run_e2e.TOAST_INSTEAD_OF_EDIT,
        run_e2e.BUTTON_NOT_FOUND,
        run_e2e.SCENARIO_DEFECT,
        run_e2e.ALL_GOOD,
        run_e2e.RUNNER_ERROR,
    }
    cases = [
        (KIND_REPLY, "что-то"),
        (KIND_EDIT, "что-то"),
        (KIND_TOAST, "тост"),
        (KIND_NO_EDIT, NO_EDIT_FACT),
        (KIND_NO_REPLY, NO_REPLY_FACT),
        (KIND_NO_BUTTON, NO_BUTTON_FACT),
    ]
    for kind, actual in cases:
        verdicts = judge_scenario(
            _scenario(_step(expect="ожидаемое", press="x")),
            [StepResult(kind=kind, actual=actual)],
        )
        assert verdicts[0].reason in known, verdicts[0].reason


def test_the_verdict_does_not_leak_a_newline_from_the_bots_text():
    """A bot that answers with several lines must not break the log's format.

    The verdict is one line per step, and a newline in the fact would make
    the next line look like a verdict of its own - or like nothing at all.
    """
    verdicts = judge_scenario(
        _scenario(_step(expect="ожидаемое")),
        [StepResult(kind=KIND_REPLY, actual="первая строка\nвторая строка")],
    )
    line = run_e2e.format_verdict("botkit-x", verdicts[0])
    assert "\n" not in line
    assert "первая строка вторая строка" in line
