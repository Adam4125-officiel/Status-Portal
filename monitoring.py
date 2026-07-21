"""
monitoring.py — Live CPU/RAM/disk/GPU snapshot for the resource panel
(admin-only by default, optionally also on the public page). No historical
tracking, just a fresh reading each time the page loads/auto-refreshes.
"""
import psutil

# Pseudo/virtual filesystems to skip when listing disks - psutil.disk_partitions(all=False)
# already filters most of these, but this is extra insurance across platforms/setups.
_IGNORED_FSTYPES = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "devfs", "overlay", "squashfs",
    "autofs", "cgroup", "cgroup2", "debugfs", "tracefs", "mqueue",
    "hugetlbfs", "pstore", "bpf", "binfmt_misc", "securityfs", "configfs",
    "fusectl", "rpc_pipefs", "nsfs",
}


def _severity(percent):
    if percent >= 85:
        return "crit"
    if percent >= 60:
        return "warn"
    return "ok"


def get_resource_snapshot():
    cpu_percent = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": cpu_percent,
        "cpu_severity": _severity(cpu_percent),
        "mem_percent": mem.percent,
        "mem_severity": _severity(mem.percent),
        "mem_used_gb": round(mem.used / (1024 ** 3), 1),
        "mem_total_gb": round(mem.total / (1024 ** 3), 1),
        "disks": _get_disk_snapshots(),
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
        disks.append({
            "path": part.mountpoint,
            "percent": usage.percent,
            "severity": _severity(usage.percent),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
        })
    return disks


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
