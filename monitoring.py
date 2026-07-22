"""
monitoring.py — Live CPU/RAM/disk/GPU/VM snapshot for the resource panel
(admin-only by default, optionally also on the public page). No historical
tracking, just a fresh reading each time the page loads/auto-refreshes.
"""
import json
import os
import subprocess
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

# Read/write byte counters from the previous call, used to compute a rate. Module-level
# by design (one process-wide baseline is enough for a single-instance app); the first
# ever reading has nothing to compare against, so disk_io is None until the second call.
_io_cache = {"time": None, "read_bytes": None, "write_bytes": None}

# Same idea for network throughput (aggregate across all interfaces).
_net_cache = {"time": None, "sent_bytes": None, "recv_bytes": None}


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
        "mem_percent": mem.percent,
        "mem_severity": _severity(mem.percent),
        "mem_used_gb": round(mem.used / (1024 ** 3), 1),
        "mem_total_gb": round(mem.total / (1024 ** 3), 1),
        "disks": _get_disk_snapshots(),
        "disk_io": _get_disk_io_rate(),
        "network": _get_network_rate(),
        "gpus": _get_gpu_snapshot(),
    }


def _get_disk_snapshots():
    """All real, distinct disks/drives - not just one configured path. The same
    physical/logical device is often bind-mounted at several paths (very common under
    Docker: /etc/resolv.conf, /etc/hosts, etc. all point at the same underlying disk as
    the root filesystem) - those are deduplicated down to one entry per device, keeping
    whichever mountpoint has the shortest path as the more meaningful label.
    Running inside Docker only shows what's actually mounted into the container (the
    overlay root fs + any declared volumes), not arbitrary other host disks - that's a
    Docker limitation, not something this function can work around."""
    best_by_device = {}
    for part in psutil.disk_partitions(all=False):
        if part.fstype.lower() in _IGNORED_FSTYPES:
            continue
        device = part.device or part.mountpoint
        existing = best_by_device.get(device)
        if existing is None or len(part.mountpoint) < len(existing.mountpoint):
            best_by_device[device] = part

    disks = []
    for part in best_by_device.values():
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        label = _get_volume_label(part.mountpoint, part.device)
        disks.append({
            "path": part.mountpoint,
            "label": label,
            "display_name": f"{label} ({part.mountpoint})" if label else part.mountpoint,
            "percent": usage.percent,
            "severity": _severity(usage.percent),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
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


def _get_disk_io_rate():
    """Aggregate (system-wide) disk read/write throughput, computed from the delta
    since the last call - not broken out per-disk, since mapping I/O counters to a
    specific drive letter/mountpoint isn't reliable cross-platform (Windows in
    particular reports I/O per physical drive number, which doesn't map cleanly to
    drive letters without extra OS-specific APIs). Returns None on the very first call
    (no baseline yet) or if the platform doesn't expose disk_io_counters at all."""
    counters = psutil.disk_io_counters()
    if counters is None:
        return None
    now = time.time()
    result = None
    if _io_cache["time"] is not None:
        elapsed = now - _io_cache["time"]
        if elapsed > 0:
            read_rate = max(counters.read_bytes - _io_cache["read_bytes"], 0) / elapsed
            write_rate = max(counters.write_bytes - _io_cache["write_bytes"], 0) / elapsed
            result = {
                "read_mb_s": round(read_rate / (1024 ** 2), 2),
                "write_mb_s": round(write_rate / (1024 ** 2), 2),
            }
    _io_cache["time"] = now
    _io_cache["read_bytes"] = counters.read_bytes
    _io_cache["write_bytes"] = counters.write_bytes
    return result


def _get_network_rate():
    """Aggregate upload/download throughput across all network interfaces, computed
    from the delta since the last call - same pattern as _get_disk_io_rate(). Returns
    None on the very first call (no baseline yet)."""
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
            gpus.append({
                "name": name,
                "util_percent": util.gpu,
                "mem_used_gb": round(mem.used / (1024 ** 3), 1),
                "mem_total_gb": round(mem.total / (1024 ** 3), 1),
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


_VM_QUERY_COMMAND = (
    "Import-Module Hyper-V -ErrorAction SilentlyContinue; "
    "Get-VM | Select-Object Name,@{Name='State';Expression={$_.State.ToString()}},Uptime"
    " | ConvertTo-Json -Compress"
)


def get_vm_snapshot():
    """Best-effort list of Hyper-V VMs (Windows only). Returns [] - never raises - if
    this isn't Windows, PowerShell/Hyper-V isn't available, or the query fails for any
    reason, so the feature silently disables itself instead of crashing the page.

    Unlike earlier, a failure is now logged (with PowerShell's actual stderr) instead of
    silently swallowed - "no VMs shown" was indistinguishable from "the query is
    failing" before. The single most likely real cause on a real Hyper-V host: Get-VM
    requires the account running this app to be an Administrator or in the "Hyper-V
    Administrators" group - if so, stderr below will say so explicitly."""
    if os.name != "nt":
        return []
    for shell in ("powershell", "pwsh"):
        try:
            result = subprocess.run(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _VM_QUERY_COMMAND],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            continue  # this shell isn't installed - try the next candidate
        except Exception as e:
            print(f"[monitoring] Hyper-V VM query via {shell} failed to run: {e}")
            return []

        if result.returncode != 0:
            print(f"[monitoring] Hyper-V VM query via {shell} exited {result.returncode}: "
                  f"{result.stderr.strip() or '(no stderr output)'}")
            return []
        if not result.stdout.strip():
            return []  # no VMs defined - not an error
        try:
            data = json.loads(result.stdout)
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
    print("[monitoring] Hyper-V VM query failed: neither 'powershell' nor 'pwsh' found on PATH")
    return []


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
