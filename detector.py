import wmi
import pythoncom
import psutil
import platform
import socket
import hashlib
import threading
import time
import re
from datetime import datetime

try:
    import mss
    _HAS_MSS = True
except ImportError:
    _HAS_MSS = False

try:
    import pynvml
    _HAS_NVML = True
except ImportError:
    _HAS_NVML = False


class FPSCounter:
    """Monitor screen FPS by detecting frame changes via screen capture."""

    def __init__(self):
        self.fps = 0
        self._running = False
        self._frame_count = 0
        self._last_hash = None
        self._last_time = None

    def start(self):
        if not _HAS_MSS:
            return
        self._running = True
        threading.Thread(target=self._count_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _count_loop(self):
        sct = mss.mss()
        monitor = sct.monitors[1]  # Primary monitor
        # Capture a small region in the center to reduce overhead
        cx, cy = monitor["width"] // 2, monitor["height"] // 2
        region = {"left": cx - 160, "top": cy - 120, "width": 320, "height": 240}

        while self._running:
            try:
                img = sct.grab(region)
                # Hash a sampled slice instead of the full frame (~2x faster
                # md5, still reliably detects frame changes).
                h = hashlib.md5(img.rgb[::2]).hexdigest()
                if h != self._last_hash:
                    self._frame_count += 1
                    self._last_hash = h
            except Exception:
                pass
            # ~66 Hz sampling cap: balances FPS accuracy vs CPU usage
            time.sleep(0.015)

    def get_fps(self):
        """Return FPS normalized to per-second since the last call."""
        now = time.time()
        if self._last_time is None:
            self._last_time = now
            return 0
        dt = now - self._last_time
        fps = self._frame_count / dt if dt > 0 else 0
        self._frame_count = 0
        self._last_time = now
        self.fps = fps
        return fps


# Global FPS counter instance
_fps_counter = FPSCounter()


# ==================== Hardware Info ====================

_nvml_lock = threading.Lock()
_nvml_inited = False


def _ensure_nvml():
    """Initialize NVML once per process (nvmlInit is expensive).

    Returns True when NVML is available and initialized. Safe to call
    from multiple threads; the lock guards the first-time init only.
    """
    global _nvml_inited
    if _nvml_inited:
        return True
    if not _HAS_NVML:
        return False
    with _nvml_lock:
        if not _nvml_inited:
            try:
                pynvml.nvmlInit()
                _nvml_inited = True
            except Exception:
                return False
    return True


def get_cpu_info():
    c = wmi.WMI()
    cpus = []
    for cpu in c.Win32_Processor():
        cpus.append({
            "name": cpu.Name.strip(),
            "manufacturer": cpu.Manufacturer,
            "cores": cpu.NumberOfCores,
            "threads": cpu.NumberOfLogicalProcessors,
            "max_clock_mhz": cpu.MaxClockSpeed,
        })
    return cpus


def _get_nvml_vram_mb():
    """Get VRAM for each NVIDIA GPU via NVML. Returns list of (name, vram_mb)."""
    results = []
    if not _ensure_nvml():
        return results
    try:
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name_raw = pynvml.nvmlDeviceGetName(h)
            name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else name_raw
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            results.append((name, mem.total // (1024 * 1024)))
    except Exception:
        pass
    return results


def get_gpu_info():
    c = wmi.WMI()
    nvml_map = {n: v for n, v in _get_nvml_vram_mb()}
    gpus = []
    for gpu in c.Win32_VideoController():
        name = gpu.Name or ""
        if "virtual" in name.lower() or "remote" in name.lower():
            continue
        # Use NVML for NVIDIA GPUs to avoid WMI uint32 overflow
        if name in nvml_map:
            ram_mb = nvml_map[name]
        else:
            raw_ram = int(gpu.AdapterRAM) if gpu.AdapterRAM else 0
            if raw_ram < 0:
                raw_ram += 2**32
            ram_mb = raw_ram // (1024 * 1024)
        gpus.append({
            "name": name,
            "manufacturer": gpu.AdapterCompatibility or "Unknown",
            "vram_mb": ram_mb,
            "driver_version": gpu.DriverVersion,
        })
    return gpus


def get_memory_info():
    c = wmi.WMI()
    sticks = []
    for stick in c.Win32_PhysicalMemory():
        capacity_gb = int(stick.Capacity) // (1024 ** 3) if stick.Capacity else 0
        sticks.append({
            "manufacturer": (stick.Manufacturer or "Unknown").strip(),
            "part_number": (stick.PartNumber or "").strip(),
            "capacity_gb": capacity_gb,
            "speed_mhz": stick.Speed,
        })
    return sticks


def _clean_disk_manufacturer(disk):
    mfr = disk.Manufacturer or ""
    if mfr.startswith("(") or not mfr.isascii():
        mfr = ""
    if not mfr:
        model = disk.Model or ""
        for brand in ("Samsung", "Kingston", "Crucial", "Western Digital", "WD",
                       "Seagate", "Toshiba", "Intel", "SK Hynix", "Micron",
                       "SanDisk", "UMIS", "Phison", "ADATA", "Lexar"):
            if brand.lower() in model.lower():
                return brand
        return model.split()[0] if model else "Unknown"
    return mfr


def get_disk_info():
    c = wmi.WMI()
    disks = []
    for disk in c.Win32_DiskDrive():
        size_gb = round(int(disk.Size) / (1024 ** 3), 1) if disk.Size else 0
        disks.append({
            "model": disk.Model,
            "manufacturer": _clean_disk_manufacturer(disk),
            "size_gb": size_gb,
            "media_type": disk.MediaType or "Unknown",
            "interface": disk.InterfaceType,
        })
    return disks


def collect_hardware():
    pythoncom.CoInitialize()
    return {
        "cpu": get_cpu_info(),
        "gpu": get_gpu_info(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
    }


# ==================== Real-time Monitoring ====================

# Windows built-in GPU Engine perf counters (vendor-agnostic: works for
# NVIDIA / AMD / Intel dGPU & iGPU). Utilization matches Task Manager.
# Implemented with a persistent PDH query (pywin32); the WMI enumeration is
# kept as a fallback for systems without pywin32.
_gpu_engine_cache = {"t": 0.0, "v": {}}

# Rebuild the PDH counter set this often to follow process churn.
_PDH_REBUILD_TTL = 10.0

_ENGINE_NAME_RE = re.compile(
    r"pid_\d+_luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_\d+_eng_\d+_engtype_3D"
)

try:
    import win32pdh
    _HAS_PDH = True
except ImportError:
    _HAS_PDH = False

_pdh_state = {
    "query": None,
    "counters": {},   # instance name -> counter handle
    "built_at": 0.0,
}


def _pdh_build_query():
    """(Re)build the persistent PDH query against current GPU Engine instances."""
    st = _pdh_state
    try:
        if st["query"] is not None:
            win32pdh.CloseQuery(st["query"])
        for hc in st["counters"].values():
            try:
                win32pdh.RemoveCounter(hc)
            except Exception:
                pass
        st["query"] = None
        st["counters"] = {}

        _, instances = win32pdh.EnumObjectItems(None, None, "GPU Engine",
                                                win32pdh.PERF_DETAIL_WIZARD)
        eng3d = [i for i in instances if i.endswith("engtype_3D")]
        if not eng3d:
            return
        hq = win32pdh.OpenQuery()
        st["query"] = hq
        for inst in eng3d:
            path = r"\GPU Engine(" + inst + r")\Utilization Percentage"
            try:
                st["counters"][inst] = win32pdh.AddCounter(hq, path)
            except Exception:
                pass
        st["built_at"] = time.time()
        # Warm up so the first read after a rebuild already has a baseline.
        try:
            win32pdh.CollectQueryData(hq)
            time.sleep(0.15)
            win32pdh.CollectQueryData(hq)
        except Exception:
            pass
    except Exception:
        pass


def _get_gpu_engine_3d_utils():
    """Return {luid_key: max 3D util%} via the Windows GPU Engine counters.

    Vendor-agnostic. The PDH utilization counter needs two collection samples;
    since the monitor loop calls this about once a second, values represent
    roughly the last-second average. Falls back to the WMI enumeration when
    pywin32 is unavailable, and to an empty dict on any failure.
    """
    now = time.time()
    if now - _gpu_engine_cache["t"] < _SLOW_TTL:
        return _gpu_engine_cache["v"]

    utils = {}
    try:
        if _HAS_PDH:
            st = _pdh_state
            if st["query"] is None or now - st["built_at"] > _PDH_REBUILD_TTL:
                _pdh_build_query()
            if st["query"] is not None:
                win32pdh.CollectQueryData(st["query"])
                for inst, hc in st["counters"].items():
                    try:
                        _, val = win32pdh.GetFormattedCounterValue(hc, win32pdh.PDH_FMT_DOUBLE)
                    except Exception:
                        continue
                    m = _ENGINE_NAME_RE.match(inst)
                    if not m:
                        continue
                    v = int(round(val))
                    key = m.group(1)
                    utils[key] = max(utils.get(key, 0), v)
            # PDH path (even when empty on the very first sample) is
            # authoritative; never fall through to the slow WMI cold start.
            _gpu_engine_cache["t"] = now
            _gpu_engine_cache["v"] = utils
            return utils
        # WMI fallback only when pywin32 is unavailable (slow cold start)
        pythoncom.CoInitialize()
        c = wmi.WMI(namespace=r"root/cimv2")
        for e in c.Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine():
            m = _ENGINE_NAME_RE.match(e.Name)
            if not m:
                continue
            key = m.group(1)
            try:
                v = int(e.UtilizationPercentage)
            except (TypeError, ValueError):
                v = 0
            utils[key] = max(utils.get(key, 0), v)
    except Exception:
        pass
    _gpu_engine_cache["t"] = now
    _gpu_engine_cache["v"] = utils
    return utils


def _list_phys_gpus():
    """Enumerate physical GPUs (virtual/remote filtered) with VRAM + display flag."""
    gpus = []
    try:
        pythoncom.CoInitialize()
        c = wmi.WMI()
        for gpu in c.Win32_VideoController():
            name = gpu.Name or ""
            if "virtual" in name.lower() or "remote" in name.lower():
                continue
            raw_ram = int(gpu.AdapterRAM) if gpu.AdapterRAM else 0
            if raw_ram < 0:
                raw_ram += 2**32
            gpus.append({
                "name": name,
                "vram_mb": raw_ram // (1024 * 1024),
            })
    except Exception:
        pass
    return gpus


def _build_engine_gpu(util_pct, phys):
    """Build a GPU stats dict from GPU Engine / WMI data (non-NVIDIA path).

    Utilization comes from the perf counters; temperature, clock and VRAM
    usage have no public API on non-NVIDIA GPUs -> -1 / 0.
    """
    g = phys[0] if phys else {"name": "GPU", "vram_mb": 0}
    return {
        "name": g["name"],
        "gpu_percent": util_pct,
        "mem_percent": -1,
        "temp_c": -1,
        "clock_mhz": -1,
        "mem_used_mb": 0,
        "mem_total_mb": g["vram_mb"],
    }


def _get_igpu_realtime():
    """Final fallback: report the display-driving GPU with VRAM only."""
    try:
        pythoncom.CoInitialize()
        c = wmi.WMI()
        for gpu in c.Win32_VideoController():
            name = gpu.Name or ""
            if "virtual" in name.lower() or "remote" in name.lower():
                continue
            # Only pick the display-driving GPU (has a video mode)
            if not gpu.VideoModeDescription:
                continue
            raw_ram = int(gpu.AdapterRAM) if gpu.AdapterRAM else 0
            if raw_ram < 0:
                raw_ram += 2**32
            total_mb = raw_ram // (1024 * 1024)
            return [{
                "name": name,
                "gpu_percent": -1,   # -1 means not available
                "mem_percent": -1,
                "temp_c": -1,
                "clock_mhz": -1,
                "mem_used_mb": 0,
                "mem_total_mb": total_mb,
            }]
    except Exception:
        pass
    return []


def get_gpu_realtime():
    """Monitor GPU in real time.

    1. NVIDIA GPU present & NVML available -> full NVML data
       (util / temp / clock / VRAM usage). The dGPU is always reported when
       NVML works, so discrete-only and hybrid laptops behave consistently.
    2. Otherwise (iGPU-only machines, AMD/Intel dGPU, or NVML unavailable)
       -> Windows GPU Engine perf counters (3D utilization, vendor-agnostic)
       + WMI VRAM total; temp / clock / VRAM usage stay -1 (no public API
       for non-NVIDIA GPUs).
    """
    # Path 1: NVIDIA via NVML
    if _ensure_nvml():
        try:
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                h = pynvml.nvmlDeviceGetHandleByIndex(0)
                name_raw = pynvml.nvmlDeviceGetName(h)
                name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else name_raw
                util = pynvml.nvmlDeviceGetUtilizationRates(h)
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    temp = 0
                try:
                    clock = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
                except Exception:
                    clock = 0
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(h)
                mem_used_mb = mem_info.used // (1024 * 1024)
                mem_total_mb = mem_info.total // (1024 * 1024)
                mem_percent = round(mem_info.used / mem_info.total * 100) if mem_info.total else 0
                return [{
                    "name": name,
                    "gpu_percent": util.gpu,
                    "mem_percent": mem_percent,
                    "temp_c": temp,
                    "clock_mhz": clock,
                    "mem_used_mb": mem_used_mb,
                    "mem_total_mb": mem_total_mb,
                }]
        except Exception:
            pass

    # Path 2: no NVIDIA / NVML unavailable -> vendor-agnostic engine counters
    phys = _list_phys_gpus()
    engine_utils = _get_gpu_engine_3d_utils()
    global_max3d = max(engine_utils.values()) if engine_utils else 0
    if engine_utils or phys:
        return [_build_engine_gpu(global_max3d, phys)]

    # Path 3: last resort (WMI counters unavailable) -> VRAM-only report
    return _get_igpu_realtime()


# Short-lived caches for slow WMI readings (monitor loop refreshes ~1s;
# temps/frequencies change slowly, so re-querying every 2s is plenty).
# The monitor loop is the only consumer -> single-threaded access is safe.
_SLOW_TTL = 2.0
_cpu_temp_cache = {"t": 0.0, "v": 0}
_cpu_freq_cache = {"t": 0.0, "v": 0}
_mem_freq_cache = {"t": 0.0, "v": 0}


def _cache_result(cache, now, value):
    cache["t"] = now
    cache["v"] = value
    return value


def _get_cpu_temp():
    """Read CPU temperature. Tries multiple methods for Intel/AMD compatibility."""
    now = time.time()
    if now - _cpu_temp_cache["t"] < _SLOW_TTL:
        return _cpu_temp_cache["v"]

    pythoncom.CoInitialize()

    # Method 1: MSAcpi_ThermalZoneTemperature (most accurate, may need admin)
    try:
        c = wmi.WMI(namespace=r"root/wmi")
        for t in c.MSAcpi_ThermalZoneTemperature():
            val = int(t.CurrentTemperature)
            celsius = (val - 2732) / 10.0
            if 0 < celsius < 150:
                return _cache_result(_cpu_temp_cache, now, round(celsius, 1))
    except Exception:
        pass

    # Method 2: ThermalZone perf counter (unit: tenths of Kelvin)
    try:
        c = wmi.WMI(namespace=r"root/cimv2")
        for t in c.Win32_PerfFormattedData_Counters_ThermalZoneInformation():
            val = int(t.HighPrecisionTemperature)
            if val > 0:
                celsius = val / 10.0 - 273.15
                if 0 < celsius < 150:
                    return _cache_result(_cpu_temp_cache, now, round(celsius, 1))
    except Exception:
        pass

    # Method 3: OpenHardwareMonitor WMI namespace (if OHM is running)
    try:
        c = wmi.WMI(namespace=r"root/OpenHardwareMonitor")
        for sensor in c.Sensor():
            if sensor.SensorType == "Temperature" and "CPU" in sensor.Name:
                return _cache_result(_cpu_temp_cache, now, round(float(sensor.Value), 1))
    except Exception:
        pass

    return _cache_result(_cpu_temp_cache, now, 0)


def _get_cpu_freq_mhz():
    """Read actual CPU frequency including boost via ProcessorInformation WMI."""
    now = time.time()
    if now - _cpu_freq_cache["t"] < _SLOW_TTL:
        return _cpu_freq_cache["v"]

    pythoncom.CoInitialize()
    try:
        c = wmi.WMI(namespace=r"root/cimv2")
        for p in c.Win32_PerfFormattedData_Counters_ProcessorInformation():
            if p.Name == "_Total":
                base = int(p.ProcessorFrequency)
                perf_pct = int(p.PercentProcessorPerformance)
                if base > 0 and perf_pct > 0:
                    return _cache_result(_cpu_freq_cache, now, round(base * perf_pct / 100))
    except Exception:
        pass
    # Fallback to psutil
    freq = psutil.cpu_freq()
    result = round(freq.current) if freq else 0
    return _cache_result(_cpu_freq_cache, now, result)


def _get_mem_freq():
    """Read actual memory frequency via WMI (ConfiguredClockSpeed)."""
    now = time.time()
    if now - _mem_freq_cache["t"] < _SLOW_TTL:
        return _mem_freq_cache["v"]

    try:
        pythoncom.CoInitialize()
        c = wmi.WMI()
        freqs = [int(stick.ConfiguredClockSpeed) for stick in c.Win32_PhysicalMemory()
                 if stick.ConfiguredClockSpeed]
        return _cache_result(_mem_freq_cache, now, freqs[0] if freqs else 0)
    except Exception:
        return _cache_result(_mem_freq_cache, now, 0)


def get_realtime_stats():
    global _fps_counter
    # Start FPS counter on first call
    if _HAS_MSS and not _fps_counter._running:
        _fps_counter.start()

    # interval=None -> non-blocking; returns average since the previous call
    # (first call yields 0.0). Saves ~0.3s of blocking per monitor tick.
    cpu_percent = psutil.cpu_percent(interval=None, percpu=False)
    cpu_freq_current = _get_cpu_freq_mhz()
    cpu_temp = _get_cpu_temp()
    mem_freq = _get_mem_freq()
    fps = round(_fps_counter.get_fps()) if _HAS_MSS else -1

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    disk_io = psutil.disk_io_counters()
    disk_parts = []
    for p in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(p.mountpoint)
            disk_parts.append({
                "device": p.device,
                "mountpoint": p.mountpoint,
                "total_gb": round(usage.total / (1024**3), 1),
                "used_gb": round(usage.used / (1024**3), 1),
                "percent": usage.percent,
            })
        except (PermissionError, OSError):
            pass

    net_io = psutil.net_io_counters()

    return {
        "cpu_percent": cpu_percent,
        "cpu_freq_mhz": cpu_freq_current,
        "cpu_temp_c": cpu_temp,
        "mem_total_gb": round(mem.total / (1024**3), 1),
        "mem_used_gb": round(mem.used / (1024**3), 1),
        "mem_percent": mem.percent,
        "mem_freq_mhz": mem_freq,
        "swap_total_gb": round(swap.total / (1024**3), 1),
        "swap_percent": swap.percent,
        "disk_partitions": disk_parts,
        "disk_read_mb": round(disk_io.read_bytes / (1024**2)) if disk_io else 0,
        "disk_write_mb": round(disk_io.write_bytes / (1024**2)) if disk_io else 0,
        "net_sent_mb": round(net_io.bytes_sent / (1024**2)) if net_io else 0,
        "net_recv_mb": round(net_io.bytes_recv / (1024**2)) if net_io else 0,
        "fps": fps,
        "gpu": get_gpu_realtime(),
    }


# ==================== System Info ====================

def get_system_info():
    uname = platform.uname()
    boot_time = psutil.boot_time()
    boot_dt = datetime.fromtimestamp(boot_time)

    net_if = psutil.net_if_addrs()
    adapters = []
    for name, addrs in net_if.items():
        ips = []
        mac = ""
        for addr in addrs:
            if addr.family.name == "AF_INET":
                ips.append(addr.address)
            elif addr.family.name == "AF_PACKET":
                mac = addr.address
        adapters.append({"name": name, "ips": ips, "mac": mac})

    return {
        "os": f"{uname.system} {uname.release} ({uname.version})",
        "hostname": uname.node,
        "machine_arch": uname.machine,
        "processor": uname.processor,
        "python_version": platform.python_version(),
        "boot_time": boot_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "adapters": adapters,
    }
