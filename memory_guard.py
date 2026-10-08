"""
Memory + fleet guard.

Tokyo 8Gi host (2026-08-26): spawn is not RAM-blocked when
GB_DISABLE_MEM_GUARD=1. Fleet cap is GB_MAX_GURU_BOTS / GB_MAX_BINANCE_BOTS
(default 10).

  GB_DISABLE_MEM_GUARD   (0)   — 1 = never hard-block spawns on RAM/RSS
  GB_WARN_MEM_PCT        (70)  — dashboard warning only
  GB_CRITICAL_MEM_PCT    (80)  — hard block (ignored if guard disabled)
  GB_MIN_AVAIL_MB        (300)
  GB_MAX_SWAP_PCT        (90)
  GB_PROCESS_WARN_MB     (500)
  GB_PROCESS_CRITICAL_MB (650)
  GB_MAX_GURU_BOTS       (10)  — 🧠 stepper / ALL + meme clamp
  GB_MAX_BINANCE_BOTS    (10)  — running bots per user per platform
"""
import os

try:
    import psutil
    _HAS_PSUTIL = True
except Exception:
    _HAS_PSUTIL = False


WARN_MEM_PCT = float(os.getenv("GB_WARN_MEM_PCT", "70"))
CRITICAL_MEM_PCT = float(os.getenv("GB_CRITICAL_MEM_PCT", "80"))
MIN_AVAIL_MB = float(os.getenv("GB_MIN_AVAIL_MB", "300"))
MAX_SWAP_PCT = float(os.getenv("GB_MAX_SWAP_PCT", "90"))
PROCESS_WARN_MB = float(os.getenv("GB_PROCESS_WARN_MB", "500"))
PROCESS_CRITICAL_MB = float(os.getenv("GB_PROCESS_CRITICAL_MB", "650"))
MAX_GURU_BOTS = int(os.getenv("GB_MAX_GURU_BOTS", "10"))
MAX_BINANCE_BOTS = int(os.getenv("GB_MAX_BINANCE_BOTS", str(MAX_GURU_BOTS)))
DISABLE_MEM_GUARD = os.getenv("GB_DISABLE_MEM_GUARD", "0").strip().lower() in (
    "1", "true", "yes", "on")

CRITICAL_MSG = (
    "Critical hardware limit reached — this host holds "
    f"{MAX_BINANCE_BOTS} bots per platform. Stop a bot or expand the server."
)
FLEET_MSG = (
    f"Fleet cap is {MAX_BINANCE_BOTS} bots per user per platform. "
    "Stop a bot or raise GB_MAX_BINANCE_BOTS."
)


def _read_linux_mem() -> dict:
    """Fallback via /proc/meminfo when psutil is unavailable (Linux)."""
    total = used = available = 0
    swap_total = swap_free = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                key = parts[0][:-1]
                val = int(parts[1]) * 1024  # kB -> bytes
                if key == "MemTotal":
                    total = val
                elif key == "MemAvailable":
                    available = val
                elif key == "MemFree":
                    available = available or val
                elif key == "SwapTotal":
                    swap_total = val
                elif key == "SwapFree":
                    swap_free = val
    except Exception:
        pass
    if total > 0:
        used = total - available
    return {
        "total": total, "used": used, "available": available,
        "swap_total": swap_total, "swap_free": swap_free,
    }


def _read_macos_mem() -> dict:
    """Fallback via sysctl when psutil is unavailable (macOS)."""
    import subprocess
    total = used = available = 0
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        total = int(out)
    except Exception:
        pass
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = 4096
        for line in out.splitlines():
            if "page size of" in line:
                try:
                    page = int(line.split("=")[1].split()[0])
                except Exception:
                    pass
                break
        free = active = 0
        for line in out.splitlines():
            if line.startswith("Pages free:"):
                free = int(line.split(":")[1].split()[0].replace(".", ""))
            elif line.startswith("Pages active:"):
                active = int(line.split(":")[1].split()[0].replace(".", ""))
        available = free * page
        used = active * page
    except Exception:
        pass
    return {
        "total": total, "used": used, "available": available,
        "swap_total": 0, "swap_free": 0,
    }


def process_rss_mb() -> float:
    """This process RSS in MiB (0 if unknown)."""
    if _HAS_PSUTIL:
        try:
            return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return 0.0


def snapshot() -> dict:
    """Return a dict describing current memory usage + capacity state."""
    total = used = available = 0
    swap_total = swap_free = 0
    if _HAS_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            total = vm.total
            available = vm.available
            used = max(total - available, 0)
        except Exception:
            total = used = available = 0
        try:
            sm = psutil.swap_memory()
            swap_total = sm.total
            swap_free = sm.free
        except Exception:
            pass
    if total <= 0:
        if os.path.exists("/proc/meminfo"):
            m = _read_linux_mem()
        else:
            m = _read_macos_mem()
        total, used, available = m["total"], m["used"], m["available"]
        swap_total = m.get("swap_total") or 0
        swap_free = m.get("swap_free") or 0

    rss_mb = process_rss_mb()
    if total <= 0:
        return {"total_gb": 0, "used_gb": 0, "available_gb": 0,
                "used_pct": 0.0, "critical": False, "warn": False,
                "warn_pct": WARN_MEM_PCT, "critical_pct": CRITICAL_MEM_PCT,
                "available_for_spawn": True, "error": "memory info unavailable",
                "process_rss_mb": rss_mb, "swap_used_pct": 0.0,
                "max_guru_bots": MAX_GURU_BOTS,
                "max_binance_bots": MAX_BINANCE_BOTS,
                "mem_guard_disabled": DISABLE_MEM_GUARD}

    # Pressure = un-allocatable RAM (1 - MemAvailable/MemTotal). Cache does
    # not count as pressure. 90% of "used" on a 1.9Gi box is already OOM.
    used_pct = ((total - available) / total) * 100.0 if total else 0.0
    avail_mb = available / (1024 * 1024)
    swap_used_pct = 0.0
    if swap_total > 0:
        swap_used_pct = ((swap_total - swap_free) / swap_total) * 100.0

    ram_critical = used_pct >= CRITICAL_MEM_PCT or avail_mb < MIN_AVAIL_MB
    ram_warn = used_pct >= WARN_MEM_PCT or avail_mb < (MIN_AVAIL_MB * 1.5)
    # Stale swap after a past OOM must not block a healthy box. Only treat
    # swap as critical when allocatable RAM is also tight.
    swap_critical = swap_used_pct >= MAX_SWAP_PCT and avail_mb < 500
    swap_warn = swap_used_pct >= 75 and avail_mb < 700
    rss_critical = rss_mb >= PROCESS_CRITICAL_MB
    rss_warn = rss_mb >= PROCESS_WARN_MB
    critical = bool(ram_critical or swap_critical or rss_critical)
    if DISABLE_MEM_GUARD:
        critical = False
    warn = bool(ram_warn or swap_warn or rss_warn or critical)

    gb = 1024 ** 3
    return {
        "total_gb": round(total / gb, 2),
        "used_gb": round(used / gb, 2),
        "available_gb": round(available / gb, 2),
        "used_pct": round(used_pct, 1),
        "warn": warn,
        "critical": critical,
        "warn_pct": WARN_MEM_PCT,
        "critical_pct": CRITICAL_MEM_PCT,
        "available_for_spawn": (not critical) or DISABLE_MEM_GUARD,
        "mem_guard_disabled": DISABLE_MEM_GUARD,
        "process_rss_mb": rss_mb,
        "swap_used_pct": round(swap_used_pct, 1),
        "available_mb": round(avail_mb, 0),
        "max_guru_bots": MAX_GURU_BOTS,
        "max_binance_bots": MAX_BINANCE_BOTS,
    }


def assert_capacity():
    """Raise MemoryLimitError if we are at/over the critical threshold."""
    if DISABLE_MEM_GUARD:
        return
    s = snapshot()
    if s["critical"]:
        raise MemoryLimitError(CRITICAL_MSG)


def clamp_guru_n(n, default: int = 10) -> int:
    """GuruAI fleet size: 0 = disabled, else 1..MAX_GURU_BOTS."""
    try:
        n = int(n) if n is not None else default
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        return 0
    return max(1, min(MAX_GURU_BOTS, n))


def _status_name(sub) -> str:
    st = getattr(sub, "status", None)
    if st is None:
        return ""
    return str(getattr(st, "value", None) or getattr(st, "name", None) or st).lower()


def _bot_platform(sub) -> str:
    br = getattr(sub, "bridge", None)
    p = getattr(br, "platform", None) if br is not None else None
    if p:
        return str(p).lower()
    e = getattr(sub, "entry", None)
    p = getattr(e, "platform", None) if e is not None else None
    if p:
        return str(p).lower()
    return str(getattr(sub, "platform", "") or "binance").lower()


def _bot_user(sub) -> str:
    uid = getattr(sub, "user_id", None)
    if uid:
        return str(uid)
    e = getattr(sub, "entry", None)
    return str(getattr(e, "user_id", "") or "") if e is not None else ""


def is_running_bot(sub) -> bool:
    name = _status_name(sub)
    if name in ("stopped", "idle"):
        return False
    if hasattr(sub, "is_alive"):
        try:
            return bool(sub.is_alive())
        except Exception:
            pass
    return True


def count_running_binance(orch, user_id: str = None) -> int:
    """Live Binance bots for this user (GuruAI + manual spawn)."""
    n = 0
    bots = getattr(orch, "_bots", None) or {}
    for sub in list(bots.values()):
        if not is_running_bot(sub):
            continue
        plat = _bot_platform(sub)
        if plat not in ("binance", ""):
            continue
        uid = _bot_user(sub)
        if user_id and uid and uid != user_id:
            continue
        n += 1
    return n


def remaining_binance_slots(orch, user_id: str = None) -> int:
    return max(0, MAX_BINANCE_BOTS - count_running_binance(orch, user_id))


def assert_binance_slots(orch, user_id: str = None, extra: int = 1):
    if remaining_binance_slots(orch, user_id) < extra:
        raise FleetLimitError(FLEET_MSG)


def count_running_platform(orch, user_id: str = None, platform: str = "binance") -> int:
    plat = (platform or "binance").lower()
    n = 0
    bots = getattr(orch, "_bots", None) or {}
    for sub in list(bots.values()):
        if not is_running_bot(sub):
            continue
        p = _bot_platform(sub)
        if p != plat and not (plat == "binance" and p in ("binance", "")):
            continue
        uid = _bot_user(sub)
        if user_id and uid and uid != user_id:
            continue
        n += 1
    return n


def remaining_platform_slots(orch, user_id: str = None, platform: str = "binance") -> int:
    return max(0, MAX_BINANCE_BOTS - count_running_platform(orch, user_id, platform))


def assert_platform_slots(orch, user_id: str = None, platform: str = "binance",
                          extra: int = 1):
    if remaining_platform_slots(orch, user_id, platform) < extra:
        p = (platform or "binance").lower()
        raise FleetLimitError(
            f"Fleet cap is {MAX_BINANCE_BOTS} {p} bots per user. "
            "Stop a bot or raise GB_MAX_BINANCE_BOTS.")


class MemoryLimitError(Exception):
    pass


class FleetLimitError(MemoryLimitError):
    pass
