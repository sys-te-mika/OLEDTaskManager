#!/usr/bin/env python3
"""
OLEDTaskManager - PC sender
Collects system metrics and streams them to an Arduino over serial.

Setup:
    pip install -r requirements.txt

Optional — GPU support (NVIDIA):
    pip install GPUtil

Optional — temperatures on Windows:
    1. Download and run LibreHardwareMonitor as Administrator.
       (https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
       Enable "Options > Remote Web Server" or just keep it running.
    2. pip install wmi
    Without this, CPU/GPU temperatures will show as N/A on Windows.

Usage:
    python sender.py [COM_PORT] [BAUD_RATE]
    e.g.  python sender.py COM3 115200
"""

import sys
import time
import argparse
import serial
import psutil

# ---------------------------------------------------------------------------
# Optional: GPU via GPUtil (NVIDIA / AMD via NVML)
# ---------------------------------------------------------------------------
try:
    import GPUtil
    _GPU_MOD = True
except ImportError:
    _GPU_MOD = False

# ---------------------------------------------------------------------------
# Windows GPU fallback via Performance Counters (works for NVIDIA + AMD).
# Reads the "GPU Engine" counter which is available on Windows 10/11.
# Cache the result so we don't shell out on every 0.2 s tick.
# ---------------------------------------------------------------------------
import subprocess as _subprocess
_gpu_counter_available: bool = sys.platform == "win32"
_gpu_counter_cache: float = -1.0
_gpu_counter_last: float = 0.0
_GPU_COUNTER_INTERVAL: float = 1.0  # only query perf counters once per second


def _get_gpu_via_counter() -> float:
    """Query GPU utilisation % via Windows Performance Counters (no driver needed)."""
    global _gpu_counter_cache, _gpu_counter_last
    import time
    now = time.monotonic()
    if now - _gpu_counter_last < _GPU_COUNTER_INTERVAL:
        return _gpu_counter_cache
    _gpu_counter_last = now
    try:
        # Sum utilisation across all GPU engine nodes (3D, Copy, Video, etc.)
        cmd = (
            'powershell -NoProfile -Command "'
            "(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction Stop)"
            ".CounterSamples | Where-Object { $_.InstanceName -match 'engtype_3D' }"
            ' | Measure-Object -Property CookedValue -Sum | Select-Object -ExpandProperty Sum"'
        )
        out = _subprocess.check_output(cmd, shell=True, timeout=2,
                                       stderr=_subprocess.DEVNULL).decode().strip()
        val = min(round(float(out), 1), 100.0) if out else -1.0
        _gpu_counter_cache = val
        return val
    except Exception:
        _gpu_counter_cache = -1.0
        return -1.0

# ---------------------------------------------------------------------------
# Optional: temperatures via LibreHardwareMonitor WMI provider (Windows only)
# ---------------------------------------------------------------------------
_wmi_obj = None
try:
    import wmi  # type: ignore
    _wmi_obj = wmi.WMI(namespace=r"root\LibreHardwareMonitor")
    print("[INFO] LibreHardwareMonitor WMI provider found — "
          "temperature reading enabled.")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Metric collectors
# ---------------------------------------------------------------------------

# Exponential moving average for CPU — smooths out spikes when polling rapidly.
_cpu_ema: float = 0.0
_CPU_ALPHA: float = 0.25   # lower = smoother but slower to react

# EMA for network speeds — network is very bursty at 0.2 s intervals
_net_up_ema:   float = 0.0
_net_down_ema: float = 0.0
_NET_ALPHA:    float = 0.15  # lower than CPU — network is noisier


def get_cpu() -> float:
    """CPU utilisation in percent, EMA-smoothed."""
    global _cpu_ema
    raw = psutil.cpu_percent(interval=None)
    _cpu_ema = _CPU_ALPHA * raw + (1.0 - _CPU_ALPHA) * _cpu_ema
    return round(_cpu_ema, 1)


def get_ram() -> float:
    """RAM utilisation in percent."""
    return psutil.virtual_memory().percent


def get_disks() -> dict:
    """Disk usage % for C, D, E, F (Windows) or / (Linux). -1.0 if absent."""
    result = {}
    if sys.platform == "win32":
        for letter in ("C", "D", "E", "F"):
            try:
                result[letter] = round(psutil.disk_usage(f"{letter}:\\").percent, 1)
            except Exception:
                result[letter] = -1.0
    else:
        pct = round(psutil.disk_usage("/").percent, 1)
        for letter in ("C", "D", "E", "F"):
            result[letter] = pct if letter == "C" else -1.0
    return result


def get_gpu_usage() -> float:
    """GPU load in percent. Tries GPUtil first, then Windows perf counters."""
    if _GPU_MOD:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                return round(gpus[0].load * 100, 1)
        except Exception:
            pass
    if _gpu_counter_available:
        return _get_gpu_via_counter()
    return -1.0


def _wmi_sensor(sensor_type: str, name_fragment: str) -> float:
    """Query LibreHardwareMonitor WMI for a sensor value."""
    if _wmi_obj is None:
        return -1.0
    try:
        for sensor in _wmi_obj.Sensor():
            if (sensor.SensorType == sensor_type
                    and name_fragment.lower() in sensor.Name.lower()):
                return round(float(sensor.Value), 1)
    except Exception:
        pass
    return -1.0


def get_cpu_temp() -> float:
    """CPU temperature in °C, or -1 if unavailable."""
    # 1) Try LibreHardwareMonitor WMI (Windows, requires LHM running)
    val = _wmi_sensor("Temperature", "CPU")
    if val >= 0:
        return val
    # 2) Try psutil (Linux only — sensors_temperatures() not available on Windows)
    if hasattr(psutil, "sensors_temperatures"):
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            entries = temps.get(key, [])
            if entries:
                return round(entries[0].current, 1)
    return -1.0


def get_gpu_temp() -> float:
    """GPU temperature in °C, or -1 if unavailable."""
    if _GPU_MOD:
        try:
            gpus = GPUtil.getGPUs()
            if gpus and gpus[0].temperature is not None:
                return round(float(gpus[0].temperature), 1)
        except Exception:
            pass
    return _wmi_sensor("Temperature", "GPU")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _safe(fn, fallback: float = -1.0) -> float:
    """Call fn(); return fallback on any exception."""
    try:
        return fn()
    except Exception as exc:
        print(f"\n[WARN] metric error: {exc}")
        return fallback


def build_packet(net_up_kbs: float, net_down_kbs: float) -> bytes:
    disks = _safe(get_disks, {"C": -1.0, "D": -1.0, "E": -1.0, "F": -1.0})
    line = (
        f"CPU:{_safe(get_cpu, 0.0):.1f},"
        f"RAM:{_safe(get_ram, 0.0):.1f},"
        f"GPU:{_safe(get_gpu_usage):.1f},"
        f"DC:{disks['C']:.1f},"
        f"DD:{disks['D']:.1f},"
        f"DE:{disks['E']:.1f},"
        f"DF:{disks['F']:.1f},"
        f"CT:{_safe(get_cpu_temp):.1f},"
        f"GT:{_safe(get_gpu_temp):.1f},"
        f"NU:{net_up_kbs:.2f},"
        f"ND:{net_down_kbs:.2f}\n"
    )
    return line.encode("ascii")


def main(stop_event=None) -> None:
    """Entry point. Pass a threading.Event as stop_event to allow clean shutdown
    from external code (e.g. the tray launcher). If None, runs until Ctrl-C."""
    import threading
    if stop_event is None:
        stop_event = threading.Event()

    parser = argparse.ArgumentParser(description="OLEDTaskManager PC sender")
    parser.add_argument("port",      nargs="?", default="COM3",
                        help="Serial port (default: COM3)")
    parser.add_argument("baud",      nargs="?", default=115200, type=int,
                        help="Baud rate (default: 115200)")
    parser.add_argument("--interval", default=0.2, type=float,
                        help="Seconds between updates (default: 0.2)")
    args = parser.parse_args()

    # Prime the CPU counter (first call always returns 0.0)
    psutil.cpu_percent(interval=None)

    # Open serial port
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
        print(f"[OK] Connected to {args.port} @ {args.baud} baud")
    except serial.SerialException as exc:
        print(f"[ERROR] Cannot open serial port: {exc}")
        sys.exit(1)

    # Wait for the Arduino to finish its boot/splash sequence before sending.
    # Opening the serial port resets the Uno via DTR; it needs ~2.5 s to boot.
    print("[INFO] Waiting for Arduino to boot (2.5 s)...")
    time.sleep(2.5)

    # Initial network snapshot (taken after the boot wait so the first delta is valid)
    prev_net  = psutil.net_io_counters()
    prev_time = time.monotonic()

    print("[INFO] Streaming data — press Ctrl+C to stop.\n")

    try:
        while not stop_event.is_set():
            global _net_up_ema, _net_down_ema
            # Sleep in small chunks so stop_event is checked promptly
            for _ in range(int(args.interval / 0.05)):
                if stop_event.is_set():
                    break
                time.sleep(0.05)

            # Compute network delta
            curr_net  = psutil.net_io_counters()
            curr_time = time.monotonic()
            dt = max(curr_time - prev_time, 1e-3)

            raw_up   = (curr_net.bytes_sent - prev_net.bytes_sent) / 1024 / dt
            raw_down = (curr_net.bytes_recv - prev_net.bytes_recv) / 1024 / dt

            prev_net  = curr_net
            prev_time = curr_time

            _net_up_ema   = _NET_ALPHA * raw_up   + (1.0 - _NET_ALPHA) * _net_up_ema
            _net_down_ema = _NET_ALPHA * raw_down + (1.0 - _NET_ALPHA) * _net_down_ema
            net_up   = round(_net_up_ema,   2)
            net_down = round(_net_down_ema, 2)

            packet = build_packet(net_up, net_down)
            ser.write(packet)
            print(f"\r{packet.decode().strip():<80}", end="", flush=True)

    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
