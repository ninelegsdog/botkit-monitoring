"""Контракт на общий механизм согласованной копии (ops/lib/snapshot.sh).

До 05.10.2026 у двух потоков бэкапа была своя логика, и у каждой — свой способ
сломаться без всякого сигнал:

- restic-backup писал снимок в /app/backups — bind-mount хоста. Хостовой каталог
  остался root:root 700 после общего chown -R 03.10, контейнер работает от
  1001:1001, и все девять согласованных снимков тихо перестали создаваться на два
  дня: проверки зеленели, потому что пробовали только /app/data;
- backup_bots брал обычный cp с хоста — копия SQLite, снятая в момент записи,
  бывает надорванной, и integrity_check после cp такое не всегда ловит.

Оба потока теперь обязаны идти через одну функцию. Здесь проверяется её поведение
(путь через tmpfs контейнера, фолбэк на хост, отказ вместо надорванной копии) и то,
что оба скрипта действительно её подключили, а cp из backup_bots исчез.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SNAPSHOT_SH = REPO / "ops" / "lib" / "snapshot.sh"
RESTIC_BACKUP_SH = REPO / "ops" / "backup" / "restic-backup.sh"
BACKUP_BOTS_SH = REPO / "ops" / "backup" / "backup_bots.sh"

# docker в песочнице: inspect/exec, но поведение управляется STUB_DOCKER_MODE.
#   ok      — контейнер отвечает, exec выполняет python3/cat на хосте (пути в тесте
#             указывают на хост, это тот же файл, что и bind-mount);
#   broken  — exec не работает: контейнер есть, но снимок внутри него не получить;
#   down    — контейнер не запущен (inspect падает).
# Ветки `cp` в заглушке намеренно нет: docker cp из tmpfs читать не умеет (см.
# комментарий в snapshot.sh) — регресс на cp должен всплыть именно как поломка пути.
DOCKER_STUB = """#!/usr/bin/env bash
mode="${STUB_DOCKER_MODE:-ok}"
cmd="$1"; shift
case "$cmd" in
  inspect) [ "$mode" = "down" ] && exit 1; exit 0 ;;
  exec)
    shift                       # имя контейнера
    sub="$1"; shift             # python3 | cat | rm
    case "$sub" in
      python3) [ "$mode" = "broken" ] && exit 1; exec python3 "$@" ;;
      cat)     [ "$mode" = "broken" ] && exit 1; exec cat "$@" ;;
      rm)      exec rm -f "$@" ;;
    esac
    exit 1 ;;
  *) exit 1 ;;
esac
"""


def _make_db(path: pathlib.Path, rows: int = 200) -> None:
    con = sqlite3.connect(path)
    con.execute("create table t (v text)")
    con.executemany("insert into t values (?)", [(f"value-{i}",) for i in range(rows)])
    con.commit()
    con.close()


def _integrity(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def _run(tmp_path: pathlib.Path, mode: str, *, src_kind: str = "db") -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)

    src_host = tmp_path / "bot.db"
    if src_kind == "db":
        _make_db(src_host)
    elif src_kind == "empty":
        src_host.write_bytes(b"")
    else:
        src_host.write_text("это не база, а просто текст\n")

    dest = tmp_path / "out" / "export.db"
    dest.parent.mkdir(exist_ok=True)
    src_ctr = str(src_host)  # в тесте «контейнерный» путь указывает на тот же файл

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_DOCKER_MODE"] = mode
    cmd = (
        f'. "{SNAPSHOT_SH}" && '
        f'consistent_snapshot botkit-fake "{src_ctr}" "{src_host}" "{dest}"'
    )
    result = subprocess.run(
        ["bash", "-c", cmd], env=env, capture_output=True, text=True, check=False
    )
    return result


def test_container_path_wins_and_produces_valid_snapshot(tmp_path: pathlib.Path) -> None:
    """Основной путь: снимок внутри контейнера (tmpfs), затем docker cp на хост."""
    result = _run(tmp_path, "ok")

    dest = tmp_path / "out" / "export.db"
    assert result.returncode == 0, f"снимок не создан:\n{result.stderr}"
    assert _integrity(dest) == "ok"
    assert "пробую хост" not in result.stderr, (
        "фолбэк не должен срабатывать, пока контейнер отвечает:\n" + result.stderr
    )


def test_falls_back_to_host_when_container_exec_fails(tmp_path: pathlib.Path) -> None:
    """Контейнер есть, но внутри снимок не получить — берём хостовой python3."""
    result = _run(tmp_path, "broken")

    dest = tmp_path / "out" / "export.db"
    assert result.returncode == 0, f"фолбэк не сработал:\n{result.stderr}"
    assert _integrity(dest) == "ok"
    assert "пробую хост" in result.stderr, "фолбэк молчит — оператор его не увидит"


def test_works_when_container_is_down(tmp_path: pathlib.Path) -> None:
    """Бэкап остановленного бота всё равно нужен: docker inspect падает, копия есть."""
    result = _run(tmp_path, "down")

    dest = tmp_path / "out" / "export.db"
    assert result.returncode == 0, f"остановленный бот не скопирован:\n{result.stderr}"
    assert _integrity(dest) == "ok"


def test_torn_or_missing_source_is_refused_not_faked(tmp_path: pathlib.Path) -> None:
    """Надорванная/не-база: отказ и удаление файла. cp в ход не идёт."""
    result = _run(tmp_path, "down", src_kind="text")

    dest = tmp_path / "out" / "export.db"
    assert result.returncode != 0, "нечитаемый источник дал «успех» — ложная зелень"
    assert not dest.exists(), "битый файл остался на месте и его можно принять за бэкап"


def test_empty_source_is_refused(tmp_path: pathlib.Path) -> None:
    """Нулевой файл: PRAGMA integrity_check на пустой базе отвечает «ok».

    Именно поэтому в функции есть проверка размера, а не только integrity_check:
    бот с удалённым bot.db получал бы «успешный» бэкап из ничего.
    """
    result = _run(tmp_path, "down", src_kind="empty")

    dest = tmp_path / "out" / "export.db"
    assert result.returncode != 0, "пустой источник дал «успех»"
    assert not dest.exists(), "пустой файл остался в backups"


def test_docker_cp_is_not_used_because_it_cannot_read_tmpfs() -> None:
    """docker cp не умеет читать из tmpfs контейнера — проверено на проде 05.10.

    Daemon архивирует слои образа, а не mount-namespace работающего контейнера:
    файл в /tmp есть, exec его видит, docker cp отвечает «Could not find the file».
    Возврат к cp молча перевёл бы весь поток на хостовой фолбэк и вернул бы
    зависимость от хоста, которую этот механизм и убирал.
    """
    shell = SNAPSHOT_SH.read_text()
    executable = "\n".join(
        line for line in shell.splitlines() if not line.lstrip().startswith("#")
    )
    assert "docker cp" not in executable, (
        "snapshot.sh снова использует docker cp — он не видит содержимое tmpfs"
    )


def test_both_backup_scripts_use_the_shared_mechanism() -> None:
    """П.1.3 плана: одна процедура, один источник истины в обоих потоках."""
    restic = RESTIC_BACKUP_SH.read_text()
    bots = BACKUP_BOTS_SH.read_text()

    for name, text in (("restic-backup", restic), ("backup_bots", bots)):
        assert "lib/snapshot.sh" in text, f"{name} не подключил общий механизм"
        assert "consistent_snapshot" in text, f"{name} не вызывает consistent_snapshot"

    assert 'cp "$d/data/bot.db"' not in bots, (
        "backup_bots снова копирует cp — надорванная копия вернулась"
    )
    # Снимок не должен больше зависеть от владельца хостового каталога backups.
    assert "/app/backups/export" not in restic, (
        "restic-backup снова пишет снимок в bind-mount /app/backups"
    )
