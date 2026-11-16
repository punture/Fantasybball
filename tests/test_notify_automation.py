"""Engine-only automation for free alerts: job commands, the 15-minute lineup-guard schedule, scheduler rendering."""

import subprocess
from datetime import date

from fbm import automation as auto
from fbm.automation import Trigger
from fbm.settings import JOBS, Settings

ON = date(2026, 11, 16)


def test_engine_only_jobs_send_digests_and_alerts():
    s = Settings(automation_mode="engine-only")
    tail = [cmd[3:] for cmd in auto.plan_run("lineup-guard", s)]
    assert tail == [["alert", "lineup", "--send"], ["alert", "injuries", "--send"]]
    assert [c[3:] for c in auto.plan_run("daily-gm", s)] == [["snapshot"], ["digest", "--send"]]
    assert [c[3:] for c in auto.plan_run("weekly-retro", s)] == [
        ["learn", "retro"],
        ["digest", "--kind", "weekly", "--send"],
    ]
    assert tuple(auto.CATALOG) == JOBS  # no new job: injuries ride along with lineup-guard


def test_lineup_guard_runs_every_15_minutes_only_in_engine_only_mode():
    job = auto.CATALOG["lineup-guard"]
    engine = job.triggers("engine-only")
    assert len(engine) == 56 and engine[0] == Trigger(10, 0) and engine[-1] == Trigger(23, 45)
    assert {t.minute for t in engine} == {0, 15, 30, 45}
    assert job.triggers("scheduler") == job.et_triggers and len(job.et_triggers) == 12  # AI runs stay hourly
    assert auto.CATALOG["daily-gm"].triggers("engine-only") == auto.CATALOG["daily-gm"].et_triggers
    s = Settings(owner_timezone="America/Chicago")
    [entry] = auto.plan_entries(s, ["lineup-guard"], "engine-only", ON)
    assert len(entry.triggers) == 56 and entry.triggers[0] == Trigger(9, 0)
    [ai] = auto.plan_entries(s, ["lineup-guard"], "scheduler", ON)
    assert len(ai.triggers) == 12


def test_trigger_runs_and_descriptions():
    window = auto.CATALOG["lineup-guard"].engine_et_triggers
    assert auto.trigger_runs(window) == [(Trigger(10, 0), 15, 56)]
    assert auto.describe_triggers(window) == "daily 10:00-23:45 every 15 min"
    assert auto.describe_triggers([Trigger(5, 0, (1,))]) == "Mon 05:00"
    # Across midnight in the owner's zone (Manila: 10:00 ET = 23:00), still one run.
    manila = auto.owner_triggers("lineup-guard", "Asia/Manila", ON, "engine-only")
    assert auto.trigger_runs(manila) == [(Trigger(23, 0), 15, 56)]
    mixed = [Trigger(1, 0), Trigger(2, 0), Trigger(9, 30, (2,))]
    assert auto.trigger_runs(mixed) == [(Trigger(1, 0), 0, 1), (Trigger(2, 0), 0, 1), (Trigger(9, 30, (2,)), 0, 1)]


def test_task_scheduler_uses_one_repeating_trigger_for_the_15_minute_window(tmp_path):
    window = auto.CATALOG["lineup-guard"].engine_et_triggers
    xml = auto.task_xml("lineup-guard", window, ["C:\\py.exe", "-m", "fbm.cli"], tmp_path, ON)
    assert xml.count("<CalendarTrigger>") == 1  # Task Scheduler allows at most 48 triggers per task
    assert "<Repetition><Interval>PT15M</Interval><Duration>PT13H46M</Duration>" in xml
    assert "<StartBoundary>2026-11-16T10:00:00</StartBoundary>" in xml


def test_cron_collapses_the_window_into_one_line(tmp_path):
    window = auto.CATALOG["lineup-guard"].engine_et_triggers
    lines = auto.cron_lines("lineup-guard", window, ["/v/py", "-m", "fbm.cli"], tmp_path)
    assert len(lines) == 1
    assert lines[0].startswith("0,15,30,45 10,11,12,13,14,15,16,17,18,19,20,21,22,23 * * * cd ")
    assert lines[0].endswith("# fbm-lineup-guard")


def test_scheduler_entry_point_sees_every_command():
    """Scheduler entries run `<python> -m fbm.cli ...`: commands registered by other modules must be there too."""
    cmd = auto.fbm_command("alert", "--help")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert p.returncode == 0, p.stderr
    assert "lineup" in p.stdout and "injuries" in p.stdout


def test_launchd_lists_every_run(tmp_path):
    window = auto.CATALOG["lineup-guard"].engine_et_triggers
    plist = auto.launchd_plist("lineup-guard", window, ["/v/py"], tmp_path)
    assert plist.count("<key>Minute</key>") == 56
