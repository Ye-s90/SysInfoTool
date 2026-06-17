import wmi
import pythoncom
import psutil
import platform
import socket
from datetime import datetime

try:
    import pynvml
    _HAS_NVML = True
except ImportError:
    _HAS_NVML = False


# ==================== Hardware Info ====================

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
    if not _HAS_NVML:
        return results
    try:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name_raw = pynvml.nvmlDeviceGetName(h)
            name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else name_raw
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            results.append((name, mem.total // (1024 * 1024)))
        pynvml.nvmlShutdown()
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

def _get_igpu_realtime():
    """Fallback: monitor integrated GPU via WMI when no NVIDIA GPU is present."""
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
    """Query GPU for real-time monitoring.
    Hybrid/dGPU mode -> use NVIDIA via NVML.
    iGPU-only mode   -> use display-driving GPU via WMI.
    """
    gpus = []
    # Try NVIDIA dGPU first via NVML
    if _HAS_NVML:
        try:
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
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
                gpus.append({
                    "name": name,
                    "gpu_percent": util.gpu,
                    "mem_percent": mem_percent,
                    "temp_c": temp,
                    "clock_mhz": clock,
                    "mem_used_mb": mem_used_mb,
                    "mem_total_mb": mem_total_mb,
                })
            pynvml.nvmlShutdown()
        except Exception:
            pass

    # No NVIDIA GPU found -> fall back to iGPU via WMI
    if not gpus:
        gpus = _get_igpu_realtime()
    return gpus


def _get_cpu_temp():
    """Read CPU temperature. Tries multiple methods for Intel/AMD compatibility."""
    pythoncom.CoInitialize()

    # Method 1: MSAcpi_ThermalZoneTemperature (most accurate, may need admin)
    try:
        c = wmi.WMI(namespace=r"root/wmi")
        for t in c.MSAcpi_ThermalZoneTemperature():
            val = int(t.CurrentTemperature)
            celsius = (val - 2732) / 10.0
            if 0 < celsius < 150:
                return round(celsius, 1)
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
                    return round(celsius, 1)
    except Exception:
        pass

    # Method 3: OpenHardwareMonitor WMI namespace (if OHM is running)
    try:
        c = wmi.WMI(namespace=r"root/OpenHardwareMonitor")
        for sensor in c.Sensor():
            if sensor.SensorType == "Temperature" and "CPU" in sensor.Name:
                return round(float(sensor.Value), 1)
    except Exception:
        pass

    return 0


def _get_cpu_freq_mhz():
    """Read actual CPU frequency including boost via ProcessorInformation WMI."""
    pythoncom.CoInitialize()
    try:
        c = wmi.WMI(namespace=r"root/cimv2")
        for p in c.Win32_PerfFormattedData_Counters_ProcessorInformation():
            if p.Name == "_Total":
                base = int(p.ProcessorFrequency)
                perf_pct = int(p.PercentProcessorPerformance)
                if base > 0 and perf_pct > 0:
                    return round(base * perf_pct / 100)
    except Exception:
        pass
    # Fallback to psutil
    freq = psutil.cpu_freq()
    return round(freq.current) if freq else 0


def _get_mem_freq():
    """Read actual memory frequency via WMI (ConfiguredClockSpeed)."""
    try:
        pythoncom.CoInitialize()
        c = wmi.WMI()
        freqs = [int(stick.ConfiguredClockSpeed) for stick in c.Win32_PhysicalMemory()
                 if stick.ConfiguredClockSpeed]
        return freqs[0] if freqs else 0
    except Exception:
        return 0


def get_realtime_stats():
    cpu_percent = psutil.cpu_percent(interval=0.3, percpu=False)
    cpu_freq_current = _get_cpu_freq_mhz()
    cpu_temp = _get_cpu_temp()
    mem_freq = _get_mem_freq()

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
