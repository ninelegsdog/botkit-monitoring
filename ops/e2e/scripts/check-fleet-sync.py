#!/usr/bin/env python3
"""Сверка реестра флота: канон `cases/fleet.json` фермы vs `ops/e2e/bots.yml`.

Источник истины — `cases/fleet.json` репозитория фермы. Локальная копия в
мониторинге (второй экземпляр) запрещена: канон берётся по запинненному SHA
или по пути из переменной окружения. Иначе «канон» и «копия» разойдутся
молча.

Коды возврата:
  0 — реестры совпадают;
  1 — расхождение (добавлен/удалён бот в одном из реестров);
  2 — проверить не удалось (канон недоступен / не читается / SHA не совпал).
Код 2 — НЕ «зелёный»: гейт не имеет права молчать.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CANNOT_VERIFY = 2

# SHA-256 канонического cases/fleet.json фермы. Запинен, чтобы «второй
# экземпляр» не разошёлся с каноном незаметно. Обновляется интегратором
# вместе с фермой.
PINNED_FLEET_SHA256 = "199af398d797b584e3e70e44f75cbfff563f3b3725ff64beb7f6197dc61a86c0"

ENV_FLEET_PATH = "BOTKIT_FARM_FLEET_JSON"
ENV_FARM_ROOT = "BOTKIT_FARM_ROOT"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_canonical() -> Path | None:
    """Найти канонический fleet.json: env-путь, env-корень фермы, затем
    фиксированные расположения. None — канон недоступен."""
    env_path = os.environ.get(ENV_FLEET_PATH)
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return p.resolve()
        return None

    env_root = os.environ.get(ENV_FARM_ROOT)
    if env_root:
        p = Path(env_root).expanduser() / "cases" / "fleet.json"
        if p.is_file():
            return p.resolve()
        return None

    candidates = [
        Path("/home/algtro/code/wt/farm-t8/cases/fleet.json"),
        Path("/home/algtro/code/botkit-farm/cases/fleet.json"),
        Path("/home/algtro/code/farm-t8/cases/fleet.json"),
        # относительно мониторинга: соседнее дерево фермы
        Path(__file__).resolve().parents[4] / "farm-t8" / "cases" / "fleet.json",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def load_fleet_bots(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fleet.json должен быть объектом")
    bots = data.get("bots")
    if not isinstance(bots, list) or not all(isinstance(b, str) for b in bots):
        raise ValueError("fleet.json: поле bots должно быть списком строк")
    return list(bots)


def load_bots_yml(path: Path) -> list[str]:
    """Прочитать ops/e2e/bots.yml — простой словарь 'bot: username'."""
    bots: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _ = line.split(":", 1)
        key = key.strip()
        if key:
            bots.append(key)
    return bots


def repo_root_from_script() -> Path:
    # ops/e2e/scripts/check-fleet-sync.py -> repo root
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сверка реестра флота фермы и мониторинга")
    parser.add_argument(
        "bots_yml",
        nargs="?",
        default=None,
        help="путь к ops/e2e/bots.yml (по умолчанию — из корня репозитория)",
    )
    parser.add_argument(
        "--fleet",
        default=None,
        help="путь к каноническому cases/fleet.json (перекрывает env)",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="корень репозитория мониторинга",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else repo_root_from_script()
    bots_path = Path(args.bots_yml).resolve() if args.bots_yml else (root / "ops" / "e2e" / "bots.yml")

    if args.fleet:
        canonical = Path(args.fleet).expanduser()
        if not canonical.is_file():
            print(f"канон недоступен: {canonical}", file=sys.stderr)
            return EXIT_CANNOT_VERIFY
        canonical = canonical.resolve()
    else:
        canonical = find_canonical()
        if canonical is None:
            print("канон недоступен: cases/fleet.json фермы не найден "
                  "(задайте BOTKIT_FARM_FLEET_JSON или BOTKIT_FARM_ROOT)", file=sys.stderr)
            return EXIT_CANNOT_VERIFY

    # Проверка запинненного SHA и чтение обоих реестров — в одном блоке,
    # чтобы число точек возврата не разрасталось (ruff PLR0911).
    try:
        actual_sha = sha256_file(canonical)
        if actual_sha != PINNED_FLEET_SHA256:
            print(f"расхождение канона: sha256 {actual_sha} != запиненный {PINNED_FLEET_SHA256}",
                  file=sys.stderr)
            print(f"  канон: {canonical}", file=sys.stderr)
            return EXIT_FAIL
        fleet_bots = load_fleet_bots(canonical)
        bots_yml_bots = load_bots_yml(bots_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"канон не читается: {canonical}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_VERIFY

    fleet_set = set(fleet_bots)
    bots_set = set(bots_yml_bots)

    if fleet_set != bots_set:
        print("реестр флота разошёлся:", file=sys.stderr)
        print(f"  только в fleet.json: {sorted(fleet_set - bots_set)}", file=sys.stderr)
        print(f"  только в bots.yml:    {sorted(bots_set - fleet_set)}", file=sys.stderr)
        return EXIT_FAIL

    print(f"реестр флота совпадает: {len(fleet_set)} ботов ({canonical})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
