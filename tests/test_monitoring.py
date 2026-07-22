import os
import subprocess
import time
from types import SimpleNamespace

import monitoring


def test_severity_thresholds():
    assert monitoring._severity(10) == "ok"
    assert monitoring._severity(59.9) == "ok"
    assert monitoring._severity(60) == "warn"
    assert monitoring._severity(84.9) == "warn"
    assert monitoring._severity(85) == "crit"
    assert monitoring._severity(100) == "crit"


def test_get_resource_snapshot_shape():
    snap = monitoring.get_resource_snapshot()
    assert 0 <= snap["cpu_percent"] <= 100
    assert snap["cpu_severity"] in ("ok", "warn", "crit")
    assert isinstance(snap["cpu_per_core"], list) and len(snap["cpu_per_core"]) >= 1
    assert isinstance(snap["disks"], list)
    assert isinstance(snap["gpus"], list)


def test_disk_io_rate_needs_two_samples():
    monitoring._io_cache["time"] = None
    assert monitoring._get_disk_io_rate() is None
    time.sleep(0.05)
    second = monitoring._get_disk_io_rate()
    assert second is not None
    assert "read_mb_s" in second and "write_mb_s" in second


def test_vm_snapshot_is_noop_off_windows():
    if os.name != "nt":
        assert monitoring.get_vm_snapshot() == []


def test_vm_snapshot_parses_powershell_output(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    fake_json = '[{"Name":"web01","State":"Running","Uptime":{"TotalSeconds":3661}}]'
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_json, stderr=""))
    vms = monitoring.get_vm_snapshot()
    assert vms == [{"name": "web01", "state": "Running", "uptime": "1h 1m"}]


def test_vm_snapshot_handles_single_vm_as_dict(monkeypatch):
    """ConvertTo-Json returns a bare object (not a list) when there's exactly one VM -
    a well-known PowerShell gotcha that must not be mistaken for a parse failure."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    fake_json = '{"Name":"web01","State":"Off","Uptime":{"TotalSeconds":0}}'
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_json, stderr=""))
    vms = monitoring.get_vm_snapshot()
    assert len(vms) == 1 and vms[0]["name"] == "web01"


def test_vm_snapshot_logs_stderr_on_failure(monkeypatch, capsys):
    """Regression guard: a failing query must not be silently indistinguishable from
    'no VMs' - the actual PowerShell error (e.g. an elevation/permissions failure)
    must reach the console so it's diagnosable."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(
                             returncode=1, stdout="", stderr="Get-VM : requires elevation"))
    vms = monitoring.get_vm_snapshot()
    assert vms == []
    assert "requires elevation" in capsys.readouterr().out


def test_vm_snapshot_falls_back_to_pwsh(monkeypatch):
    """If 'powershell' isn't on PATH, 'pwsh' (PowerShell 7+) should be tried next."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "powershell":
            raise FileNotFoundError()
        return SimpleNamespace(returncode=0, stdout='[{"Name":"vm1","State":"Running","Uptime":{}}]', stderr="")

    monkeypatch.setattr(monitoring.subprocess, "run", fake_run)
    vms = monitoring.get_vm_snapshot()
    assert calls == ["powershell", "pwsh"]
    assert vms[0]["name"] == "vm1"


def test_vm_snapshot_returns_empty_when_no_shell_found(monkeypatch, capsys):
    monkeypatch.setattr(monitoring.os, "name", "nt")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(monitoring.subprocess, "run", fake_run)
    assert monitoring.get_vm_snapshot() == []
    assert "neither" in capsys.readouterr().out
