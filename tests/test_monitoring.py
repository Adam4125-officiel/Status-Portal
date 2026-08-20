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


def test_evaluate_low_disk_no_threshold_never_triggers():
    snapshot = {"disks": [{"path": "/", "percent": 99}]}
    assert monitoring.evaluate_low_disk(snapshot, None) == []
    assert monitoring.evaluate_low_disk(snapshot, 0) == []


def test_evaluate_low_disk_returns_disks_at_or_above_threshold():
    snapshot = {"disks": [
        {"path": "/", "percent": 92},
        {"path": "/data", "percent": 50},
    ]}
    result = monitoring.evaluate_low_disk(snapshot, 90)
    assert [d["path"] for d in result] == ["/"]

    assert monitoring.evaluate_low_disk(snapshot, 95) == []


def test_control_vm_rejects_unknown_name(monkeypatch):
    """The allow-list check: a name that isn't a currently-live VM must be refused
    before any PowerShell command is ever built, regardless of platform."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring, "get_vm_snapshot", lambda: [{"name": "web01", "state": "Running", "uptime": "1h"}])
    calls = []
    monkeypatch.setattr(monitoring.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace(
        returncode=0, stdout="", stderr=""))

    success, message = monitoring.control_vm("not-a-real-vm", "stop")

    assert success is False
    assert "not-a-real-vm" in message
    assert calls == []  # never even attempted the PowerShell call


def test_control_vm_rejects_unknown_action(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring, "get_vm_snapshot", lambda: [{"name": "web01", "state": "Running", "uptime": "1h"}])
    success, message = monitoring.control_vm("web01", "delete-everything")
    assert success is False
    assert "Unknown" in message


def test_control_vm_not_available_on_non_windows(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "posix")
    success, message = monitoring.control_vm("web01", "start")
    assert success is False
    assert "Windows" in message


def test_control_vm_sends_correct_powershell_command_for_a_known_vm(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring, "get_vm_snapshot", lambda: [{"name": "web01", "state": "Off", "uptime": "0m"}])
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(monitoring.subprocess, "run", fake_run)
    success, message = monitoring.control_vm("web01", "start")

    assert success is True
    assert "web01" in message
    assert "Start-VM" in captured["cmd"][-1]
    assert "web01" in captured["cmd"][-1]


def test_control_vm_escapes_single_quotes_in_name(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "nt")
    monkeypatch.setattr(monitoring, "get_vm_snapshot",
                         lambda: [{"name": "O'Brien-VM", "state": "Off", "uptime": "0m"}])
    captured = {}
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda cmd, **k: captured.update(cmd=cmd) or SimpleNamespace(
                             returncode=0, stdout="ok", stderr=""))

    success, _ = monitoring.control_vm("O'Brien-VM", "stop")

    assert success is True
    assert "O''Brien-VM" in captured["cmd"][-1]  # PowerShell single-quote escaping


def test_control_host_uses_fixed_argument_list(monkeypatch):
    """No string interpolation anywhere in the command - always a fixed list of
    literal arguments, regardless of platform."""
    monkeypatch.setattr(monitoring.os, "name", "nt")
    captured = {}
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda cmd, **k: captured.update(cmd=cmd) or SimpleNamespace(
                             returncode=0, stdout="", stderr=""))

    success, message = monitoring.control_host("restart")

    assert success is True
    assert captured["cmd"] == ["shutdown", "/r", "/t", "5"]
    assert isinstance(captured["cmd"], list)


def test_control_host_posix_command(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "posix")
    captured = {}
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda cmd, **k: captured.update(cmd=cmd) or SimpleNamespace(
                             returncode=0, stdout="", stderr=""))

    success, message = monitoring.control_host("shutdown")

    assert success is True
    assert captured["cmd"] == ["shutdown", "-h", "now"]


def test_control_host_rejects_unknown_action():
    success, message = monitoring.control_host("format-drive")
    assert success is False
    assert "Unknown" in message


def test_control_host_treats_nonzero_returncode_as_failure(monkeypatch, caplog):
    """Regression test: the OS command can run without raising a Python exception
    and still refuse (e.g. insufficient privileges reported via exit code/stderr,
    not an exception) - this must not be silently reported as success."""
    monkeypatch.setattr(monitoring.os, "name", "posix")
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda cmd, **k: SimpleNamespace(
                             returncode=1, stdout="", stderr="shutdown: Need to be root"))

    with caplog.at_level("ERROR"):
        success, message = monitoring.control_host("shutdown")

    assert success is False
    assert "Need to be root" in message
    assert "Need to be root" in caplog.text


def test_control_host_logs_info_on_success(monkeypatch, caplog):
    monkeypatch.setattr(monitoring.os, "name", "posix")
    monkeypatch.setattr(monitoring.subprocess, "run",
                         lambda cmd, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))

    with caplog.at_level("INFO"):
        success, message = monitoring.control_host("restart")

    assert success is True
    assert "accepted" in caplog.text


def test_control_host_handles_subprocess_failure(monkeypatch):
    monkeypatch.setattr(monitoring.os, "name", "posix")

    def raise_error(cmd, **kwargs):
        raise PermissionError("not permitted")

    monkeypatch.setattr(monitoring.subprocess, "run", raise_error)
    success, message = monitoring.control_host("restart")
    assert success is False
    assert "not permitted" in message


# ---------------------------------------------------------------------------
# CPU sampling off the request path
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_cpu_cache():
    """_CPU_CACHE is a module-level global; reset it so these tests don't inherit a
    reading left behind by whatever ran before them."""
    saved = dict(monitoring._CPU_CACHE)
    monitoring._CPU_CACHE.update({"per_core": [], "updated_at": None,
                                  "sampled_at": None, "max_age": None})
    yield
    monitoring._CPU_CACHE.update(saved)


def test_snapshot_reads_the_cpu_cache_without_blocking(clean_cpu_cache, monkeypatch):
    """The whole point of the change: with a fresh cached reading, get_resource_snapshot()
    must not call the *blocking* form of psutil.cpu_percent at all."""
    monitoring._CPU_CACHE.update({"per_core": [10.0, 30.0], "updated_at": time.time(),
                                  "max_age": 60})

    def _explode(*args, **kwargs):
        raise AssertionError("psutil.cpu_percent must not be called when the cache is fresh")

    monkeypatch.setattr(monitoring.psutil, "cpu_percent", _explode)
    snapshot = monitoring.get_resource_snapshot()
    assert snapshot["cpu_percent"] == 20.0
    assert [c["percent"] for c in snapshot["cpu_per_core"]] == [10.0, 30.0]


def test_falls_back_to_a_live_sample_when_no_reading_has_been_published(clean_cpu_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(monitoring.psutil, "cpu_percent",
                        lambda interval=None, percpu=False: calls.append(interval) or [50.0])
    assert monitoring._get_cpu_percentages() == [50.0]
    # The blocking form, i.e. a real interval - not psutil's meaningless 0.0 first call.
    assert calls == [monitoring.CPU_FALLBACK_SAMPLE_SECONDS]


def test_falls_back_when_the_cached_reading_has_gone_stale(clean_cpu_cache, monkeypatch):
    """A dead background thread must show a fresh number, not a silently frozen one."""
    monitoring._CPU_CACHE.update({"per_core": [99.0], "updated_at": time.time() - 500,
                                  "max_age": 35})
    monkeypatch.setattr(monitoring.psutil, "cpu_percent",
                        lambda interval=None, percpu=False: [7.0])
    assert monitoring._get_cpu_percentages() == [7.0]


def test_cpu_cache_refresh_ignores_a_too_short_first_window(clean_cpu_cache, monkeypatch):
    """psutil's first cpu_percent(interval=None) call always answers 0.0 regardless of
    load - publishing it would show an idle CPU on a busy machine."""
    monkeypatch.setattr(monitoring.psutil, "cpu_percent",
                        lambda interval=None, percpu=False: [0.0])
    monitoring._refresh_cpu_cache()
    assert monitoring._CPU_CACHE["updated_at"] is None
    assert monitoring._CPU_CACHE["sampled_at"] is not None

    monitoring._CPU_CACHE["sampled_at"] -= monitoring.MIN_CPU_SAMPLE_WINDOW_SECONDS + 1
    monkeypatch.setattr(monitoring.psutil, "cpu_percent",
                        lambda interval=None, percpu=False: [42.0])
    monitoring._refresh_cpu_cache()
    assert monitoring._CPU_CACHE["per_core"] == [42.0]
    assert monitoring._CPU_CACHE["updated_at"] is not None


def test_volume_label_is_looked_up_once_per_device(monkeypatch):
    monitoring._volume_label_cache.clear()
    calls = []
    monkeypatch.setattr(monitoring, "_query_volume_label",
                        lambda mp, dev: calls.append(dev) or "Media")
    assert monitoring._get_volume_label("/mnt/media", "/dev/sdb1") == "Media"
    assert monitoring._get_volume_label("/mnt/media", "/dev/sdb1") == "Media"
    assert calls == ["/dev/sdb1"]

    # "no label" is an answer too, and must not be re-queried on every page load.
    monkeypatch.setattr(monitoring, "_query_volume_label",
                        lambda mp, dev: calls.append(dev) or None)
    assert monitoring._get_volume_label("/mnt/other", "/dev/sdc1") is None
    assert monitoring._get_volume_label("/mnt/other", "/dev/sdc1") is None
    assert calls == ["/dev/sdb1", "/dev/sdc1"]
    monitoring._volume_label_cache.clear()


def test_background_refresh_now_starts_on_every_platform(monkeypatch):
    """It used to return early off Windows. The CPU half of the loop applies
    everywhere, and that's what keeps the blocking sample out of the request path."""
    started = []
    monkeypatch.setattr(monitoring.threading, "Thread",
                        lambda **kwargs: started.append(kwargs) or SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(monitoring.os, "name", "posix")
    monitoring.start_background_refresh(10)
    assert len(started) == 1
    assert monitoring._CPU_CACHE["max_age"] == 35


def test_cache_summary_reports_iso_timestamps(clean_cpu_cache):
    """The caches stamp themselves with time.time() floats; static/js/local_time.js
    parses ISO strings and silently leaves anything else showing its raw fallback
    text, so the conversion has to happen before the template sees it."""
    monitoring._CPU_CACHE.update({"per_core": [5.0], "updated_at": 1_700_000_000.0})
    entry = next(c for c in monitoring.cache_summary() if c["name"].startswith("CPU"))
    assert entry["updated_at"].startswith("2023-11-14T")
    assert entry["entries"] == 1

    monitoring._CPU_CACHE["updated_at"] = None
    entry = next(c for c in monitoring.cache_summary() if c["name"].startswith("CPU"))
    assert entry["updated_at"] is None


def test_clear_caches_resets_every_module_cache(clean_cpu_cache):
    monitoring._WINDOWS_CACHE.update({"vms": [{"name": "vm1"}], "cpu_temp_c": 45,
                                      "disk_details": {"C": {}}, "updated_at": 1.0})
    monitoring._CPU_CACHE.update({"per_core": [3.0], "updated_at": 1.0, "sampled_at": 1.0})
    monitoring._volume_label_cache["/dev/sda1"] = "Media"
    monitoring._perdisk_io_cache["PhysicalDrive0"] = {"time": 1.0, "read_bytes": 1, "write_bytes": 1}
    monitoring._net_cache.update({"time": 1.0, "sent_bytes": 1, "recv_bytes": 1})

    monitoring.clear_caches()

    assert monitoring._WINDOWS_CACHE["vms"] == []
    assert monitoring._WINDOWS_CACHE["cpu_temp_c"] is None
    assert monitoring._CPU_CACHE["updated_at"] is None
    assert monitoring._volume_label_cache == {}
    assert monitoring._perdisk_io_cache == {}
    assert monitoring._net_cache["time"] is None
