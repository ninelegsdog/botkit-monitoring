"""Contract tests for the certificate deploy path.

The 25.09 certificate change is the mechanical cause of a 15-day outage: the certificate
reached nginx, the fleet was recreated, and nothing ever told Telegram that the pinned
self-signed public key was obsolete. Two failures had to be pinned down here:

  * the deploy hook could not run at all (no bash in the certbot image, rc=127), so the
    automated renewal was dead and only a human had ever installed a certificate;
  * the hook recreated all nine bot containers, which is noise, not safety.

These tests use throwaway certificates and never touch nginx, docker or Telegram.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2]
DEPLOY = OPS / "tls" / "certbot-deploy.sh"
UNIT = OPS / "tls" / "systemd" / "botkit-certbot-renew.service"

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("bash") is None,
    reason="bash and openssl are required to exercise the deploy script",
)


def make_cert(directory: Path, common_name: str = "fixture.test") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(directory / "privkey.pem"),
            "-out", str(directory / "fullchain.pem"),
            "-days", "30", "-subj", f"/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )


def copy_pair(src: Path, dst: Path) -> None:
    """Same certificate in both places.

    Generating twice is not the same fixture: each `openssl req -newkey` invents a
    different RSA key, so "identical" fixtures would silently mean "changed" ones.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("fullchain.pem", "privkey.pem"):
        shutil.copy(src / name, dst / name)


def run_deploy(
    src: Path,
    dst: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    docker_rc: dict[str, int] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "BOTKIT_CERT_SRC": str(src),
        "BOTKIT_CERT_DST": str(dst),
        "BOTKIT_FLEET_ENV": str(OPS / "lib" / "fleet.env"),
    }
    env.update(env_extra or {})
    if docker_rc is not None:
        env["PATH"] = f"{fake_docker(tmp_path_of(src), docker_rc)}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(DEPLOY), *args], capture_output=True, text=True, check=False, env=env
    )


def tmp_path_of(path: Path) -> Path:
    """src lives at <tmp>/src, so its parent is the per-test temporary directory."""
    return path.parent


def fake_docker(directory: Path, behaviour: dict[str, int]) -> Path:
    """A docker stub on PATH.

    The install/reload/verify path is the part that matters, and it only reaches nginx
    through the docker CLI. Stubbing the CLI exercises the real script end to end
    without needing a container runtime, and lets a test make `nginx -t` fail on
    demand - which is the only way to prove the rollback actually restores the old pair.
    """
    stub_dir = directory / "fakebin"
    stub_dir.mkdir(exist_ok=True)
    cases = "\n".join(
        f'  *"{needle}"*) exit {code} ;;' for needle, code in behaviour.items()
    )
    (stub_dir / "docker").write_text(
        "#!/bin/sh\n"
        'echo "docker $*" >> "$(dirname "$0")/docker.log"\n'
        f'case "$*" in\n{cases}\n  *) exit 0 ;;\nesac\n'
    )
    (stub_dir / "docker").chmod(0o755)
    return stub_dir


def stub_check(directory: Path, rc: int, name: str = "check.sh") -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho \"checker ran\"\nexit {rc}\n")
    path.chmod(0o755)
    return path


def changed_fixture(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src, "new.test")
    make_cert(dst, "old.test")
    return src, dst


def script_text() -> str:
    return DEPLOY.read_text()


def unit_directives() -> list[str]:
    """Non-comment lines only: the comments explain the old failure on purpose."""
    return [
        ln.strip()
        for ln in UNIT.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(DEPLOY)], check=False).returncode == 0


def test_unchanged_certificate_is_a_noop(tmp_path):
    """Twice-daily timer: a day with no renewal due must stay green and change nothing."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src)
    copy_pair(src, dst)
    before = dst.joinpath("fullchain.pem").read_bytes()

    result = run_deploy(src, dst, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNCHANGED" in result.stdout
    assert dst.joinpath("fullchain.pem").read_bytes() == before
    assert not list(dst.glob("*.pre-*")), "an unchanged certificate must not create backups"


def test_changed_certificate_is_detected(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src, "new.test")
    make_cert(dst, "old.test")

    result = run_deploy(src, dst, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHANGED" in result.stdout
    assert "would back up" in result.stdout, "dry-run must state the plan, not just the verdict"


def test_force_reinstalls_an_identical_certificate(tmp_path):
    """--force exists to exercise the real path against the live certificate."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src)
    copy_pair(src, dst)

    result = run_deploy(src, dst, "--dry-run", "--force")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reinstalling on purpose" in result.stdout


def test_mismatched_key_and_certificate_is_fatal(tmp_path):
    """A swapped private key must never reach nginx."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src, "cert.test")
    other = tmp_path / "other"
    make_cert(other, "other.test")
    shutil.copy(other / "privkey.pem", src / "privkey.pem")

    result = run_deploy(src, dst, "--dry-run")

    assert result.returncode == 1
    assert "do not match" in result.stderr


def test_certificate_too_close_to_expiry_is_refused(tmp_path):
    """Deploying a certificate that dies within a day turns a renewal into an outage.

    The threshold is passed through the environment rather than by editing the script:
    a test that rewrites the file it is testing can leave it corrupted for the next run,
    which is exactly how every other fixture in this file started failing.
    """
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src, "soon.test")
    make_cert(dst, "old.test")

    result = run_deploy(src, dst, "--dry-run", env_extra={"BOTKIT_CERT_MIN_VALID_SECONDS": "99999999"})

    assert result.returncode == 1
    assert "refusing to deploy" in result.stderr
    assert dst.joinpath("fullchain.pem").exists(), "nothing may be installed on a refusal"


def test_unknown_argument_is_rejected(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    make_cert(src)
    copy_pair(src, dst)
    result = run_deploy(src, dst, "--whatever")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_deploy_no_longer_recreates_the_fleet():
    """The certificate terminates at nginx; the bots never read it."""
    assert "force-recreate" not in script_text()
    assert "docker compose" not in script_text(), "the hook must not deploy the fleet"


def test_real_path_installs_reloads_and_verifies(tmp_path):
    """The full success path: install, nginx -t, reload, then the delivery contract."""
    src, dst = changed_fixture(tmp_path)
    check = stub_check(tmp_path, 0)
    old_bytes = dst.joinpath("fullchain.pem").read_bytes()

    result = run_deploy(
        src,
        dst,
        env_extra={"BOTKIT_WEBHOOK_CHECK": str(check)},
        docker_rc={"nginx -t": 0},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "delivery contract verified" in result.stdout
    assert dst.joinpath("fullchain.pem").read_bytes() == src.joinpath("fullchain.pem").read_bytes()
    assert dst.joinpath("fullchain.pem").read_bytes() != old_bytes
    backups = list(dst.glob("fullchain.pem.pre-*"))
    assert len(backups) == 1, "the previous pair must be kept for rollback"
    assert backups[0].read_bytes() == old_bytes

    log = tmp_path_of(src).joinpath("fakebin", "docker.log").read_text()
    assert "nginx -t" in log, "the new pair must be validated before it is used"
    assert "-s reload" in log, "nginx must actually be reloaded"


def test_failed_delivery_contract_is_fatal_with_a_remediation(tmp_path):
    """This is the incident's shape: the certificate ships, delivery does not work."""
    src, dst = changed_fixture(tmp_path)
    check = stub_check(tmp_path, 1)

    result = run_deploy(
        src, dst, env_extra={"BOTKIT_WEBHOOK_CHECK": str(check)}, docker_rc={"nginx -t": 0}
    )

    assert result.returncode == 1
    assert "delivery contract FAILED" in result.stderr
    assert "has_custom_certificate" in result.stderr, "the message must name the failing check"
    assert "setWebhook" in result.stderr, "and must say what to do about it"


def test_nginx_rejection_rolls_the_previous_pair_back(tmp_path):
    """A bad pair must never become the one nginx serves, even transiently."""
    src, dst = changed_fixture(tmp_path)
    check = stub_check(tmp_path, 0)
    old_bytes = dst.joinpath("fullchain.pem").read_bytes()
    old_key = dst.joinpath("privkey.pem").read_bytes()

    result = run_deploy(
        src, dst, env_extra={"BOTKIT_WEBHOOK_CHECK": str(check)}, docker_rc={"nginx -t": 1}
    )

    assert result.returncode == 1
    assert "rolled back" in result.stderr
    assert dst.joinpath("fullchain.pem").read_bytes() == old_bytes
    assert dst.joinpath("privkey.pem").read_bytes() == old_key


def test_missing_checker_is_fatal_rather_than_a_silent_skip(tmp_path):
    """No verification available is not the same as verification passing."""
    src, dst = changed_fixture(tmp_path)
    result = run_deploy(
        src,
        dst,
        env_extra={"BOTKIT_WEBHOOK_CHECK": str(tmp_path / "absent.sh")},
        docker_rc={"nginx -t": 0},
    )
    assert result.returncode == 1
    assert "cannot verify the public path" in result.stderr


def test_unit_runs_the_deploy_on_the_host_not_in_the_container():
    effective = "\n".join(unit_directives())
    assert "--deploy-hook" not in effective, (
        "an in-container deploy hook cannot run: the image has no bash and no docker"
    )
    assert re.search(r"^ExecStartPost=.*certbot-deploy\.sh", effective, re.MULTILINE), (
        "the host-side deploy must be wired in or a renewal ships nothing"
    )


def test_renewal_container_gets_no_docker_socket():
    assert "/var/run/docker.sock" not in "\n".join(unit_directives()), (
        "a renew job does not need host root to write a certificate file"
    )


def test_renewal_image_is_pinned_by_digest():
    text = "\n".join(unit_directives())
    assert re.search(r"certbot/certbot@sha256:[0-9a-f]{64}", text), "the image must stay pinned"
