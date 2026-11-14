"""The sync must prove the running Prometheus loaded the canon rules.

Prometheus does not watch its rule files. On 14.11 the image-retention rule was merged,
`sync-canon.sh` reset the clone to the merge commit, the content markers passed, and
`/api/v1/rules` still listed the previous 40 rules: the new check did not exist on the
host while every gate said the sync was fine. The container runs without
`--web.enable-lifecycle`, so the only way in is SIGHUP.

These assertions are structural on purpose - the step talks to a real docker daemon and
a real Prometheus, which a unit test cannot provide. They pin the two properties that
made the gap invisible: the reload exists and is skipped in `--check-only`, and the
verification reads every alert name out of the file instead of checking a marker for the
rule that happened to be added last (a marker stays true while the rest of the file is
stale).
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
SYNC_SH = REPO / "ops" / "lib" / "sync-canon.sh"


def _reload_section() -> str:
    source = SYNC_SH.read_text()
    start = source.index("=== 6. running Prometheus")
    end = source.index("SYNC OK", start)
    return source[start:end]


def test_the_sync_reloads_prometheus() -> None:
    section = _reload_section()
    assert "docker kill -s HUP botkit-monitoring-prometheus-1" in section, (
        "the sync no longer asks the running Prometheus to reload"
    )


def test_the_reload_is_skipped_in_check_only() -> None:
    section = _reload_section()
    guard = section.index("CHECK_ONLY")
    reload_at = section.index("docker kill -s HUP")
    assert guard < reload_at, "--check-only must be tested before the server is touched"


def test_the_reload_waits_for_the_container_to_be_there() -> None:
    """No docker or no container means a scratch host, not a reason to fail the sync."""
    section = _reload_section()
    inspect = section.index("docker inspect botkit-monitoring-prometheus-1")
    reload_at = section.index("docker kill -s HUP")
    assert inspect < reload_at, "the reload runs before anything checks that the container exists"


def test_every_canon_rule_is_verified_not_a_marker() -> None:
    section = _reload_section()
    # The prose above the step names the rule that exposed the gap; only the code counts.
    code = "\n".join(line for line in section.splitlines() if not line.lstrip().startswith("#"))
    assert "alerts.yml" in code, "the verification no longer reads the canon rule file"
    assert "BotkitImagePruneStale" not in code, (
        "the verification is a marker for one rule; the next rule added would not be checked"
    )
