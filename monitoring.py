"""
monitoring.py — Live CPU/RAM/disk/GPU/VM snapshot for the resource panel
(admin-only by default, optionally also on the public page). No historical
tracking, just a fresh reading each time the page loads/auto-refreshes.
"""
import json
import os
import subprocess
import threading
import time

import psutil

# Pseudo/virtual filesystems to skip when listing disks - psutil.disk_partitions(all=False)
# already filters most of these, but this is extra insurance across platforms/setups.
_IGNORED_FSTYPES = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "devfs", "overlay", "squashfs",
    "autofs", "cgroup", "cgroup2", "debugfs", "tracefs", "mqueue",
    "hugetlbfs", "pstore", "bpf", "binfmt_misc", "securityfs", "configfs",
    "fusectl", "rpc_pipefs", "nsfs",
}

# Per-disk read/write byte counters from the previous call, keyed by the same device
# key used to look them up in psutil.disk_io_counters(perdisk=True) (e.g.
# "PhysicalDrive0" on Windows) - one entry per disk instead of one global baseline,
# since each disk's rate needs its own delta. A disk's entry stays None until its
# second reading has something to compare against.
_perdisk_io_cache = {}

# Same idea for network throughput (aggregate across all interfaces).
_net_cache = {"time": None, "sent_bytes": None, "recv_bytes": None}

# Best-effort Windows-only data that requires a PowerShell/CIM subprocess call (CPU
# temperature, per-disk temperature/drive-letter mapping, Hyper-V VMs) - populated by
# a background thread (see start_background_refresh()) instead of queried live inside
# a request handler, per the project's standing rule against slow I/O in the request
# path. Empty/None by default, which is exactly correct on non-Windows (nothing ever
# populates it there, and every reader already treats "no data" as "feature not
# available here" the same way GPU detection does when there's no NVIDIA card.
_WINDOWS_CACHE = {"vms": [], "cpu_temp_c": None, "disk_details": {}, "updated_at": None}


def _severity(percent):
    if percent >= 85:
        return "crit"
    if percent >= 60:
        return "warn"
    return "ok"


def get_resource_snapshot():
    per_core = psutil.cpu_percent(interval=0.2, percpu=True)
    cpu_percent = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": cpu_percent,
        "cpu_severity": _severity(cpu_percent),
        "cpu_per_core": [{"percent": c, "severity": _severity(c)} for c in per_core],
        "cpu_temp_c": _WINDOWS_CACHE["cpu_temp_c"],  # cache-only - see _WINDOWS_CACHE docstring
        "mem_percent": mem.percent,
        "mem_severity": _severity(mem.percent),
        "mem_used_gb": round(mem.used / (1024 ** 3), 1),
        "mem_total_gb": round(mem.total / (1024 ** 3), 1),
        "disks": _get_disk_snapshots(),
        "network": _get_network_rate(),
        "gpus": _get_gpu_snapshot(),
    }


def evaluate_high_load(snapshot, thresholds):
    """Pure function, no DB/network access - takes a snapshot (from
    get_resource_snapshot()) and admin-configured thresholds
    ({"cpu_percent", "disk_io_mbs", "network_mbs"}, any of which may be missing/None
    to disable that check) and returns {"active": bool, "reasons": [str, ...]}.
    Deliberately DB-free so both app.py and discord_bot.py can call it after
    fetching thresholds themselves, without this module needing to import db.py.

    Disk I/O is summed only across disks that actually reported a rate (per-disk
    I/O is Windows-only and best-effort - see _get_disk_snapshots()); a disk with no
    I/O reading contributes nothing to the total rather than being treated as 0."""
    reasons = []

    cpu_percent = snapshot.get("cpu_percent") or 0
    cpu_threshold = thresholds.get("cpu_percent")
    if cpu_threshold and cpu_percent >= cpu_threshold:
        reasons.append(f"CPU {cpu_percent}%")

    disk_io_mb_s = sum(
        d["io"]["read_mb_s"] + d["io"]["write_mb_s"] for d in snapshot.get("disks", []) if d.get("io")
    )
    disk_io_threshold = thresholds.get("disk_io_mbs")
    if disk_io_threshold and disk_io_mb_s >= disk_io_threshold:
        reasons.append(f"Disk I/O {round(disk_io_mb_s, 1)} MB/s")

    network = snapshot.get("network")
    network_mb_s = (network["up_mb_s"] + network["down_mb_s"]) if network else 0
    network_threshold = thresholds.get("network_mbs")
    if network_threshold and network_mb_s >= network_threshold:
        reasons.append(f"Network {round(network_mb_s, 1)} MB/s")

    return {"active": bool(reasons), "reasons": reasons}


def _get_disk_snapshots():
    """All real, distinct disks/drives - not just one configured path. The same
    physical/logical device is often bind-mounted at several paths (very common under
    Docker: /etc/resolv.conf, /etc/hosts, etc. all point at the same underlying disk as
    the root filesystem) - those are deduplicated down to one entry per device, keeping
    whichever mountpoint has the shortest path as the more meaningful label.
    Running inside Docker only shows what's actually mounted into the container (the
    overlay root fs + any declared volumes), not arbitrary other host disks - that's a
    Docker limitation, not something this function can work around.

    Temperature and per-disk I/O are Windows-only, best-effort, and cache-only (see
    _WINDOWS_CACHE) - both come from None/absent on any other platform, or if the
    disk couldn't be correlated to a physical disk (e.g. a network drive)."""
    best_by_device = {}
    for part in psutil.disk_partitions(all=False):
        if part.fstype.lower() in _IGNORED_FSTYPES:
            continue
        device = part.device or part.mountpoint
        existing = best_by_device.get(device)
        if existing is None or len(part.mountpoint) < len(existing.mountpoint):
            best_by_device[device] = part

    by_drive_letter = _WINDOWS_CACHE["disk_details"]
    perdisk_counters = psutil.disk_io_counters(perdisk=True) or {} if os.name == "nt" else {}

    disks = []
    for part in best_by_device.values():
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        label = _get_volume_label(part.mountpoint, part.device)
        temp_c = None
        io = None
        if os.name == "nt" and part.mountpoint:
            detail = by_drive_letter.get(part.mountpoint[0].upper())
            if detail:
                temp_c = detail["temp_c"]
                counters = perdisk_counters.get(f"PhysicalDrive{detail['disk_number']}")
                if counters is not None:
                    io = _get_perdisk_io_rate(f"PhysicalDrive{detail['disk_number']}", counters)
        disks.append({
            "path": part.mountpoint,
            "label": label,
            "display_name": f"{label} ({part.mountpoint})" if label else part.mountpoint,
            "percent": usage.percent,
            "severity": _severity(usage.percent),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "temp_c": temp_c,
            "io": io,
        })
    return disks


def _get_volume_label(mountpoint, device):
    """Best-effort human-readable volume/partition label (e.g. "Media" instead of a
    bare drive letter or mountpoint). Returns None if unavailable - unlabeled
    partitions, or a lookup method that isn't supported here - callers fall back to
    showing the raw path instead."""
    try:
        if os.name == "nt":
            import ctypes
            buf = ctypes.create_unicode_buffer(261)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(mountpoint), buf, ctypes.sizeof(buf),
                None, None, None, None, 0)
            return buf.value if ok and buf.value else None
        by_label_dir = "/dev/disk/by-label"
        if os.path.isdir(by_label_dir):
            real_device = os.path.realpath(device)
            for entry in os.listdir(by_label_dir):
                if os.path.realpath(os.path.join(by_label_dir, entry)) == real_device:
                    return entry
    except Exception:
        pass
    return None


def _get_perdisk_io_rate(key, counters):
    """Read/write throughput for one disk, computed from the delta since the last
    call for that same key - same pattern as the old system-wide version, just with
    one cache entry per disk instead of a single global one. Returns None on that
    disk's first-ever reading (no baseline yet)."""
    now = time.time()
    cache = _perdisk_io_cache.setdefault(key, {"time": None, "read_bytes": None, "write_bytes": None})
    result = None
    if cache["time"] is not None:
        elapsed = now - cache["time"]
        if elapsed > 0:
            read_rate = max(counters.read_bytes - cache["read_bytes"], 0) / elapsed
            write_rate = max(counters.write_bytes - cache["write_bytes"], 0) / elapsed
            result = {
                "read_mb_s": round(read_rate / (1024 ** 2), 2),
                "write_mb_s": round(write_rate / (1024 ** 2), 2),
            }
    cache["time"] = now
    cache["read_bytes"] = counters.read_bytes
    cache["write_bytes"] = counters.write_bytes
    return result


def _get_network_rate():
    """Aggregate upload/download throughput across all network interfaces, computed
    from the delta since the last call - same delta-cache pattern as
    _get_perdisk_io_rate(). Returns None on the very first call (no baseline yet)."""
    counters = psutil.net_io_counters()
    if counters is None:
        return None
    now = time.time()
    result = None
    if _net_cache["time"] is not None:
        elapsed = now - _net_cache["time"]
        if elapsed > 0:
            up_rate = max(counters.bytes_sent - _net_cache["sent_bytes"], 0) / elapsed
            down_rate = max(counters.bytes_recv - _net_cache["recv_bytes"], 0) / elapsed
            result = {
                "up_mb_s": round(up_rate / (1024 ** 2), 2),
                "down_mb_s": round(down_rate / (1024 ** 2), 2),
            }
    _net_cache["time"] = now
    _net_cache["sent_bytes"] = counters.bytes_sent
    _net_cache["recv_bytes"] = counters.bytes_recv
    return result


def _get_gpu_snapshot():
    """Best-effort NVIDIA GPU stats via the optional nvidia-ml-py package.
    Returns [] (never raises) if it's not installed or there's no NVIDIA GPU -
    this section simply doesn't render rather than breaking the page."""
    try:
        import pynvml
        pynvml.nvmlInit()
        gpus = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            try:
                temp_c = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp_c = None  # a specific card/driver not exposing this shouldn't drop the rest
            gpus.append({
                "name": name,
                "util_percent": util.gpu,
                "mem_used_gb": round(mem.used / (1024 ** 3), 1),
                "mem_total_gb": round(mem.total / (1024 ** 3), 1),
                "temp_c": temp_c,
            })
        pynvml.nvmlShutdown()
        return gpus
    except Exception:
        return []


# Hyper-V's VMState enum, as a fallback in case ConvertTo-Json ever serializes it as
# its underlying integer instead of a name (ToString() in the PowerShell command below
# should already prevent this - this is a second line of defense, not the primary fix).
_VM_STATE_NAMES = {
    "0": "Unknown", "1": "Other", "2": "Running", "3": "Off", "4": "Stopping",
    "6": "Saved", "7": "Paused", "9": "Starting", "10": "Snapshotting",
    "11": "Saving", "13": "Pausing", "14": "Resuming", "17": "FastSaved", "18": "FastSaving",
}


def _normalize_vm_state(state):
    return _VM_STATE_NAMES.get(str(state), str(state))


def _run_powershell(command, timeout=10):
    """Runs a PowerShell command, trying 'powershell' then falling back to 'pwsh'
    (PowerShell 7+) if the former isn't on PATH - shared by every Windows-only,
    PowerShell/CIM-backed query in this module (Hyper-V VMs, CPU temperature,
    per-disk details). Returns the completed stdout string, or None (after logging
    why) on any failure - trying pwsh after powershell isn't found isn't itself
    logged as an error, since that's an expected fallback, not a failure."""
    for shell in ("powershell", "pwsh"):
        try:
            result = subprocess.run(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            continue  # this shell isn't installed - try the next candidate
        except Exception as e:
            print(f"[monitoring] PowerShell query via {shell} failed to run: {e}")
            return None
        if result.returncode != 0:
            print(f"[monitoring] PowerShell query via {shell} exited {result.returncode}: "
                  f"{result.stderr.strip() or '(no stderr output)'}")
            return None
        return result.stdout
    print("[monitoring] PowerShell query failed: neither 'powershell' nor 'pwsh' found on PATH")
    return None


_VM_QUERY_COMMAND = (
    "Import-Module Hyper-V -ErrorAction SilentlyContinue; "
    "Get-VM | Select-Object Name,@{Name='State';Expression={$_.State.ToString()}},Uptime"
    " | ConvertTo-Json -Compress"
)


def get_vm_snapshot():
    """Best-effort list of Hyper-V VMs (Windows only). Returns [] - never raises - if
    this isn't Windows, PowerShell/Hyper-V isn't available, or the query fails for any
    reason, so the feature silently disables itself instead of crashing the page.
    The single most likely real cause on a real Hyper-V host: Get-VM requires the
    account running this app to be an Administrator or in the "Hyper-V
    Administrators" group - _run_powershell() logs the actual stderr if so.

    Always queries live - this is unit-tested directly by mocking subprocess.run.
    Request handlers should use get_cached_vm_snapshot() instead, which reads the
    background-refreshed cache rather than shelling out on every page load."""
    if os.name != "nt":
        return []
    stdout = _run_powershell(_VM_QUERY_COMMAND)
    if not stdout or not stdout.strip():
        return []  # either the query failed (already logged) or no VMs are defined
    try:
        data = json.loads(stdout)
    except ValueError as e:
        print(f"[monitoring] Hyper-V VM query returned unparseable output: {e}")
        return []
    if isinstance(data, dict):
        data = [data]
    return [
        {
            "name": vm.get("Name", "Unknown"),
            "state": _normalize_vm_state(vm.get("State", "Unknown")),
            "uptime": _format_uptime(vm.get("Uptime") or {}),
        }
        for vm in data
    ]


_CPU_TEMP_QUERY_COMMAND = (
    "$t = (Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
    "-ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty CurrentTemperature); "
    "if ($null -eq $t) { '' } else { $t }"
)


def _query_cpu_temp():
    """Best-effort CPU temperature in Celsius (Windows only) via the ACPI thermal
    zone WMI namespace - the only temperature source that doesn't require a
    third-party tool (OpenHardwareMonitor/LibreHardwareMonitor etc.) to be installed
    and running. Well-known-unreliable: many systems block this without admin
    rights, or the ACPI thermal zone reports chassis/motherboard temp rather than
    the CPU package - there's no universal free Windows API for the latter. Returns
    None on any failure or non-Windows, same as every other best-effort sensor here."""
    if os.name != "nt":
        return None
    stdout = _run_powershell(_CPU_TEMP_QUERY_COMMAND)
    if not stdout or not stdout.strip():
        return None
    try:
        tenths_kelvin = float(stdout.strip())
    except ValueError:
        return None
    return round(tenths_kelvin / 10 - 273.15, 1)


_DISK_DETAILS_QUERY_COMMAND = (
    "Get-PhysicalDisk | ForEach-Object { "
    "$pd = $_; "
    "$rel = $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
    "$parts = Get-Partition -DiskNumber $pd.DeviceId -ErrorAction SilentlyContinue | Where-Object DriveLetter; "
    "[PSCustomObject]@{ DiskNumber = $pd.DeviceId; TemperatureC = $rel.Temperature; "
    "DriveLetters = @($parts.DriveLetter) } "
    "} | ConvertTo-Json -Compress -Depth 3"
)


def _query_windows_disk_details():
    """Best-effort per-physical-disk temperature, keyed by drive letter (Windows
    only) - used to enrich the per-disk resource cards with temp and to correlate a
    disk's mountpoint to the PhysicalDriveN key psutil.disk_io_counters(perdisk=True)
    uses on Windows, via the disk number Get-Partition reports for that drive letter.

    Get-StorageReliabilityCounter's Temperature is well-known-unreliable - many
    consumer SATA/NVMe drives return null through it even though the drive itself
    reports a SMART temperature; that's a Windows storage-stack limitation, not
    something fixable here, so a disk with no temperature just shows none rather
    than a guess. Returns {drive_letter: {"disk_number": int, "temp_c": float|None}}."""
    if os.name != "nt":
        return {}
    stdout = _run_powershell(_DISK_DETAILS_QUERY_COMMAND, timeout=15)
    if not stdout or not stdout.strip():
        return {}
    try:
        data = json.loads(stdout)
    except ValueError as e:
        print(f"[monitoring] Windows disk-detail query returned unparseable output: {e}")
        return {}
    if isinstance(data, dict):
        data = [data]
    by_drive_letter = {}
    for entry in data:
        disk_number = entry.get("DiskNumber")
        if disk_number is None:
            continue
        temp = entry.get("TemperatureC")
        temp_c = float(temp) if isinstance(temp, (int, float)) else None
        letters = entry.get("DriveLetters") or []
        if isinstance(letters, str):
            letters = [letters]
        for letter in letters:
            if letter:
                by_drive_letter[str(letter).upper()] = {"disk_number": disk_number, "temp_c": temp_c}
    return by_drive_letter


def _refresh_windows_cache():
    _WINDOWS_CACHE["vms"] = get_vm_snapshot()
    _WINDOWS_CACHE["cpu_temp_c"] = _query_cpu_temp()
    _WINDOWS_CACHE["disk_details"] = _query_windows_disk_details()
    _WINDOWS_CACHE["updated_at"] = time.time()


def _background_refresh_loop(interval_seconds):
    while True:
        try:
            _refresh_windows_cache()
        except Exception as e:
            print(f"[monitoring] background refresh error: {e}")
        time.sleep(interval_seconds)


def start_background_refresh(interval_seconds=10):
    """Starts the Windows-only PowerShell/CIM-backed refresh loop (VM list, CPU
    temp, per-disk details) in a background thread, mirroring
    start_background_checker() in app.py and discord_bot.start() - these three
    queries are the only slow (subprocess) I/O in this module, so per the project's
    standing rule against slow I/O in a request handler, they're polled here instead
    of queried live from index()/admin_resources(). No-op on non-Windows, since
    nothing in this module shells out there. Called once from app.py at startup."""
    if os.name != "nt":
        return
    threading.Thread(target=_background_refresh_loop, args=(interval_seconds,),
                      daemon=True, name="monitoring-windows-refresh").start()


def get_cached_vm_snapshot():
    """Request-handler-safe VM list - reads the background-refreshed cache instead
    of shelling out live. get_vm_snapshot() itself stays a live, directly-callable
    query (that's what's unit-tested by mocking subprocess.run) - this is the cached
    wrapper around it for the hot request path."""
    return _WINDOWS_CACHE["vms"] if os.name == "nt" else []


def _format_uptime(uptime_obj):
    try:
        total_seconds = uptime_obj.get("TotalSeconds", 0)
        days = int(total_seconds // 86400)
        hours = int((total_seconds % 86400) // 3600)
        minutes = int((total_seconds % 3600) // 60)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "—"
