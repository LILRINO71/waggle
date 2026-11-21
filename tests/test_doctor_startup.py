"""`waggle doctor`'s "Startup" line. Since 2026-11-20 the gateway starts at logon from the scheduled task
"Waggle Gateway", not from the Startup folder; the check still looked in the folder, warned "not starting at
login" on a machine where it did, and told the user to run the old installer, which would have put back the very
launchers that were moved out."""
import subprocess
import types

from waggle import doctor


def _startup_folder(tmp_path, monkeypatch, *files):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    folder.mkdir(parents=True)
    for name in files:
        (folder / name).write_text("x")
    return folder


def test_the_scheduled_task_counts_as_starting_at_login(tmp_path, monkeypatch):
    _startup_folder(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "_task_state", lambda name: "ready" if name == "Waggle Gateway" else None)
    status, detail, fix = doctor.check_startup()
    assert status == "ok" and "scheduled task" in detail and fix == ""
    assert "by hand" in detail, "the voice window being off is said, not warned about"


def test_the_startup_folder_still_counts_and_a_disabled_task_is_said(tmp_path, monkeypatch):
    _startup_folder(tmp_path, monkeypatch, "Waggle Gateway.vbs", "Waggle Voice.lnk")
    monkeypatch.setattr(doctor, "_task_state", lambda name: None)
    status, detail, _ = doctor.check_startup()
    assert status == "ok" and "Startup folder" in detail and "starts too" in detail

    monkeypatch.setattr(doctor, "_task_state", lambda name: "disabled")
    (tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Waggle Gateway.vbs").unlink()
    status, detail, fix = doctor.check_startup()
    assert status == "warn" and "switched off" in detail and "Enable-ScheduledTask" in fix


def test_nothing_set_up_points_at_the_installer_that_exists(tmp_path, monkeypatch):
    _startup_folder(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "_task_state", lambda name: None)
    status, detail, fix = doctor.check_startup()
    assert status == "warn" and "install-gateway-task.ps1" in fix and "install-startup" not in fix
    from pathlib import Path
    assert (Path(doctor.__file__).resolve().parents[1] / "tools" / "install-gateway-task.ps1").exists()


def test_a_task_is_read_from_its_xml_in_any_language(monkeypatch):
    """`schtasks /XML`: tags are not translated. Ready unless <Settings> says Enabled false; a trigger's own
    <Enabled> does not count; a missing task is None; the child gets no window."""
    seen = {}

    def fake(xml, code=0):
        def run(cmd, **kw):
            seen["cmd"], seen["flags"] = cmd, kw.get("creationflags")
            return types.SimpleNamespace(returncode=code, stdout=xml)
        return run

    monkeypatch.setattr(doctor.os, "name", "nt")
    ready = "<Task><Triggers><LogonTrigger><Enabled>false</Enabled></LogonTrigger></Triggers><Settings><Hidden>true</Hidden></Settings></Task>"
    monkeypatch.setattr(subprocess, "run", fake(ready.encode("utf-8")))
    assert doctor._task_state("Waggle Gateway") == "ready"
    assert seen["cmd"][:4] == ["schtasks", "/Query", "/TN", "Waggle Gateway"] and "/XML" in seen["cmd"]
    assert seen["flags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    off = "<Task><Settings>\n  <Enabled>false</Enabled>\n</Settings></Task>"
    monkeypatch.setattr(subprocess, "run", fake(b"\xff\xfe" + off.encode("utf-16-le")))
    assert doctor._task_state("Waggle Gateway") == "disabled"
    monkeypatch.setattr(subprocess, "run", fake(b"", code=1))
    assert doctor._task_state("No Such Task") is None
