"""
monitoring.py — Live CPU/RAM/disk/GPU snapshot for the admin-only resource
panel. No historical tracking, just a fresh reading each time the page loads.
"""
import psutil

import config


def get_resource_snapshot():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(config.MONITOR_DISK_PATH)
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "mem_percent": mem.percent,
        "mem_used_gb": round(mem.used / (1024 ** 3), 1),
        "mem_total_gb": round(mem.total / (1024 ** 3), 1),
        "disk_path": config.MONITOR_DISK_PATH,
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "gpus": _get_gpu_snapshot(),
    }


def _get_gpu_snapshot():
    """Best-effort NVIDIA GPU stats via the optional nvidia-ml-py package.
    Returns [] (never raises) if it's not installed or there's no NVIDIA GPU -
    this section simply doesn't render rather than breaking the admin page."""
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
