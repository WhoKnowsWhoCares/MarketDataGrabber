"""Tests for deploy/crontab.mdg and deploy/install_cron.sh."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CRONTAB = REPO / "deploy" / "crontab.mdg"
INSTALLER = REPO / "deploy" / "install_cron.sh"

# script name -> path relative to repo root, and expected cron schedule (MSK)
EXPECTED_JOBS = {
    "increment_candles": ("scripts/increment_candles.py", "15 0 * * 1-5"),
    "run_marts": ("scripts/run_marts.py", "35 0 * * *"),  # появится из P3.6a
    "run_dq": ("transforms/run_dq.py", "45 0 * * *"),
    "collect_fundamentals": ("scripts/collect_fundamentals.py", "30 6 * * *"),
    "collect_events": ("scripts/collect_events.py", "0 4 * * 0"),
    "watchdog": ("scripts/watchdog.py", "0 * * * *"),
    "daily_digest": ("scripts/daily_digest.py", "10 9 * * *"),
}

# единственный скрипт, которого может ещё не быть в репо ( ждём P3.6a )
PENDING = {"scripts/run_marts.py"}


def job_lines() -> list[str]:
    """Non-comment, non-empty lines of crontab.mdg (i.e. the job lines)."""
    return [
        ln
        for ln in CRONTAB.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_every_job_line_has_marker() -> None:
    lines = job_lines()
    assert len(lines) == 7, f"expected 7 job lines, got {len(lines)}"
    for ln in lines:
        assert "MDG:auto" in ln, f"job line without marker: {ln[:60]}..."


def test_expected_scripts_and_schedules() -> None:
    lines = job_lines()
    for name, (path, schedule) in EXPECTED_JOBS.items():
        matches = [ln for ln in lines if path in ln]
        assert len(matches) == 1, f"{name}: expected exactly one crontab line for {path}"
        fields = matches[0].split()
        assert " ".join(fields[:5]) == schedule, f"{name}: schedule {fields[:5]!r} != {schedule!r}"


def test_script_paths_exist() -> None:
    mentioned = set(re.findall(r"(?:scripts|transforms)/[\w.]+\.py", CRONTAB.read_text(encoding="utf-8")))
    assert mentioned == set(path for path, _ in EXPECTED_JOBS.values())
    missing = [p for p in sorted(mentioned) if not (REPO / p).is_file()]
    assert missing == sorted(PENDING), f"missing scripts other than pending {PENDING}: {missing}"


def test_installer_syntax() -> None:
    assert INSTALLER.is_file()
    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_installer_is_executable() -> None:
    import os

    assert os.access(INSTALLER, os.X_OK), "install_cron.sh must be executable"
