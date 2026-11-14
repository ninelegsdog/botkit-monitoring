"""Contract tests for ops/rollout/prune_images.sh, run against the shipped script.

Nothing removed an image before this: nine bots accumulated 375 images (47.8 GB) and
only a hand-run `docker system prune` had ever reclaimed any. The failure mode is slow
and silent, so the test pins the two properties that make the policy safe:

  - the newest window is kept and the image the container runs is kept even when it is
    older than the window, because that is the only rollback target deploy_rollout.sh
    has;
  - a repository whose running image cannot be read is skipped entirely, never pruned
    against an empty "running" reference.

The docker stub records every `image rm`, so an assertion on what was removed is an
assertion on the script's decisions, not on a copy of its logic.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

# ops/e2e/tests/test_image_prune_contract.py -> parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
PRUNE_SH = REPO / "ops" / "rollout" / "prune_images.sh"

FLEET_ENV_FIXTURE = """FLEET="bookingbot:8081"
WEBHOOK_DOMAIN="example.invalid"
WEBHOOK_IP="192.0.2.1"
BASE_URL="https://example.invalid"
PROMETHEUS_URL="http://127.0.0.1:9090"
ALERTMANAGER_URL="http://127.0.0.1:9093"
ALERTMANAGER_ALERTS_URL="http://127.0.0.1:9093/api/v2/alerts"
ALERTMANAGER_ALERTS_ACTIVE_URL="http://127.0.0.1:9093/api/v2/alerts?active=true"
GRAFANA_URL="http://127.0.0.1:3000"
LOKI_URL="http://127.0.0.1:3100"
TEMPO_URL="http://127.0.0.1:3200"
MINIO_URL="http://127.0.0.1:9000"
MINIO_CONSOLE_URL="http://127.0.0.1:9001"
OTLP_HTTP_URL="http://127.0.0.1:4318"
OTLP_GRPC_URL="http://127.0.0.1:4317"
CHANNEL="v0.8.5"
WG_OBSERVE_SUBNET="10.8.0.0/24"
WG_OBSERVE_PROD_IP="10.8.0.2"
WG_OBSERVE_MONITOR_IP="10.8.0.1"
WG_OBSERVE_PORT="51820"
"""

# One bot, fourteen tags, newest first. The running tag (t12) sits outside the keep
# window on purpose: a rollback target must not be deleted because it is old.
IMAGES = "\n".join(
    f"t{n:02d} 2026-11-{14 - (n - 1):02d} 10:00:00 +0000 UTC" for n in range(1, 15)
) + "\n"
RUNNING = "ghcr.io/ninelegsdog/botkit-bookingbot:t12"

DOCKER_STUB = """#!/usr/bin/env bash
if [ "$1" = inspect ]; then
  [ "${STUB_INSPECT_FAIL:-0}" = 1 ] && exit 1
  printf '%s\\n' "${STUB_RUNNING:-}"
  exit 0
fi
if [ "$1" = image ] && [ "$2" = ls ]; then
  if [ "$4" = "-q" ]; then
    while read -r t _rest; do echo "id-$t"; done < "$STUB_IMAGES"
  else
    while read -r t rest; do echo "$t $rest"; done < "$STUB_IMAGES"
  fi
  exit 0
fi
if [ "$1" = image ] && [ "$2" = rm ]; then
  echo "$3" >> "$STUB_RM_LOG"
  exit 0
fi
if [ "$1" = image ] && [ "$2" = prune ]; then
  echo "Total reclaimed space: 0B"
  exit 0
fi
exit 0
"""


class RunPrune:
    """The real script, copied beside a real ops/lib so its fleet.sh resolution works."""

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.tmp_path = tmp_path
        self.log = tmp_path / "prune.log"
        self.textfile = tmp_path / "botkit_image_prune.prom"
        self.rm_log = tmp_path / "removed.txt"
        self.images = tmp_path / "images.txt"
        self.images.write_text(IMAGES)
        self.fleet_env = tmp_path / "fleet.env"
        self.fleet_env.write_text(FLEET_ENV_FIXTURE)

        root = tmp_path / "repo"
        script_dir = root / "ops" / "rollout"
        lib_dir = root / "ops" / "lib"
        script_dir.mkdir(parents=True)
        lib_dir.mkdir(parents=True)
        shutil.copy(REPO / "ops" / "lib" / "fleet.sh", lib_dir / "fleet.sh")
        self.script = script_dir / "prune_images.sh"
        shutil.copy(PRUNE_SH, self.script)

        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        docker = stub_bin / "docker"
        docker.write_text(DOCKER_STUB)
        docker.chmod(0o755)

    def __call__(self, *args: str, **env: str) -> str:
        subprocess.run(
            ["bash", str(self.script), *args],
            env={
                **os.environ,
                "PATH": f"{self.tmp_path / 'bin'}:{os.environ['PATH']}",
                "BOTKIT_FLEET_ENV": str(self.fleet_env),
                "BOTKIT_PRUNE_LOG": str(self.log),
                "BOTKIT_PRUNE_TEXTFILE": str(self.textfile),
                "STUB_IMAGES": str(self.images),
                "STUB_RM_LOG": str(self.rm_log),
                "STUB_RUNNING": RUNNING,
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return self.log.read_text() if self.log.exists() else ""

    def removed(self) -> list[str]:
        return self.rm_log.read_text().split() if self.rm_log.exists() else []


@pytest.fixture
def run_prune(tmp_path: pathlib.Path) -> RunPrune:
    return RunPrune(tmp_path)


def test_keeps_the_newest_window(run_prune: RunPrune) -> None:
    run_prune()
    removed = [ref.rsplit(":", 1)[1] for ref in run_prune.removed()]
    for tag in ("t01", "t05", "t10"):
        assert tag not in removed, f"{tag} is inside the keep window but was removed"
    for tag in ("t11", "t13", "t14"):
        assert tag in removed, f"{tag} is outside the keep window but survived: {removed}"


def test_running_image_survives_the_window(run_prune: RunPrune) -> None:
    """The rollback target: t12 is older than the ten newest and must still be kept."""
    run_prune()
    assert not any(ref.endswith(":t12") for ref in run_prune.removed()), run_prune.removed()


def test_window_size_is_configurable(run_prune: RunPrune) -> None:
    run_prune(BOTKIT_KEEP_IMAGES="2")
    removed = [ref.rsplit(":", 1)[1] for ref in run_prune.removed()]
    assert "t03" in removed and "t04" in removed, removed
    assert "t01" not in removed and "t02" not in removed, removed
    assert not any(ref.endswith(":t12") for ref in run_prune.removed()), removed


def test_unreadable_running_image_skips_the_repository(run_prune: RunPrune) -> None:
    """No running reference means no way to know what a rollback would need."""
    log = run_prune(STUB_INSPECT_FAIL="1")
    assert run_prune.removed() == [], run_prune.removed()
    assert "cannot read the running image" in log, log


def test_dry_run_removes_nothing(run_prune: RunPrune) -> None:
    log = run_prune("--dry-run")
    assert run_prune.removed() == [], run_prune.removed()
    assert "would remove" in log, log


def test_textfile_is_readable_by_the_collector(run_prune: RunPrune) -> None:
    """BotkitImagePruneStale is an absent() rule: a 600 file fires it on a healthy timer."""
    run_prune()
    assert run_prune.textfile.exists()
    mode = run_prune.textfile.stat().st_mode & 0o777
    assert mode == 0o644, f"textfile mode is {oct(mode)}"
    content = run_prune.textfile.read_text()
    assert "botkit_image_prune_last_run_seconds " in content, content
    assert 'botkit_image_count{bot="bookingbot"} ' in content, content
