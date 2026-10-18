"""The replica must be able to report its own death.

The gap this covers: replication died silently before. `replicate.sh` ran from
cron on predator, cron had no MTA, nothing scraped predator, and the monitor that
holds the offsite backups could reach it only over sftp. A run that stopped
happening looked exactly like a run that succeeded - the replica just got older.

The fix has two halves and this module breaks each of them:

* `replicate.sh` (predator) must end every run with a heartbeat file, and must
  refuse to replicate when the NTFS disk is not mounted or the container is
  looking at a different device than the host path. Both are silent by nature:
  an unmounted `nofail` disk leaves MinIO running happily on an empty directory
  of the root filesystem.
* `heartbeat-watch.sh` (monitor) must turn that file into an Uptime Kuma push -
  `up` when the replica is fresh, `down` when it is stale, failed or has never
  reported at all. Kuma notices the missing pushes too, which is the only reason
  the alarm cannot be silenced by killing the producer.

Every test runs offline: `curl` is a double recording what it was asked to send,
`restic`/`docker`/`mountpoint`/`sftp` are doubles whose exit codes drive the
branches under test. The doubles are the point - they make the failure branches
reachable at all, since a live break is exactly what we cannot schedule.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
BACKLOG = REPO / "ops" / "backlog"
WATCH = BACKLOG / "heartbeat-watch.sh"
REPLICATE = BACKLOG / "replicate.sh"


def _write_exec(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _heartbeat(tmp_path: pathlib.Path, *, age_s: int = 0, ok: int = 1, **extra: object) -> pathlib.Path:
    """A heartbeat file as `replicate.sh` writes it, with the given age faked."""
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_s)
    hb: dict[str, object] = {
        "ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "host": "predator",
        "ok": ok,
        "reason": "ok" if ok else "репликация завершилась с ошибкой",
        "mount_ok": 1,
        "device_ok": 1,
        "duration_s": 19,
        "disk_free_bytes": 1_900_000_000_000,
        "bucket_bytes": 964_689_000,
        "snapshots": {"data": 30, "cp": 12, "monitor": 5},
    }
    hb.update(extra)
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    return path


def _curl_double(tmp_path: pathlib.Path, *, code: str = "200") -> tuple[pathlib.Path, pathlib.Path]:
    """A `curl` that records its whole argv and answers with the given status code."""
    calls = tmp_path / "curl-calls"
    binary = _write_exec(
        tmp_path / "curl",
        f'printf "%s\\n" "$*" >> "{calls}"\nprintf "%s" "{code}"\n',
    )
    return binary, calls


def _run_watch(tmp_path: pathlib.Path, hb: pathlib.Path | None, *,
               url: str | None = "http://127.0.0.1:3001/api/push/deadbeef",
               url_path: pathlib.Path | None = None, maxage: int = 10800,
               curl_code: str = "200") -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    binary, calls = _curl_double(tmp_path, code=curl_code)
    if url_path is None:
        url_path = tmp_path / "kuma-push.url"
        if url is not None:
            url_path.write_text(url + "\n", encoding="utf-8")
            url_path.chmod(0o600)
    log = tmp_path / "watch.log"
    env = {
        "PATH": f"{binary.parent}:/usr/bin:/bin",
        "HB": str(hb) if hb else str(tmp_path / "missing-heartbeat.json"),
        "URL_FILE": str(url_path),
        "LOG": str(log),
        "MAXAGE": str(maxage),
    }
    proc = subprocess.run(["bash", str(WATCH)], env=env, capture_output=True, text=True, check=False)
    return proc, calls, log


def _pushed(calls: pathlib.Path) -> str:
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


# --- сторож на мониторе: вердикт и push в Kuma -------------------------------------

def test_fresh_healthy_heartbeat_pushes_up(tmp_path):
    proc, calls, _ = _run_watch(tmp_path, _heartbeat(tmp_path))
    assert proc.returncode == 0, proc.stderr
    sent = _pushed(calls)
    assert "status=up" in sent, sent
    assert "snapshots=cp=12,data=30,monitor=5" in sent, sent


def test_stale_heartbeat_pushes_down(tmp_path):
    """The whole product of this change: silence becomes a signal."""
    proc, calls, _ = _run_watch(tmp_path, _heartbeat(tmp_path, age_s=4 * 3600))
    assert proc.returncode == 0, proc.stderr
    assert "status=down" in _pushed(calls)
    assert "устарел" in _pushed(calls)


def test_failed_run_pushes_down(tmp_path):
    proc, calls, _ = _run_watch(tmp_path, _heartbeat(tmp_path, ok=0))
    assert proc.returncode == 0
    sent = _pushed(calls)
    assert "status=down" in sent and "ошибкой" in sent, sent


@pytest.mark.parametrize(
    "field",
    ["mount_ok", "device_ok"],
)
def test_wrong_storage_pushes_down(tmp_path, field):
    """A replica on the root filesystem is the silent failure P0-2 exists for."""
    proc, calls, _ = _run_watch(tmp_path, _heartbeat(tmp_path, **{field: 0}))
    assert proc.returncode == 0
    assert "status=down" in _pushed(calls)


def test_missing_heartbeat_pushes_down(tmp_path):
    proc, calls, _ = _run_watch(tmp_path, None)
    assert proc.returncode == 0
    assert "status=down" in _pushed(calls)
    assert "отсутствует" in _pushed(calls)


def test_unreadable_heartbeat_pushes_down(tmp_path):
    hb = tmp_path / "heartbeat.json"
    hb.write_text("{не json", encoding="utf-8")
    proc, calls, _ = _run_watch(tmp_path, hb)
    assert proc.returncode == 0
    assert "status=down" in _pushed(calls)


def test_heartbeat_without_timestamp_pushes_down(tmp_path):
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ok": 1}', encoding="utf-8")
    proc, calls, _ = _run_watch(tmp_path, hb)
    assert proc.returncode == 0
    assert "status=down" in _pushed(calls)


# --- носитель прав: push-URL. Ошибка настройки обязана быть громкой -----------------

def test_missing_url_file_fails_loudly_without_pushing(tmp_path):
    proc, calls, log = _run_watch(tmp_path, _heartbeat(tmp_path), url=None)
    assert proc.returncode == 1
    assert _pushed(calls) == "", "push без настроенного URL — это подделка тревоги"
    assert "не заведён" in log.read_text(encoding="utf-8")


def test_non_loopback_url_is_refused(tmp_path):
    """Kuma слушает только на 127.0.0.1: чужой URL — подмена носителя, а не настройка."""
    proc, calls, log = _run_watch(
        tmp_path, _heartbeat(tmp_path), url="http://evil.example.com/api/push/x"
    )
    assert proc.returncode == 1
    assert _pushed(calls) == ""
    assert "evil.example.com" not in log.read_text(encoding="utf-8"), "URL не должен попадать в лог"


def test_failed_push_is_not_silent(tmp_path):
    proc, _calls, log = _run_watch(tmp_path, _heartbeat(tmp_path), curl_code="500")
    assert proc.returncode == 1
    assert "http=500" in log.read_text(encoding="utf-8")


def test_url_never_lands_in_the_log(tmp_path):
    """Push-URL — носитель прав: кто его прочитал, тот молчание подделает."""
    _, _, log = _run_watch(tmp_path, _heartbeat(tmp_path))
    assert "deadbeef" not in log.read_text(encoding="utf-8")


# --- producer: гейт хранилища в replicate.sh ---------------------------------------
@pytest.fixture()
def producer_env(tmp_path):
    """A predator-shaped environment with storage/restic/sftp replaced by doubles."""
    secrets = tmp_path / "config"
    secrets.mkdir()
    (secrets / "minio.env").write_text("MINIO_ROOT_USER=probe\nMINIO_ROOT_PASSWORD=probe\n")
    (secrets / "target.pw").write_text("probe\n")
    (secrets / "data.pw").write_text("probe\n")
    state = tmp_path / "state"
    data = tmp_path / "data"
    data.mkdir()
    calls = tmp_path / "restic-calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    restic_body = (
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'case "$*" in\n'
        '  *snapshots*--json*) echo "[{}]" ;;\n'
        '  *list*locks*) echo "" ;;\n'
        '  *snapshots*) printf "%s\\n" 1 ;;\n'
        '  *copy*|*init*|*unlock*) exit 0 ;;\n'
        '  *) echo "" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    _write_exec(bin_dir / "restic", restic_body)
    _write_exec(bin_dir / "sftp", "exit 0\n")
    _write_exec(bin_dir / "mountpoint", "exit ${MOUNTPOINT_RC:-0}\n")
    _write_exec(bin_dir / "docker", 'echo "${DOCKER_DEV:-0}"\n')
    return {
        "bin": bin_dir,
        "secrets": secrets,
        "state": state,
        "data": data,
        "calls": calls,
        "hb": state / "heartbeat.json",
    }


def _run_replicate(producer_env, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{producer_env['bin']}:/usr/bin:/bin",
        "HOME": str(producer_env["state"].parent),
        "SECRETS": str(producer_env["secrets"]),
        "STATEDIR": str(producer_env["state"]),
        "MOUNT": str(producer_env["state"].parent / "mnt"),
        "DATA": str(producer_env["data"]),
        "REPOS": "data",
        "RESTIC": str(producer_env["bin"] / "restic"),
        **overrides,
    }
    # Устройство хоста берём настоящее: гейт сверяет именно его с ответом контейнера.
    env["DOCKER_DEV"] = overrides.pop("DOCKER_DEV", str(producer_env["data"].stat().st_dev))
    return subprocess.run(["bash", str(REPLICATE)], env=env, capture_output=True, text=True, check=False)


def test_replicate_refuses_to_run_without_the_disk(producer_env):
    """`nofail` in fstab means boot continues: without this gate MinIO writes into the root fs."""
    proc = _run_replicate(producer_env, MOUNTPOINT_RC="1")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    hb = json.loads(producer_env["hb"].read_text(encoding="utf-8"))
    assert hb["ok"] == 0 and hb["mount_ok"] == 0
    assert "не примонтирован" in hb["reason"]
    assert "FATAL" in producer_env["state"].joinpath("replicate.log").read_text(encoding="utf-8") \
        or proc.stdout, "отказ обязан быть виден в логе"
    assert not producer_env["calls"].exists(), "restic не должен трогать репозитории при снятом диске"


def test_replicate_refuses_when_container_sees_another_device(producer_env):
    """Container started before the mount: same path, different filesystem."""
    proc = _run_replicate(producer_env, DOCKER_DEV=str(producer_env["data"].stat().st_dev + 12345))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    hb = json.loads(producer_env["hb"].read_text(encoding="utf-8"))
    assert hb["ok"] == 0 and hb["device_ok"] == 0
    assert "другое устройство" in hb["reason"]
    assert not producer_env["calls"].exists(), "репликация обязана встать, а не писать мимо"


def test_replicate_heartbeat_survives_a_failed_snapshot_counter(producer_env):
    """`count_snaps` упал → «?», и heartbeat обязан быть записан, а не упасть вместе с ним.

    Формат серийного JSON из строк (склеивание `{"data":<значение>}`) даёт документ
    вида `{"data":?}`, который json.loads отвергает: write_hb умирает под `set -e`,
    heartbeat не остаётся вовсе, а сторож видит просто старый файл.
    """
    _write_exec(
        producer_env["bin"] / "restic",
        'printf "%s\\n" "$*" >> "' + str(producer_env["calls"]) + '"\n'
        'case "$*" in *snapshots*--json*) echo "not json" ;; esac\n'
        "exit 0\n",
    )
    proc = _run_replicate(producer_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hb = json.loads(producer_env["hb"].read_text(encoding="utf-8"))
    assert hb["ok"] == 1
    assert hb["snapshots"] == {"data": "?"}, hb["snapshots"]


def test_replicate_reports_success_with_a_heartbeat(producer_env):
    proc = _run_replicate(producer_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    hb = json.loads(producer_env["hb"].read_text(encoding="utf-8"))
    assert hb["ok"] == 1 and hb["mount_ok"] == 1 and hb["device_ok"] == 1
    assert hb["snapshots"] == {"data": 1}, hb["snapshots"]
    # Каждый репозиторий обязан быть переснят, и локи сняты до копирования.
    calls = producer_env["calls"].read_text(encoding="utf-8")
    assert "copy" in calls and "unlock" in calls, calls


def test_replicate_ignores_concurrent_run(producer_env):
    """Два прогона на одном WORM-репозитории — это гонка за локи, а не скорость."""
    lock_dir = producer_env["state"]
    lock_dir.mkdir(parents=True, exist_ok=True)
    # Держим лок настоящим flock: условие наступает гарантированно, без гонки и без сна.
    with open(lock_dir / "replicate.lock", "w", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = _run_replicate(producer_env)
    assert proc.returncode == 75, proc.stdout + proc.stderr
    assert "УЖЕ ВЫПОЛНЯЕТСЯ" in producer_env["state"].joinpath("replicate.log").read_text(encoding="utf-8")


# --- канон ↔ развёрнутая копия: юнит обязан указывать на файл из репозитория -------

def test_units_point_at_scripts_that_exist_in_the_canon():
    """Юнит, чей ExecStart не существует, — это тревога, которой нет (инцидент 26.09)."""
    wired = {
        "botkit-backlog-heartbeat.service": BACKLOG / "heartbeat-watch.sh",
    }
    readme = (BACKLOG / "README.md").read_text(encoding="utf-8")
    for unit_name, script in wired.items():
        text = (BACKLOG / "systemd" / unit_name).read_text(encoding="utf-8")
        installed = f"/usr/local/sbin/botkit-backlog-{script.stem}"
        assert installed in text, f"{unit_name} не запускает {installed}"
        assert script.is_file(), f"{script} отсутствует в каноне"
        # Канон и инструкция установки — один стык: копия обязана ставиться туда,
        # откуда её берёт юнит, иначе на хосте окажется файл, который никто не читает.
        assert installed in readme, f"README не устанавливает {installed}"


def test_stack_unit_owns_the_container_lifecycle():
    """Пока контейнер поднимает docker restart-policy, диск монтируется позже контейнера."""
    unit = (BACKLOG / "systemd" / "botkit-backlog-stack.service").read_text(encoding="utf-8")
    compose = (BACKLOG / "compose.yml").read_text(encoding="utf-8")
    assert "RequiresMountsFor=/mnt/storage" in unit, "порядок с монтированием не задан"
    assert 'restart: "no"' in compose, "docker обязан ждать юнит, а не стартовать сам"
    for directive in ("ExecStart=", "ExecStop=", "ExecStartPre="):
        assert directive in unit, f"нет {directive}"


def test_heartbeat_timer_runs_often_enough_to_notice():
    timer = (BACKLOG / "systemd" / "botkit-backlog-heartbeat.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=5min" in timer
    assert "WantedBy=timers.target" in timer
    service = (BACKLOG / "systemd" / "botkit-backlog-heartbeat.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "ExecStart=" in service
