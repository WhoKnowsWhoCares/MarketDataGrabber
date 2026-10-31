"""Tests for deploy/crontab.mdg and deploy/install_cron.sh."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CRONTAB = REPO / "deploy" / "crontab.mdg"
INSTALLER = REPO / "deploy" / "install_cron.sh"

# script name -> [(path relative to repo root, expected cron schedule (MSK)), ...]
# a script may appear on more than one line (e.g. nightly + hourly runs)
EXPECTED_JOBS: dict[str, list[tuple[str, str]]] = {
    "increment_candles": [
        ("scripts/increment_candles.py", "15 0 * * 1-6"),  # nightly full catch-up
        ("scripts/increment_candles.py", "0 10-19 * * 1-6"),  # hourly intraday (P6.3a)
    ],
    "run_marts": [
        ("scripts/run_marts.py", "35 0 * * *"),
        ("scripts/run_marts.py", "5 10-19 * * 1-6"),  # hourly refresh for gold dashboard (P6.3a)
    ],
    "run_signals": [("scripts/run_signals.py", "40 0 * * *")],
    "run_dq": [("transforms/run_dq.py", "45 0 * * *")],
    "collect_fundamentals": [("scripts/collect_fundamentals.py", "30 6 * * *")],
    "collect_events": [("scripts/collect_events.py", "0 4 * * 0")],
    "watchdog": [("scripts/watchdog.py", "0 * * * *")],
    "daily_digest": [("scripts/daily_digest.py", "10 9 * * *")],
}

TOTAL_JOB_LINES = sum(len(entries) for entries in EXPECTED_JOBS.values())

# единственный скрипт, которого может ещё не быть в репо ( ждём P3.6a )
PENDING: set[str] = set()  # P3.6a смёржена — run_marts.py существует


def job_lines() -> list[str]:
    """Non-comment, non-empty lines of crontab.mdg (i.e. the job lines)."""
    return [
        ln
        for ln in CRONTAB.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_every_job_line_has_marker() -> None:
    lines = job_lines()
    assert len(lines) == TOTAL_JOB_LINES, f"expected {TOTAL_JOB_LINES} job lines, got {len(lines)}"
    for ln in lines:
        assert "MDG:auto" in ln, f"job line without marker: {ln[:60]}..."


def test_expected_scripts_and_schedules() -> None:
    lines = job_lines()
    for name, entries in EXPECTED_JOBS.items():
        for path, schedule in entries:
            matches = [ln for ln in lines if path in ln and ln.split()[:5] == schedule.split()]
            assert len(matches) == 1, f"{name}: expected exactly one crontab line {schedule!r} for {path}"


def test_no_duplicate_schedule_per_script() -> None:
    """The same script must not share an identical schedule on two lines."""
    lines = job_lines()
    for entries in EXPECTED_JOBS.values():
        for path, schedule in entries:
            key = (path, tuple(schedule.split()))
            hits = [ln for ln in lines if path in ln and ln.split()[:5] == list(key[1])]
            assert len(hits) == 1, f"duplicate schedule {schedule!r} for {path}"


def test_script_paths_exist() -> None:
    mentioned = set(re.findall(r"(?:scripts|transforms)/[\w.]+\.py", CRONTAB.read_text(encoding="utf-8")))
    assert mentioned == {path for entries in EXPECTED_JOBS.values() for path, _ in entries}
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
