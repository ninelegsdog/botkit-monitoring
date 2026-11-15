"""Contract: каждый runbook_url в alerts.yml резолвится в существующий файл.

Регрессия из ветки prevention: 46 alert-правил получили runbook_url, но 39 ссылок
вели на несуществующий каталог docs/runbooks, 7 — на несуществующие файлы
(drift.md, healthcheck.md, rollout-check.md, image-prune.md, backup-verify.md).
Тест ловит absence цели: ссылка на каталог без файла или на удалённый/переименованный
файл падает тестом, а не только проходит проверку «есть строка runbook_url».
"""

from __future__ import annotations

import pathlib
import re
import urllib.parse

import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
ALERTS = REPO / "prometheus" / "alerts.yml"

_GITHUB_BLOB = re.compile(
    r"^https://github\.com/ninelegsdog/botkit-monitoring/blob/main/(.+)$"
)
_GITHUB_TREE = re.compile(
    r"^https://github\.com/ninelegsdog/botkit-monitoring/tree/main/(.+)$"
)


def _rules() -> list[dict]:
    doc = yaml.safe_load(ALERTS.read_text())
    out = []
    for group in doc.get("groups", []):
        out.extend(group.get("rules", []))
    return [r for r in out if "alert" in r]


def _runbook_urls() -> list[tuple[str, str]]:
    """Возвращает список (alert_name, runbook_url) для всех правил."""
    found = []
    for rule in _rules():
        url = (rule.get("annotations") or {}).get("runbook_url", "")
        if url:
            found.append((rule["alert"], url))
    return found


def _resolve(url: str) -> pathlib.Path | None:
    """Разрешает GitHub URL в локальный путь REPO/..., или None если не GitHub."""
    m = _GITHUB_BLOB.match(url) or _GITHUB_TREE.match(url)
    if not m:
        return None
    rel = urllib.parse.unquote(m.group(1))
    # Убираем якорь (#L123) если есть
    if "#" in rel:
        rel = rel.split("#", 1)[0]
    return REPO / rel


def test_every_runbook_url_resolves_to_existing_file() -> None:
    """Каждый runbook_url указывает на существующий файл в репозитории."""
    bad = []
    for alert_name, url in _runbook_urls():
        path = _resolve(url)
        if path is None:
            bad.append(f"{alert_name}: не-GitHub runbook_url: {url}")
            continue
        if not path.is_file():
            bad.append(f"{alert_name}: цель не существует: {path} ({url})")
    assert not bad, "runbook_url без существующей цели:\n  " + "\n  ".join(bad)


def test_no_runbook_url_points_to_directory() -> None:
    """runbook_url не должен вести на каталог docs/runbooks (без конкретного файла).

    Ссылка на каталог — это мёртвая ссылка: при клике попадает на список файлов,
    а не на процедуру реагирования.
    """
    bad = []
    for alert_name, url in _runbook_urls():
        path = _resolve(url)
        if path is None:
            continue
        if path.is_dir():
            bad.append(f"{alert_name}: runbook_url ведёт на каталог, а не на файл: {path}")
    assert not bad, "runbook_url на каталог вместо файла:\n  " + "\n  ".join(bad)
