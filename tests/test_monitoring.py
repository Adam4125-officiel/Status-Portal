import os
import subprocess
import time
from types import SimpleNamespace

import pytest

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


def test_perdisk_io_rate_needs_two_samples():
    monitoring._perdisk_io_cache.pop("PhysicalDrive0", None)
    counters = SimpleNamespace(read_bytes=1000, write_bytes=500)
    assert monitoring._get_perdisk_io_rate("PhysicalDrive0", counters) is None
    time.sleep(0.05)
    counters2 = SimpleNamespace(read_bytes=2000, write_bytes=1500)
    second = monitoring._get_perdisk_io_rate("PhysicalDrive0", counters2)
    assert second is not None
    assert "read_mb_s" in second and "write_mb_s" in second

    # A different key has its own independent baseline.
    monitoring._perdisk_io_cache.pop("PhysicalDrive1", None)
    assert monitoring._get_perdisk_io_rate("PhysicalDrive1", counters) is None


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


def test_vm_snapshot_logs_stderr_on_failure(monkeypatch, caplog):
    """Regression guard: a failing query must not be silently indistinguishable from
    'no VMs' - the actual PowerShell error (e.g. an elevation/permissions failure)
    must reach the log so it's diagnosable."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(
                             returncode=1, stdout="", stderr="Get-VM : requires elevation"))
    with caplog.at_level("ERROR"):
        vms = monitoring.get_vm_snapshot()
    assert vms == []
    assert "requires elevation" in caplog.text


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


def test_vm_snapshot_returns_empty_when_no_shell_found(monkeypatch, caplog):
    monkeypatch.setattr(monitoring.os, "name", "nt")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(monitoring.subprocess, "run", fake_run)
    with caplog.at_level("ERROR"):
        assert monitoring.get_vm_snapshot() == []
    assert "neither" in caplog.text


def test_query_cpu_temp_converts_tenths_kelvin_to_celsius(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout="2931", stderr=""))
    # 293.1 K -> 19.95 C, rounded to 1 decimal
    assert monitoring._query_cpu_temp() == pytest.approx(20.0, abs=0.01)


def test_query_cpu_temp_returns_none_when_sensor_unavailable(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert monitoring._query_cpu_temp() is None


def test_query_cpu_temp_is_noop_off_windows():
    if os.name != "nt":
        assert monitoring._query_cpu_temp() is None


def test_query_windows_disk_details_parses_output(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    fake_json = ('[{"DiskNumber":0,"TemperatureC":42.5,"DriveLetters":["C"]},'
                 '{"DiskNumber":1,"TemperatureC":null,"DriveLetters":["D","E"]}]')
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_json, stderr=""))
    details = monitoring._query_windows_disk_details()
    assert details["C"] == {"disk_number": 0, "temp_c": 42.5}
    assert details["D"] == {"disk_number": 1, "temp_c": None}
    assert details["E"] == {"disk_number": 1, "temp_c": None}


def test_query_windows_disk_details_treats_zero_temperature_as_no_reading(monkeypatch):
    """Get-StorageReliabilityCounter returns a literal 0 (not null) for some drives
    it can't properly read - observed on a drive whose SMART temperature is only
    exposed as attribute 190 "Airflow Temperature" rather than attribute 194
    "Temperature"/"Drive Temperature". 0C is never a real reading, so it should
    display the same as no reading at all, not as if the drive were freezing."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    fake_json = '{"DiskNumber":2,"TemperatureC":0,"DriveLetters":["F"]}'
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_json, stderr=""))
    details = monitoring._query_windows_disk_details()
    assert details["F"] == {"disk_number": 2, "temp_c": None}


def test_query_windows_disk_details_handles_single_disk_as_dict(monkeypatch):
    """Same PowerShell/ConvertTo-Json gotcha as the Hyper-V VM query: exactly one
    result comes back as a bare object, not a one-item array."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    fake_json = '{"DiskNumber":0,"TemperatureC":30,"DriveLetters":"C"}'
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_json, stderr=""))
    details = monitoring._query_windows_disk_details()
    assert details == {"C": {"disk_number": 0, "temp_c": 30.0}}


def test_query_windows_disk_details_empty_off_windows():
    if os.name != "nt":
        assert monitoring._query_windows_disk_details() == {}


def test_get_cached_vm_snapshot_reads_windows_cache(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setitem(monitoring._WINDOWS_CACHE, "vms", [{"name": "web01", "state": "Running", "uptime": "1h"}])
    assert monitoring.get_cached_vm_snapshot() == [{"name": "web01", "state": "Running", "uptime": "1h"}]


def test_get_cached_vm_snapshot_empty_off_windows():
    if os.name != "nt":
        assert monitoring.get_cached_vm_snapshot() == []


def test_evaluate_high_load_no_thresholds_never_triggers():
    snapshot = {"cpu_percent": 99, "disks": [], "network": {"up_mb_s": 500, "down_mb_s": 500}}
    result = monitoring.evaluate_high_load(snapshot, {})
    assert result == {"active": False, "reasons": []}


def test_evaluate_high_load_cpu_threshold():
    snapshot = {"cpu_percent": 95, "disks": [], "network": None}
    result = monitoring.evaluate_high_load(snapshot, {"cpu_percent": 90})
    assert result["active"] is True
    assert "CPU 95%" in result["reasons"][0]

    result = monitoring.evaluate_high_load({"cpu_percent": 50, "disks": [], "network": None},
                                            {"cpu_percent": 90})
    assert result == {"active": False, "reasons": []}


def test_evaluate_high_load_disk_io_sums_only_disks_with_a_reading():
    snapshot = {
        "cpu_percent": 10,
        "disks": [
            {"io": {"read_mb_s": 80, "write_mb_s": 40}},
            {"io": None},  # not correlated to a physical disk - contributes nothing
        ],
        "network": None,
    }
    result = monitoring.evaluate_high_load(snapshot, {"disk_io_mbs": 100})
    assert result["active"] is True
    assert "Disk I/O 120 MB/s" in result["reasons"]


def test_evaluate_high_load_network_threshold():
    snapshot = {"cpu_percent": 10, "disks": [], "network": {"up_mb_s": 60, "down_mb_s": 60}}
    result = monitoring.evaluate_high_load(snapshot, {"network_mbs": 100})
    assert result["active"] is True
    assert "Network 120 MB/s" in result["reasons"]
