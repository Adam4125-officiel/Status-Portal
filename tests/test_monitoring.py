import os
import time

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
