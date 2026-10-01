from flask import Flask, render_template, jsonify, send_from_directory
import psutil
import platform
import socket
import subprocess
import threading
import time
import json
from datetime import datetime


app = Flask(__name__)


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(
        r"C:\server\shared",
        "favicon.svg",
        mimetype="image/svg+xml"
    )


# ============================================================
# CONFIGURATION
# ============================================================

# Estimated wall power consumption in watts.
# Change this after measuring your actual server consumption.
POWER_WATTS = 30.0

# Electricity price in EUR/kWh.
ELECTRICITY_PRICE = 0.258

# Health thresholds — used to build the "health" summary.
THRESHOLDS = {
    "cpu":       {"warn": 75, "crit": 90},
    "memory":    {"warn": 80, "crit": 92},
    "swap":      {"warn": 75, "crit": 90},
    "disk":      {"warn": 80, "crit": 90},
    "disk_temp": {"warn": 50, "crit": 60},
}

# Windows services we want to watch.
WATCHED_SERVICES = ["Tailscale", "W32Time", "WinDefend", "wuauserv"]

# Set to False if you don't want the server making outbound calls.
ENABLE_PUBLIC_IP = True


# ============================================================
# CACHED MONITORING DATA
# ============================================================

data = {
    "system": {},
    "processes": [],
    "disks": [],
    "tailscale": {},
    "power": {},
    "ai": {},
    "last_updated": None
}

lock = threading.Lock()


# ============================================================
# RATE TRACKING
# ============================================================

_prev_samples = {}
_prev_samples_lock = threading.Lock()


def _compute_rate(name, current, now):
    """Turn a monotonically increasing counter into a per-second rate."""
    with _prev_samples_lock:
        prev = _prev_samples.get(name)
        _prev_samples[name] = (now, current)

    if prev is None:
        return 0.0

    prev_time, prev_value = prev
    elapsed = now - prev_time
    if elapsed <= 0:
        return 0.0

    delta = current - prev_value
    if delta < 0:
        # counter wrapped or reset
        return 0.0

    return delta / elapsed


def _format_bytes_per_sec(value):
    units = ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"]
    v = float(value)
    for u in units:
        if v < 1024:
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} PB/s"


# ============================================================
# SIMPLE TTL CACHE
# ============================================================
# Disk SMART, Windows services, Tailscale and the AI status
# endpoint all spawn a subprocess. We don't want to do that on
# every 5-second poll, so cache them for a few seconds.

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, producer):
    now = time.time()

    with _cache_lock:
        entry = _cache.get(key)
        if entry and (now - entry[0]) < ttl:
            return entry[1]

    value = producer()

    with _cache_lock:
        _cache[key] = (now, value)

    return value


# ============================================================
# SYSTEM INFORMATION
# ============================================================

def get_uptime():
    uptime_seconds = time.time() - psutil.boot_time()
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)

    return f"{days}d {hours}h {minutes}m"


def get_system():
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")

    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "cpu": psutil.cpu_percent(interval=None),
        "memory": memory.percent,
        "memory_used_gb": round(memory.used / (1024 ** 3), 2),
        "memory_total_gb": round(memory.total / (1024 ** 3), 2),
        "memory_available_gb": round(memory.available / (1024 ** 3), 2),
        "disk": disk.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "uptime": get_uptime(),
        "boot_time": time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(psutil.boot_time())
        )
    }


def get_cpu_details():
    """Per-core usage, frequency, count and load average."""
    try:
        per_core = psutil.cpu_percent(interval=None, percpu=True)
    except Exception:
        per_core = []

    frequency = None
    try:
        f = psutil.cpu_freq()
        if f:
            frequency = {
                "current": round(f.current, 0),
                "min": round(f.min, 0) if f.min else None,
                "max": round(f.max, 0) if f.max else None,
            }
    except Exception:
        pass

    try:
        load_average = list(psutil.getloadavg())
    except (AttributeError, OSError):
        load_average = None

    return {
        "per_core": per_core,
        "logical_count": psutil.cpu_count(logical=True),
        "physical_count": psutil.cpu_count(logical=False),
        "frequency": frequency,
        "load_average": load_average,
    }


def get_memory_details():
    """RAM + swap/page file breakdown."""
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "virtual": {
            "percent": memory.percent,
            "used_gb": round(memory.used / (1024 ** 3), 2),
            "available_gb": round(memory.available / (1024 ** 3), 2),
            "total_gb": round(memory.total / (1024 ** 3), 2),
        },
        "swap": {
            "percent": swap.percent,
            "used_gb": round(swap.used / (1024 ** 3), 2),
            "total_gb": round(swap.total / (1024 ** 3), 2),
        },
    }


def get_disk_io():
    """Disk read/write throughput (per second, plus totals)."""
    try:
        io = psutil.disk_io_counters()
        if not io:
            return {}
    except Exception:
        return {}

    now = time.time()
    read_rate = _compute_rate("disk_read_bytes", io.read_bytes, now)
    write_rate = _compute_rate("disk_write_bytes", io.write_bytes, now)

    return {
        "read_rate": round(read_rate, 0),
        "write_rate": round(write_rate, 0),
        "read_rate_human": _format_bytes_per_sec(read_rate),
        "write_rate_human": _format_bytes_per_sec(write_rate),
        "read_total": io.read_bytes,
        "write_total": io.write_bytes,
    }


def get_network():
    """Total + per-interface network throughput."""
    try:
        io = psutil.net_io_counters()
    except Exception:
        return {}

    now = time.time()
    rx_rate = _compute_rate("net_rx_bytes", io.bytes_recv, now)
    tx_rate = _compute_rate("net_tx_bytes", io.bytes_sent, now)

    interfaces = []
    try:
        per_nic = psutil.net_io_counters(pernic=True)
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()

        for name, counters in per_nic.items():
            stat = stats.get(name)
            if not stat or not stat.isup:
                continue

            address = ""
            for a in addrs.get(name, []):
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    address = a.address
                    break

            if not address:
                continue

            interfaces.append({
                "name": name,
                "address": address,
                "speed_mbps": stat.speed if stat else 0,
                "rx_rate": round(
                    _compute_rate(f"net_rx_{name}", counters.bytes_recv, now), 0
                ),
                "tx_rate": round(
                    _compute_rate(f"net_tx_{name}", counters.bytes_sent, now), 0
                ),
                "rx_total": counters.bytes_recv,
                "tx_total": counters.bytes_sent,
            })
    except Exception:
        pass

    return {
        "rx_rate": round(rx_rate, 0),
        "tx_rate": round(tx_rate, 0),
        "rx_rate_human": _format_bytes_per_sec(rx_rate),
        "tx_rate_human": _format_bytes_per_sec(tx_rate),
        "rx_total": io.bytes_recv,
        "tx_total": io.bytes_sent,
        "interfaces": interfaces,
    }


def get_users():
    """Currently logged-in users."""
    try:
        users = []
        for u in psutil.users():
            users.append({
                "name": u.name,
                "host": u.host or "local",
                "started": time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(u.started)
                ),
            })
        return users
    except Exception:
        return []


_public_ip_cache = {"value": None, "expires": 0}


def get_public_ip():
    """External IP, cached for 1 hour."""
    if not ENABLE_PUBLIC_IP:
        return None

    now = time.time()
    if _public_ip_cache["value"] and now < _public_ip_cache["expires"]:
        return _public_ip_cache["value"]

    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3", "https://api.ipify.org"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            ip = result.stdout.strip()
            _public_ip_cache["value"] = ip
            _public_ip_cache["expires"] = now + 3600
            return ip
    except Exception:
        pass

    return _public_ip_cache["value"]


# ============================================================
# PROCESS MONITORING
# ============================================================

def _collect_processes():
    processes = []

    for process in psutil.process_iter(
        ["pid", "name", "memory_percent", "num_threads", "username"]
    ):
        try:
            cpu = process.cpu_percent(interval=None)
            info = process.info

            processes.append({
                "pid": info["pid"],
                "name": info["name"] or "Unknown",
                "cpu": round(cpu, 1),
                "memory": round(info.get("memory_percent") or 0, 1),
                "threads": info.get("num_threads") or 0,
                "username": info.get("username") or "",
            })
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess
        ):
            continue

    return processes


def get_process_data():
    """
    Collect processes once, then derive three views:
      - top 10 by CPU
      - top 10 by memory
      - overall count + thread count
    """
    processes = _collect_processes()

    by_cpu = sorted(processes, key=lambda x: x["cpu"], reverse=True)[:10]
    by_memory = sorted(processes, key=lambda x: x["memory"], reverse=True)[:10]

    stats = {
        "count": len(processes),
        "threads": sum(p["threads"] for p in processes),
    }

    return by_cpu, by_memory, stats


# ============================================================
# DISK TEMPERATURE / SMART
# ============================================================

def get_disk_temperatures():
    """
    Query physical disk info via PowerShell SMART counters.

    Each entry has: id, name, media_type, size_gb, temperature,
    wear (%), power_on_hours. `temperature` may be None when the
    drive doesn't report it (e.g. some NVMe enclosures).
    """
    try:
        ps_script = (
            "Get-PhysicalDisk | ForEach-Object { "
            "  $d = $_; $r = $d | Get-StorageReliabilityCounter; "
            "  [PSCustomObject]@{ "
            "    DeviceId = $d.DeviceId; "
            "    FriendlyName = $d.FriendlyName; "
            "    MediaType = \"$($d.MediaType)\"; "
            "    Size = $d.Size; "
            "    Temperature = $r.Temperature; "
            "    Wear = $r.Wear; "
            "    PowerOnHours = $r.PowerOnHours "
            "  } "
            "} | ConvertTo-Json"
        )

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps_script
            ],
            capture_output=True,
            text=True,
            timeout=6
        )

        if result.returncode != 0 or not result.stdout.strip():
            return []

        parsed = json.loads(result.stdout)
        if not isinstance(parsed, list):
            parsed = [parsed]

        disks = []
        for d in parsed:
            temp = d.get("Temperature")
            size = d.get("Size") or 0

            disks.append({
                "id": d.get("DeviceId"),
                "name": d.get("FriendlyName") or f"Disk {d.get('DeviceId')}",
                "media_type": d.get("MediaType") or "",
                "size_gb": round(size / (1024 ** 3), 1) if size else None,
                "temperature": float(temp) if temp is not None else None,
                "wear": d.get("Wear"),
                "power_on_hours": d.get("PowerOnHours"),
            })

        return disks

    except Exception:
        return []


# ============================================================
# TAILSCALE
# ============================================================

def get_tailscale():
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return {"online": False, "status": "Unavailable"}

        info = json.loads(result.stdout)

        state = info.get("BackendState", "Unknown")
        self_node = info.get("Self", {})
        addresses = self_node.get("TailscaleIPs", [])
        peers = info.get("Peer") or {}

        peers_online = sum(
            1 for peer in peers.values() if peer.get("Online")
        )

        return {
            "online": state == "Running",
            "status": state,
            "hostname": self_node.get("HostName", socket.gethostname()),
            "tailscale_ip": addresses[0] if addresses else "Unknown",
            "dns_name": self_node.get("DNSName", ""),
            "os": self_node.get("OS", ""),
            "peer_count": len(peers),
            "peers_online": peers_online,
        }

    except Exception:
        return {"online": False, "status": "Unavailable"}


# ============================================================
# WINDOWS SERVICES
# ============================================================

def get_services():
    """Status of a small set of watched Windows services."""
    if not WATCHED_SERVICES:
        return []

    try:
        names = ",".join(WATCHED_SERVICES)
        ps_script = (
            f"Get-Service -Name {names} -ErrorAction SilentlyContinue | "
            "Select-Object Name,DisplayName,Status | ConvertTo-Json"
        )

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps_script
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0 or not result.stdout.strip():
            return []

        parsed = json.loads(result.stdout)
        if not isinstance(parsed, list):
            parsed = [parsed]

        return [
            {
                "name": s.get("Name"),
                "display_name": s.get("DisplayName"),
                "status": str(s.get("Status")),
                "running": str(s.get("Status")).lower() == "running",
            }
            for s in parsed
        ]

    except Exception:
        return []


# ============================================================
# AI STATUS (localhost:8081)
# ============================================================

def get_ai_status():
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3",
             "http://127.0.0.1:8081/api/ai-router/status"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            providers = data.get("providers", [])
            available = [
                provider for provider in providers
                if provider.get("available")
            ]
            return {
                "available": bool(available),
                "status": (
                    "Available"
                    if available
                    else "Unavailable"
                ),
                "providers": len(providers),
                "available_providers": len(available),
                "limit": None,
                "remaining": None,
                "reset_at": None,
            }
    except Exception:
        pass

    return {}


# ============================================================
# ELECTRICITY
# ============================================================

def get_power():
    watts = POWER_WATTS
    price = ELECTRICITY_PRICE

    kwh_hour = watts / 1000
    kwh_day = kwh_hour * 24
    kwh_month = kwh_day * 30.44
    kwh_year = kwh_day * 365

    cost_hour = kwh_hour * price
    cost_day = kwh_day * price
    cost_month = kwh_month * price
    cost_year = kwh_year * price

    return {
        "watts": watts,
        "price_per_kwh": price,
        "kwh_hour": round(kwh_hour, 4),
        "kwh_day": round(kwh_day, 3),
        "kwh_month": round(kwh_month, 3),
        "kwh_year": round(kwh_year, 2),
        "cost_hour": round(cost_hour, 4),
        "cost_day": round(cost_day, 2),
        "cost_month": round(cost_month, 2),
        "cost_year": round(cost_year, 2),
    }


# ============================================================
# HEALTH SUMMARY
# ============================================================

def get_health(system, disks, memory_details):
    """
    Roll up all the thresholds into a single status:
    "healthy" / "warning" / "critical" plus a list of warnings.
    """
    warnings = []

    def add(level, message, metric):
        warnings.append({
            "level": level,
            "message": message,
            "metric": metric,
        })

    def check(value, metric, label):
        t = THRESHOLDS.get(metric)
        if not t or value is None:
            return
        if value >= t["crit"]:
            add("critical", f"{label} at {value}%", metric)
        elif value >= t["warn"]:
            add("warning", f"{label} at {value}%", metric)

    check(system.get("cpu"), "cpu", "CPU")
    check(system.get("memory"), "memory", "Memory")
    check(system.get("disk"), "disk", "Disk")

    swap_pct = (memory_details.get("swap") or {}).get("percent")
    check(swap_pct, "swap", "Swap")

    disk_temp = THRESHOLDS["disk_temp"]
    for i, d in enumerate(disks):
        temp = d.get("temperature")
        if temp is None:
            continue
        label = d.get("name") or f"Disk {i + 1}"
        if temp >= disk_temp["crit"]:
            add("critical", f"{label} at {temp}°C", "disk_temp")
        elif temp >= disk_temp["warn"]:
            add("warning", f"{label} at {temp}°C", "disk_temp")

    if not warnings:
        status = "healthy"
    elif any(w["level"] == "critical" for w in warnings):
        status = "critical"
    else:
        status = "warning"

    return {"status": status, "warnings": warnings}


# ============================================================
# API
# ============================================================

@app.route("/")
def dashboard():
    return render_template("index.html")


@app.route("/api/system")
def system_api():

    # --- Non-cached: fast local reads, always fresh ---

    system = get_system()
    memory_details = get_memory_details()
    by_cpu, by_memory, process_stats = get_process_data()

    # --- Cached: subprocess-backed, moderate TTLs ---

    disks = cached("disks", 15, get_disk_temperatures)
    tailscale = cached("tailscale", 10, get_tailscale)
    services = cached("services", 30, get_services)
    ai = cached("ai", 10, get_ai_status)

    snapshot = {
        # --- kept for backward compatibility ---
        "system": system,
        "processes": by_cpu,
        "disks": disks,
        "tailscale": tailscale,
        "power": get_power(),
        "ai": ai,
        "last_updated": datetime.now().astimezone().isoformat(),

        # --- new metrics ---
        "cpu_details": get_cpu_details(),
        "memory_details": memory_details,
        "disk_io": get_disk_io(),
        "network": get_network(),
        "top_memory": by_memory,
        "process_stats": process_stats,
        "users": get_users(),
        "services": services,
        "public_ip": get_public_ip(),
        "health": get_health(system, disks, memory_details),
    }

    with lock:
        data.update(snapshot)
        return jsonify(snapshot)


@app.route("/api/health")
def health_api():
    """Cheap endpoint for external uptime monitors."""
    return jsonify({
        "status": "ok",
        "time": datetime.now().astimezone().isoformat(),
    })


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=False,
        threaded=True
    )