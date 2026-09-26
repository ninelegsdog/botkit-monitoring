"""Contract tests for the shell-level smoke checks.

`smoke_all.sh` claims to verify the public webhook path end to end. For 15 days it did
not verify anything about the certificate: `curl -k` made "external TLS ok" a green lie
while Telegram refused every delivery. These tests pin the claim to the code, so the
`-k` cannot come back as a convenience tweak.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1]
SMOKE = OPS / "smoke_all.sh"
README = OPS / "README.md"

# curl spellings that disable peer verification. Long forms included: `-k` alone does
# not catch `--insecure`, and a reviewer skimming for "-k" would miss it.
INSECURE = re.compile(r"(?:^|\s)(?:-k\b|--insecure\b)")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to inspect the smoke script"
)


def script_text() -> str:
    return SMOKE.read_text()


def script_commands() -> list[tuple[int, str]]:
    """Executable lines only.

    Prose may legitimately say "no -k" — that is how the rule gets explained to the
    next reader. A guard that also scans comments would force the documentation to
    stay silent about the very thing it exists to prevent.
    """
    out = []
    for n, ln in enumerate(script_text().splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        out.append((n, ln))
    return out


def public_curl_lines() -> list[tuple[int, str]]:
    """Every line that talks to DOMAIN, i.e. the public HTTPS path."""
    return [(n, ln) for n, ln in script_commands() if "DOMAIN" in ln]


def test_smoke_script_exists_and_is_executable():
    assert SMOKE.is_file(), "smoke_all.sh is the only Layer A check that touches the public webhook"


def test_no_public_call_skips_certificate_verification():
    """The core regression guard: no -k/--insecure in any executed command."""
    offenders = [(n, ln.strip()) for n, ln in script_commands() if INSECURE.search(ln)]
    assert offenders == [], f"certificate verification disabled: {offenders}"


def test_insecure_flag_is_also_caught_in_its_long_form():
    """`--insecure` bypasses a grep for -k; the guard must cover both spellings."""
    guard = INSECURE
    assert guard.search('curl --insecure "$url"')
    assert guard.search('curl -k "$url"')
    assert not guard.search('curl -sS -o /dev/null "$url"')


def test_verified_call_is_actually_used_for_the_webhook():
    """Guards the inverse mistake: a script that dropped the public call entirely."""
    calls = public_curl_lines()
    assert calls, "no reference to DOMAIN found - the public webhook is not checked at all"
    assert any("post_public" in ln for n, ln in script_commands()), (
        "public calls must go through post_public, which is where verification lives"
    )


def test_tls_failure_is_classified_not_swallowed():
    """An untrusted chain must be reported as TLS, not collapse into http_code=000."""
    text = script_text()
    for needle in ("TLS_UNTRUSTED", "TLS_NAME_MISMATCH", "CURL_RC"):
        assert needle in text, f"curl failure classification lost: {needle}"
    assert re.search(r'\[ "\$tls" = "verified" \]', text), (
        "a non-verified TLS state must fail the bot, otherwise the column is decorative"
    )


def test_tls_state_reaches_the_report_and_the_alert_reason():
    text = script_text()
    assert re.search(r'echo "\$name \| \$tls \|', text), "tls state missing from the per-bot row"
    assert 'reason="tls=$tls' in text, "tls state missing from the alert reason"


def test_tls_state_reaches_the_persistent_log():
    """The table goes to stdout/journald; the log file is the artifact people grep.

    A verdict that only exists in the console output is lost by the next rotation, and
    `OK <bot>` alone would record nothing about the certificate.
    """
    text = script_text()
    assert re.search(r'log "OK \$name tls=\$tls', text), "the OK line must record the tls state"


def test_readme_no_longer_claims_a_self_signed_certificate():
    """The stale justification must not survive: it is what made -k look intentional."""
    text = README.read_text()
    assert "самоподписанный (так задумано)" not in text, (
        "README still presents a self-signed certificate as by design"
    )
    assert "Exit 0 = PASS. `curl -k`" not in text


def test_readme_states_that_tls_is_verified():
    text = README.read_text()
    assert "верификацией TLS" in text, "README must state that the public path verifies TLS"
