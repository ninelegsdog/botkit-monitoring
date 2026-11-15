"""Regression: botkit_backup_verify_last_run_seconds должна попадать в textfile, а не в stdout.

На 14.11 три строки метрики в write_metrics() restic-verify.sh стояли вне блока
`{ ... } > "$tmp"` — уходили в stdout/journal. Тест извлекает функцию
write_metrics() из скрипта и проверяет, что метрика пишется в файл.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "ops" / "backup" / "restic-verify.sh"


def _extract_write_metrics() -> str:
    """Извлекает тело write_metrics() из restic-verify.sh."""
    content = SCRIPT.read_text(encoding="utf-8")
    start = content.index("write_metrics() {")
    # Находим закрывающую figure-skobку функции после "mv -f \"$tmp\" \"$TEXTFILE\""
    marker = "mv -f \"$tmp\" \"$TEXTFILE\"\n}"
    end = content.index(marker, start) + len(marker)
    return content[start:end]


def test_write_metrics_publishes_to_file(tmp_path) -> None:
    """write_metrics() пишет метрику в файл, а не в stdout."""
    textfile = tmp_path / "botkit_backup_verify.prom"
    func = _extract_write_metrics()

    script = tmp_path / "run.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        + func
        + f'\nTEXTFILE="{textfile}"\n'
        + 'PREFIX="[botkit-restic-verify]"\n'
        + 'declare -A OK TS\n'
        + 'OK=([data]=0 [cp]=0 [monitor]=0)\n'
        + 'TS=([data]=0 [monitor]=0)\n'
        + "write_metrics\n"
        + "exit $?\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"write_metrics failed: {result.stderr}"

    # Метрика должна быть в файле, а не в stdout.
    assert "botkit_backup_verify_last_run_seconds" not in result.stdout, (
        "метрика ушла в stdout вместо файла"
    )

    content = Path(textfile).read_text(encoding="utf-8")
    assert "botkit_backup_verify_last_run_seconds " in content
    assert "# TYPE botkit_backup_verify_last_run_seconds gauge" in content
    assert "# HELP botkit_backup_verify_last_run_seconds" in content
