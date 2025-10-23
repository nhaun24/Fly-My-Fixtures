import os, json, time, threading, queue, csv, io, sys, subprocess, socket, re, tempfile
from datetime import datetime
from flask import Flask, request, jsonify, Response, send_file
import pygame
import sacn

# headless SDL (no X server needed)
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

APP = Flask(__name__)

SETTINGS_PATH = "settings.json"
FIXTURES_CSV  = "fixtures.csv"
LOG_MAX = 5000
FIXTURE_LIMIT = 6

# ---------------- Defaults ----------------
DEFAULTS = {
    # legacy single-universe defaults (still used as defaults)
    "universe": 1,
    "priority": 150,
    "per_address_priority_enabled": True,
    "fps": 60,
    "sacn_bind_addresses": [],

    # --- GPIO LED settings ---
    "gpio_enabled": True,
    "gpio_green_pin": 17,   # BCM numbering (GPIO17)
    "gpio_red_pin": 27,     # BCM numbering (GPIO27)
    "gpio_active_low": False,  # set True if you wire LED to 3.3V and sink to GND via GPIO
    "gpio_fixture_led_pins": [],   # up to 6 BCM pins for per-fixture LEDs

    # HOTAS axis / button mapping (verify with discover endpoint if needed)
    "ax_pan": 0,    # stick X
    "ax_tilt": 1,   # stick Y
    "ax_throt": 2,  # throttle

    # Buttons
    "btn_activate": 5,   # start sending / take control
    "btn_release": 1,    # stream-terminate / release
    "btn_flash10": 0,    # trigger: hold = ~10% intensity
    "btn_dim_off": 3,    # hold = blackout
    "btn_fine": 4,       # hold = slow pan/tilt
    "btn_zoom_mod": 6,   # legacy: hold = map throttle -> ZOOM (ignored if ax_zoom >= 0)

    # Inversions
    "invert_pan": True,
    "invert_tilt": False,
    "throttle_invert": False,

    # DMX patch (legacy single-fixture fields; still editable, but superseded by fixtures[])
    "ch_pan_coarse": 1,
    "ch_pan_fine": 2,
    "ch_tilt_coarse": 3,
    "ch_tilt_fine": 4,
    "ch_dimmer": 5,
    "ch_zoom": 6,
    "ch_zoom_fine": 0,   # >0 to enable 16-bit zoom
    # Optional static color temperature channel for the legacy single-fixture mode
    "ch_color_temp": 11,
    "color_temp_value": 0,

    # Soft limits (0..65535)
    "pan_min": 2000,
    "pan_max": 63000,
    "tilt_min": 3000,
    "tilt_max": 60000,

    # Motion feel
    "deadband": 0.03,
    "expo": 0.6,
    "speed": 2200,
    "fine_divisor": 3,

    # Flash level
    "flash10_level": 26,

    # ---- Dynamic fixtures & multi-universe toggle ----
    "multi_universe_enabled": False,   # start single-universe; fan-out when enabled
    "default_universe": 1,             # used if MU disabled and as default for new fixtures

    "button_actions": [],  # e.g. [{"button":7,"type":"toggle_fixture","targets":["Left"]}]

    # Fixtures list (editable in UI)
    "fixtures": [],

    # ---- Virtual joystick toggle/behavior ----
    "virtual_joystick_enabled": True,
    "virtual_throttle_invert": True,   # client/UI uses this to flip slider mapping

    # --- Debug: log sACN output frames (throttled) ---
    "debug_log_sacn": False,
    "debug_log_interval_ms": 500,      # per-universe throttle
    "debug_log_only_changes": True,    # only log on frame changes
    "debug_log_mode": "summary",       # summary | nonzero | full
    "debug_log_nonzero_limit": 64,     # max pairs in nonzero mode (0=all)

    # --- Controller debug ---
    "debug_controller_buttons": False,

    # --- Dedicated Zoom Axis (rocker) ---
    "ax_zoom": 7,            # set to the rocker’s axis index (7 = T.Flight HOTAS rocker); -1 disables legacy zoom-mod
    "zoom_invert": False,    # invert the rocker direction
    "zoom_deadband": 0.05,   # like pan/tilt deadband
    "zoom_expo": 0.4,        # response curve
    "zoom_speed": 3000       # 16-bit units per frame (like speed for pan/tilt)
    }

state_lock = threading.Lock()
settings = {}   # loaded at start
logs = queue.Queue()  # producer/consumer for log lines
log_store = []        # last LOG_MAX lines (for UI)
status = {
    "active": False,         # streaming (taking control)
    "error": False,          # last loop error
    "error_msg": "",
    "joystick_name": "",
    "axes": 0,
    "buttons": 0,
    "last_frame_ts": None
    }

# --- Virtual joystick state (used when virtual_joystick_enabled=True) ---
virtual_state = {
    "x": 0.0,         # stick X in [-1, 1]
    "y": 0.0,         # stick Y in [-1, 1]
    "throttle": -1.0, # axis in [-1, 1]  (like a real axis; -1=full, +1=empty)
    "zaxis": 0.0,     # rocker axis in [-1, 1]
    "buttons": {}     # { index:int -> 0/1 }
    }

capture_lock = threading.Lock()
packet_capture = {
    "active": False,
    "interface": None,
    "path": None,
    "filename": None,
    "started": None,
    "stopped": None,
    "process": None,
    "error": None,
}

def vclamp(v):
    try:
        return max(-1.0, min(1.0, float(v)))
    except Exception:
        return 0.0

# ---------------- Persistence helpers ----------------

FIXTURE_FIELDS = [
    "id","enabled","universe","start_addr",
    "pan_coarse","pan_fine","tilt_coarse","tilt_fine",
    "dimmer","zoom","zoom_fine",
    "color_temp_channel","color_temp_value",
    "invert_pan","invert_tilt","pan_bias","tilt_bias",
    "status_led"
    ]

def clamp_fixtures(fixtures):
    try:
        fixtures = list(fixtures or [])
    except Exception:
        return []
    if len(fixtures) <= FIXTURE_LIMIT:
        return fixtures
    return fixtures[:FIXTURE_LIMIT]

def normalize_fixture_led_pins(pins):
    normalized = []
    for pin in pins or []:
        try:
            value = int(pin)
        except Exception:
            continue
        if value in normalized:
            continue
        normalized.append(value)
        if len(normalized) >= FIXTURE_LIMIT:
            break
    return normalized

def sanitize_bind_addresses(values):
    cleaned = []
    seen = set()
    items = values
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [values]
        except Exception:
            items = [values]
    for value in items or []:
        if value is None:
            continue
        addr = str(value).strip()
        if not addr:
            continue
        if addr in ("0.0.0.0", "255.255.255.255"):
            continue
        if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", addr):
            continue
        if addr in seen:
            continue
        parts = addr.split('.')
        if any(int(p) > 255 for p in parts if p.isdigit()):
            continue
        cleaned.append(addr)
        seen.add(addr)
    return cleaned

def list_network_interfaces():
    adapters = []
    seen = set()

    def add_adapter(name, addr, loopback=False, description=""):
        if not addr:
            return
        if addr in ("0.0.0.0", "255.255.255.255"):
            return
        key = f"{name}|{addr}"
        if key in seen:
            return
        adapters.append({
            "name": name,
            "address": addr,
            "label": f"{name} – {addr}",
            "is_loopback": bool(loopback),
            "description": description or ""
        })
        seen.add(key)

    # Try psutil if available (covers most platforms)
    try:
        import psutil  # type: ignore
        for name, addr_list in psutil.net_if_addrs().items():
            for info in addr_list:
                if getattr(info, "family", None) == socket.AF_INET:
                    add_adapter(name, info.address, info.address.startswith("127."))
    except Exception:
        pass

    # POSIX fallback using ioctl
    if not adapters:
        try:
            names = []
            if hasattr(socket, "if_nameindex"):
                names = [name for _, name in socket.if_nameindex()]
            elif os.path.isdir("/sys/class/net"):
                names = os.listdir("/sys/class/net")
            for name in names:
                try:
                    import fcntl, struct  # type: ignore
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    res = fcntl.ioctl(
                        s.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack('256s', name.encode('utf-8'))
                    )
                    addr = socket.inet_ntoa(res[20:24])
                    add_adapter(name, addr, addr.startswith("127."))
                except Exception:
                    continue
        except Exception:
            pass

    # Parse `ip -4 addr show`
    if not adapters:
        try:
            out = subprocess.check_output(["ip", "-4", "addr", "show"], text=True, encoding="utf-8", errors="ignore")
            current = None
            for line in out.splitlines():
                if not line:
                    continue
                if not line.startswith(" "):
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        current = parts[1].strip().split("@")[0]
                else:
                    line = line.strip()
                    if line.startswith("inet ") and current:
                        addr = line.split()[1].split("/")[0]
                        add_adapter(current, addr, addr.startswith("127."))
        except Exception:
            pass

    # Windows ipconfig fallback
    if not adapters and os.name == "nt":
        try:
            out = subprocess.check_output(["ipconfig"], text=True, encoding="utf-8", errors="ignore")
            current = None
            for raw in out.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if raw and not raw.startswith(" ") and not raw.startswith("\t"):
                    current = line.rstrip(":")
                    continue
                match = re.search(r"IPv4 Address[^:]*:\s*([0-9.]+)", line)
                if match and current:
                    addr = match.group(1)
                    add_adapter(current, addr, addr.startswith("127."))
        except Exception:
            pass

    adapters.sort(key=lambda item: (item["is_loopback"], item["name"], item["address"]))
    return adapters

def get_sacn_bind_addresses():
    return sanitize_bind_addresses(settings.get("sacn_bind_addresses", []))


# ---------------- Packet capture helpers ----------------

def _capture_ts_to_iso(ts):
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(float(ts)).isoformat() + "Z"
    except Exception:
        return None


def _safe_capture_filename(interface):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", interface or "iface")
    now = datetime.utcnow()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    fractional = now.strftime("%f")[:3]
    return f"sacn_capture_{safe}_{timestamp}-{fractional}.pcap"


def _monitor_packet_capture(proc):
    stderr_text = ""
    try:
        if proc.stderr:
            stderr_text = proc.stderr.read() or ""
    except Exception:
        stderr_text = ""
    finally:
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass

    rc = None
    try:
        rc = proc.wait()
    except Exception:
        rc = None

    with capture_lock:
        if packet_capture.get("process") is proc:
            packet_capture["process"] = None
            packet_capture["active"] = False
            packet_capture["stopped"] = time.time()
            if rc not in (0, None) and not packet_capture.get("error"):
                packet_capture["error"] = (stderr_text or "tcpdump exited with an error").strip()

    if rc == 0:
        log("Packet capture finished")
    elif rc not in (None, 0):
        log(f"Packet capture exited with code {rc}")


def start_packet_capture(interface_name):
    iface = str(interface_name or "").strip()
    if not iface:
        return False, "Interface is required"

    filename = _safe_capture_filename(iface)
    path = os.path.join(tempfile.gettempdir(), filename)

    with capture_lock:
        if packet_capture.get("active") and packet_capture.get("process"):
            return False, "Packet capture is already running"
        previous_path = packet_capture.get("path")

    command = [
        "tcpdump",
        "-i",
        iface,
        "-n",
        "-w",
        path,
        "udp",
        "port",
        "5568",
    ]

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        message = "tcpdump is not available. Install tcpdump to enable packet capture."
        with capture_lock:
            packet_capture["error"] = message
        log(f"Packet capture failed to start: {message}")
        return False, message
    except PermissionError:
        message = "Permission denied while starting tcpdump. Run with elevated privileges."
        with capture_lock:
            packet_capture["error"] = message
        log(f"Packet capture failed to start: {message}")
        return False, message
    except Exception as exc:
        message = f"Failed to start tcpdump: {exc}"
        with capture_lock:
            packet_capture["error"] = message
        log(f"Packet capture failed to start: {message}")
        return False, message

    time.sleep(0.3)
    if proc.poll() is not None:
        err_text = ""
        try:
            if proc.stderr:
                err_text = proc.stderr.read().strip()
        except Exception:
            err_text = ""
        finally:
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
        message = err_text or "tcpdump exited immediately"
        with capture_lock:
            packet_capture["error"] = message
        log(f"Packet capture failed to start: {message}")
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return False, message

    with capture_lock:
        if previous_path and previous_path != path and os.path.exists(previous_path):
            try:
                os.remove(previous_path)
            except Exception:
                pass
        packet_capture.update({
            "interface": iface,
            "filename": filename,
            "path": path,
            "started": time.time(),
            "stopped": None,
            "error": None,
            "active": True,
            "process": proc,
        })

    threading.Thread(target=_monitor_packet_capture, args=(proc,), daemon=True).start()
    log(f"Packet capture started on {iface}")
    return True, None


def stop_packet_capture():
    with capture_lock:
        proc = packet_capture.get("process")
        if not proc:
            if packet_capture.get("active"):
                packet_capture["active"] = False
            return False, "No packet capture is currently running"
        packet_capture["active"] = False

    try:
        proc.terminate()
    except Exception:
        pass

    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
    except Exception:
        pass

    log("Packet capture stopped")
    return True, None


def get_packet_capture_status():
    with capture_lock:
        data = {
            "active": bool(packet_capture.get("active")),
            "interface": packet_capture.get("interface"),
            "filename": packet_capture.get("filename"),
            "error": packet_capture.get("error"),
            "started_at": _capture_ts_to_iso(packet_capture.get("started")),
            "stopped_at": _capture_ts_to_iso(packet_capture.get("stopped")),
        }
        path = packet_capture.get("path")
        active = bool(packet_capture.get("active"))

    size = 0
    if path and os.path.exists(path):
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0

    data["filesize"] = size
    data["download_ready"] = bool(path and os.path.exists(path) and size > 0 and not active)
    data["active"] = bool(active)
    return data


def _apply_sender_bind_addresses(sender, bind_addrs):
    """Attempt to configure `sender` to bind to ``bind_addrs``.

    Returns a tuple ``(configured: bool, used_addrs: list[str])`` describing the
    result.  The helper tries attribute assignment as well as legacy setter
    helpers so we gracefully support different python-sACN versions without
    relying on introspection data that may be missing on C-implemented methods.
    """

    def _normalize_used(value, fallback):
        if not value:
            value = fallback
        if isinstance(value, str):
            return [value]
        try:
            return [str(v) for v in value]
        except Exception:
            return [str(value)]

    if not bind_addrs:
        return False, []

    attr_attempts = [
        ("bind_addresses", bind_addrs),
        ("bind_address", bind_addrs[0]),
    ]

    for attr, value in attr_attempts:
        if not hasattr(sender, attr):
            continue
        try:
            setattr(sender, attr, value)
        except Exception:
            continue
        try:
            current = getattr(sender, attr)
        except Exception:
            current = None
        return True, _normalize_used(current, value)

    setter_attempts = [
        ("set_bind_addresses", bind_addrs),
        ("set_bind_address", bind_addrs[0]),
    ]

    for name, value in setter_attempts:
        method = getattr(sender, name, None)
        if not callable(method):
            continue
        try:
            method(value)
        except Exception:
            continue
        return True, _normalize_used(None, value)

    return False, []

def fixtures_to_csv(fixtures):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIXTURE_FIELDS)
    w.writeheader()
    for f in clamp_fixtures(fixtures):
        row = {k: f.get(k, "") for k in FIXTURE_FIELDS}
        row["enabled"]    = "True"  if f.get("enabled", False) else "False"
        row["invert_pan"] = "True"  if f.get("invert_pan", False) else "False"
        row["invert_tilt"]= "True"  if f.get("invert_tilt", False) else "False"
        w.writerow(row)
    return buf.getvalue()

def csv_to_fixtures(text):
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for row in rd:
        out.append(normalize_fixture(row))
    return out

def write_fixtures_csv():
    try:
        with open(FIXTURES_CSV, "w", newline="") as f:
            f.write(fixtures_to_csv(settings.get("fixtures", [])))
    except Exception:
        pass

def maybe_load_fixtures_csv_into_settings():
    if settings.get("fixtures"):
        return
    if not os.path.exists(FIXTURES_CSV):
        return
    try:
        with open(FIXTURES_CSV, "r") as f:
            fixtures = csv_to_fixtures(f.read())
        if fixtures:
            settings["fixtures"] = clamp_fixtures(fixtures)
    except Exception:
        pass

def set_fixture_enabled_by_id(fid: str, enabled: bool) -> bool:
    changed = False
    arr = settings.get("fixtures", [])
    for f in arr:
        if str(f.get("id","")) == str(fid):
            if bool(f.get("enabled", False)) != bool(enabled):
                f["enabled"] = bool(enabled)
                changed = True
    if changed:
        save_settings()
        log(f"Fixture {'ENABLED' if enabled else 'DISABLED'}: {fid}")
    return changed

def toggle_fixture_by_id(fid: str) -> bool:
    arr = settings.get("fixtures", [])
    for f in arr:
        if str(f.get("id","")) == str(fid):
            f["enabled"] = not bool(f.get("enabled", False))
            save_settings()
            log(f"Fixture toggled ({'EN' if f['enabled'] else 'DIS'}): {fid}")
            return True
    return False

def load_settings():
    global settings
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, "r") as f:
            data = json.load(f)
    else:
        data = {}
    merged = DEFAULTS.copy()
    for k, v in data.items():
        merged[k] = v
    merged["fixtures"] = clamp_fixtures(merged.get("fixtures", []))
    merged["sacn_bind_addresses"] = sanitize_bind_addresses(merged.get("sacn_bind_addresses", []))
    settings = merged
    maybe_load_fixtures_csv_into_settings()
    save_settings()

def save_settings():
    settings["fixtures"] = clamp_fixtures(settings.get("fixtures", []))
    settings["gpio_fixture_led_pins"] = normalize_fixture_led_pins(settings.get("gpio_fixture_led_pins", []))
    settings["sacn_bind_addresses"] = sanitize_bind_addresses(settings.get("sacn_bind_addresses", []))
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    write_fixtures_csv()
    try:
        update_fixture_leds()
    except Exception:
        pass
    try:
        dmx_monitor.update_from_settings()
    except Exception:
        pass

def log(line):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {line}"
    logs.put(entry)

def flush_logs():
    while True:
        try:
            entry = logs.get_nowait()
        except queue.Empty:
            break
        log_store.append(entry)
    if len(log_store) > LOG_MAX:
        del log_store[:len(log_store)-LOG_MAX]

def expo_curve(v, expo, deadband):
    if abs(v) < deadband: return 0.0
    s = 1.0 if v >= 0 else -1.0
    return s * (abs(v) ** (1.0 + expo))

def clamp16(x): return max(0, min(65535, int(x)))

def clamp8(x):
    try:
        return max(0, min(255, int(x)))
    except Exception:
        return 0

def to16(v):
    v = clamp16(v)
    return (v >> 8) & 0xFF, v & 0xFF

# ---- GPIO LED helper (green=active, red=error) ----
class LedGPIO:
    def __init__(self, enabled, green_pin, red_pin, active_low):
        self.enabled = enabled
        self.green = None
        self.red = None
        if not enabled:
            return
        try:
            from gpiozero import LED
            self.green = LED(green_pin, active_high=not active_low)
            self.red   = LED(red_pin,   active_high=not active_low)
        except Exception:
            try:
                import RPi.GPIO as GPIO
                self.GPIO = GPIO
                GPIO.setmode(GPIO.BCM)
                self.GPIO.setwarnings(False)
                GPIO.setup(green_pin, GPIO.OUT, initial=GPIO.LOW if not active_low else GPIO.HIGH)
                GPIO.setup(red_pin,   GPIO.OUT, initial=GPIO.LOW if not active_low else GPIO.HIGH)
                self.green_pin = green_pin
                self.red_pin = red_pin
                self.active_low = active_low
            except Exception:
                self.enabled = False

    def _write(self, pin, on):
        if hasattr(self, "GPIO"):
            level = (self.GPIO.LOW if on else self.GPIO.HIGH) if not self.active_low else (self.GPIO.HIGH if on else self.GPIO.LOW)
            self.GPIO.output(pin, level)

    def set(self, active: bool, error: bool):
        if not self.enabled:
            return
        red_on = bool(error)
        green_on = bool(active and not error)
        if self.green and self.red:
            (self.green.on() if green_on else self.green.off())
            (self.red.on()   if red_on   else self.red.off())
        elif hasattr(self, "GPIO"):
            self._write(self.green_pin, green_on)
            self._write(self.red_pin, red_on)

    def off(self):
        self.set(False, False)

    def close(self):
        if not self.enabled:
            return
        try:
            if self.green: self.green.off()
            if self.red:   self.red.off()
        except Exception:
            pass
        if hasattr(self, "GPIO"):
            try:
                self.GPIO.cleanup()
            except Exception:
                pass

class FixtureLedBank:
    MAX_LEDS = FIXTURE_LIMIT

    def __init__(self, enabled, pins, active_low):
        self.enabled = bool(enabled)
        self.active_low = active_low
        self.leds = []
        self.GPIO = None
        self.pins = []
        if not self.enabled:
            return

        normalized = normalize_fixture_led_pins(pins)

        if not normalized:
            self.enabled = False
            return

        self.pins = normalized
        try:
            from gpiozero import LED
            self.leds = [LED(pin, active_high=not active_low) for pin in self.pins]
        except Exception:
            self.leds = []
            try:
                import RPi.GPIO as GPIO
                self.GPIO = GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                for pin in self.pins:
                    initial = GPIO.LOW if not active_low else GPIO.HIGH
                    GPIO.setup(pin, GPIO.OUT, initial=initial)
            except Exception:
                self.enabled = False

    def _write(self, pin, on):
        if not self.GPIO:
            return
        if self.active_low:
            level = self.GPIO.HIGH if on else self.GPIO.LOW
        else:
            level = self.GPIO.LOW if on else self.GPIO.HIGH
        self.GPIO.output(pin, level)

    def set_states(self, states):
        if not self.enabled:
            return
        states = list(states or [])
        # pad with False to ensure remaining LEDs are turned off
        while len(states) < len(self.pins):
            states.append(False)

        if self.leds:
            for led, on in zip(self.leds, states):
                try:
                    (led.on() if on else led.off())
                except Exception:
                    pass
        elif self.GPIO:
            for pin, on in zip(self.pins, states):
                self._write(pin, bool(on))

    def off(self):
        self.set_states([])

    def close(self):
        if not self.enabled:
            return
        try:
            if self.leds:
                for led in self.leds:
                    led.off()
        except Exception:
            pass
        if self.GPIO:
            try:
                for pin in self.pins:
                    self._write(pin, False)
                self.GPIO.cleanup()
            except Exception:
                pass

# global LED manager
led_gpio = None
fixture_leds = None

def update_fixture_leds():
    global fixture_leds
    if not fixture_leds or not fixture_leds.enabled:
        return
    fixtures = settings.get("fixtures", [])
    pin_count = len(getattr(fixture_leds, "pins", []))
    limit = pin_count if pin_count > 0 else fixture_leds.MAX_LEDS
    states = [False] * limit
    for fx in fixtures:
        try:
            slot = int(str(fx.get("status_led", 0)))
        except Exception:
            continue
        if 1 <= slot <= limit and bool(fx.get("enabled", False)):
            states[slot-1] = True
    fixture_leds.set_states(states)

def update_leds():
    try:
        if led_gpio:
            led_gpio.set(active=status["active"], error=status["error"])
        update_fixture_leds()
    except Exception:
        pass

# ---------- sACN debug logging ----------

_debug_last = {
    "ts": {},       # uni -> last log unix time
    "frame": {}     # uni -> last 512 list (for change detection)
    }

def _summarize_frame(data, first_n=12):
    nonzero = [(i+1, v) for i, v in enumerate(data) if v]
    count = len(nonzero)
    if count == 0:
        return "0 nonzero"
    head = ", ".join(f"{ch}:{val}" for ch, val in nonzero[:first_n])
    if count > first_n:
        head += f", … (+{count-first_n} more)"
    return f"{count} nonzero → {head}"

def _render_nonzero(data, limit=0):
    pairs = [f"{i+1}:{v}" for i, v in enumerate(data) if v]
    if limit and limit > 0 and len(pairs) > limit:
        head = ", ".join(pairs[:limit])
        return f"{len(pairs)} nonzero → {head}, … (+{len(pairs)-limit} more)"
    return ", ".join(pairs) if pairs else "0 nonzero"

def _render_full(data):
    return ",".join(str(int(v)) for v in data)

def _maybe_log_sacn(uni, data):
    if not settings.get("debug_log_sacn", False):
        return
    try:
        interval = max(0, int(settings.get("debug_log_interval_ms", 500))) / 1000.0
    except Exception:
        interval = 0.5
    only_changes = settings.get("debug_log_only_changes", True)
    mode = str(settings.get("debug_log_mode", "summary")).strip().lower()
    nz_limit = int(settings.get("debug_log_nonzero_limit", 64))

    now = time.time()
    last_ts = _debug_last["ts"].get(uni, 0)
    if (now - last_ts) < interval:
        return

    if only_changes:
        prev = _debug_last["frame"].get(uni)
        if prev is not None and prev == data:
            _debug_last["ts"][uni] = now
            return

    _debug_last["frame"][uni] = data[:] if data else []
    _debug_last["ts"][uni] = now

    if mode == "full":
        payload = _render_full(data)
        log(f"sACN[{uni}] full → {payload}")
    elif mode == "nonzero":
        payload = _render_nonzero(data, nz_limit)
        log(f"sACN[{uni}] {payload}")
    else:
        payload = _summarize_frame(data, first_n=12)
        log(f"sACN[{uni}] {payload}")

# ---------- Multi-universe / fixtures helpers ----------

def _blank_frame(): return [0]*512

def _resolve_fixture_channel(fx, channel):
    try:
        ch = int(channel)
    except Exception:
        return 0
    if ch <= 0:
        return 0

    try:
        start = int(fx.get("start_addr", 0))
    except Exception:
        start = 0

    if start > 0:
        relative_limit = max(0, 512 - start + 1)
        if ch <= relative_limit:
            ch = start + ch - 1

    if ch < 1 or ch > 512:
        return 0
    return ch

def _coerce_priority(value, fallback=None):
    try:
        prio = int(value)
    except Exception:
        prio = None
    if prio is None and fallback is not None:
        try:
            prio = int(fallback)
        except Exception:
            prio = None
    if prio is None:
        prio = DEFAULTS.get("priority", 150)
    return max(0, min(200, prio))


def _ensure_output(sender, uni, priority, mirrors=None):
    targets = []
    if sender:
        targets.append(sender)
    if mirrors:
        targets.extend(mirrors)
    if not targets:
        return
    prio = _coerce_priority(priority)
    for s in targets:
        try:
            s.activate_output(uni)
            s[uni].priority = prio
            s[uni].multicast = True
        except Exception:
            continue

# When per-address priority is enabled, unpatched channels should still hold the
# maximum priority expected by external fixtures. The spec for this deployment
# treats priority "10" as the highest value, so we default idle channels to 10
# instead of 1 to avoid other fixtures dropping to zero.
DEFAULT_PER_CHANNEL_FLOOR = 10  # PAP fallback for channels we do not control


def _apply_inv_bias(val16, invert, bias):
    v = 65535 - val16 if invert else val16
    v = v + int(bias)
    return max(0, min(65535, v))


def _reverse_inv_bias(val16, invert, bias):
    try:
        bias = int(bias)
    except Exception:
        bias = 0
    v = max(0, min(65535, int(val16)))
    v = v - bias
    v = max(0, min(65535, v))
    return 65535 - v if invert else v


class SacnMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._frames = {}
        self._receiver = None
        self._universes = set()
        self._failed = False

    def _ensure_receiver(self):
        if self._receiver or self._failed:
            return
        try:
            self._receiver = sacn.sACNreceiver()
            self._receiver.start()
        except Exception as exc:
            log(f"DMX monitor unavailable: {exc}")
            self._receiver = None
            self._failed = True

    def ensure_universe(self, universe):
        try:
            universe = int(universe)
        except Exception:
            return
        if universe <= 0:
            return
        self._ensure_receiver()
        if not self._receiver or universe in self._universes:
            return
        self._universes.add(universe)
        try:
            try:
                self._receiver.join_multicast(universe)
            except Exception:
                pass

            @self._receiver.listen_on("universe", universe=universe)
            def _on_packet(packet, _uni=universe):
                data = list(packet.dmxData[:512]) if getattr(packet, "dmxData", None) is not None else []
                if len(data) < 512:
                    data = data + [0] * (512 - len(data))
                with self._lock:
                    self._frames[_uni] = data
        except Exception as exc:
            log(f"Failed to monitor universe {universe}: {exc}")

    def update_from_settings(self):
        for uni in gather_fixtures_universes():
            self.ensure_universe(uni)

    def get_value(self, universe, channel):
        try:
            channel = int(channel)
        except Exception:
            return None
        if channel <= 0:
            return None
        with self._lock:
            frame = self._frames.get(int(universe))
            if not frame:
                return None
            if channel > len(frame):
                return None
            return frame[channel - 1]


dmx_monitor = SacnMonitor()


def gather_fixtures_universes():
    universes = set()
    default_uni = settings.get("default_universe", settings.get("universe", 1))
    try:
        universes.add(int(default_uni))
    except Exception:
        pass
    use_multi = settings.get("multi_universe_enabled", False)
    for fx in settings.get("fixtures", []) or []:
        try:
            uni = int(fx.get("universe", default_uni))
        except Exception:
            uni = default_uni
        if not use_multi:
            uni = default_uni
        try:
            universes.add(int(uni))
        except Exception:
            continue
    return universes


def capture_initial_fixture_state():
    try:
        dmx_monitor.update_from_settings()
    except Exception:
        return {}

    use_multi = settings.get("multi_universe_enabled", False)
    default_uni = settings.get("default_universe", settings.get("universe", 1))

    fixtures = [fx for fx in settings.get("fixtures", []) if fx.get("enabled", False)]
    candidates = fixtures if fixtures else [None]

    result = {}

    def capture_for_fixture(fx):
        target = {}
        if fx is None:
            fx = {
                "universe": default_uni,
                "pan_coarse": settings.get("ch_pan_coarse", 0),
                "pan_fine": settings.get("ch_pan_fine", 0),
                "tilt_coarse": settings.get("ch_tilt_coarse", 0),
                "tilt_fine": settings.get("ch_tilt_fine", 0),
                "dimmer": settings.get("ch_dimmer", 0),
                "zoom": settings.get("ch_zoom", 0),
                "zoom_fine": settings.get("ch_zoom_fine", 0),
                "invert_pan": settings.get("invert_pan", False),
                "invert_tilt": settings.get("invert_tilt", False),
                "pan_bias": settings.get("pan_bias", 0),
                "tilt_bias": settings.get("tilt_bias", 0),
            }
        uni = fx.get("universe", default_uni)
        if not use_multi:
            uni = default_uni
        dmx_monitor.ensure_universe(uni)

        pc = _resolve_fixture_channel(fx, fx.get("pan_coarse", 0))
        pf = _resolve_fixture_channel(fx, fx.get("pan_fine", 0))
        tc = _resolve_fixture_channel(fx, fx.get("tilt_coarse", 0))
        tf = _resolve_fixture_channel(fx, fx.get("tilt_fine", 0))
        dc = _resolve_fixture_channel(fx, fx.get("dimmer", 0))
        zc = _resolve_fixture_channel(fx, fx.get("zoom", 0))
        zf = _resolve_fixture_channel(fx, fx.get("zoom_fine", 0))

        pan_val = None
        tilt_val = None
        if pc:
            coarse = dmx_monitor.get_value(uni, pc)
            fine = dmx_monitor.get_value(uni, pf) if pf else 0
            if coarse is not None:
                pan_raw = ((coarse or 0) << 8) | (fine or 0)
                pan_val = _reverse_inv_bias(pan_raw, fx.get("invert_pan", False), fx.get("pan_bias", 0))
                pan_val = max(settings["pan_min"], min(settings["pan_max"], pan_val))
        if tc:
            coarse = dmx_monitor.get_value(uni, tc)
            fine = dmx_monitor.get_value(uni, tf) if tf else 0
            if coarse is not None:
                tilt_raw = ((coarse or 0) << 8) | (fine or 0)
                tilt_val = _reverse_inv_bias(tilt_raw, fx.get("invert_tilt", False), fx.get("tilt_bias", 0))
                tilt_val = max(settings["tilt_min"], min(settings["tilt_max"], tilt_val))
        if pan_val is not None:
            target["pan"] = pan_val
        if tilt_val is not None:
            target["tilt"] = tilt_val

        dimmer_val = dmx_monitor.get_value(uni, dc) if dc else None
        if dimmer_val is not None:
            target["dimmer"] = dimmer_val

        if zc:
            z_coarse = dmx_monitor.get_value(uni, zc)
            z_fine = dmx_monitor.get_value(uni, zf) if zf else 0
            if z_coarse is not None:
                if zf:
                    target["zoom"] = ((z_coarse or 0) << 8) | (z_fine or 0)
                else:
                    target["zoom"] = z_coarse
        return target

    for fx in candidates:
        captured = capture_for_fixture(fx)
        for key, value in captured.items():
            if key not in result and value is not None:
                result[key] = value
        if {"pan", "tilt", "dimmer", "zoom"}.issubset(result.keys()):
            break

    return result


def send_frames_for_fixtures(sender, pan16, tilt16, dimmer8, zoom_val, mirrors=None):
    frames = {}  # uni -> [512]
    pap_enabled = settings.get("per_address_priority_enabled", True)
    per_address_priority = {} if pap_enabled else None  # uni -> [512]
    use_multi = settings.get("multi_universe_enabled", False)
    default_uni = settings.get("default_universe", settings.get("universe", 1))
    priority = _coerce_priority(settings.get("priority"), DEFAULTS.get("priority", 150))
    frame_priority = DEFAULT_PER_CHANNEL_FLOOR if pap_enabled else priority

    def get_frame(uni):
        if uni not in frames:
            frames[uni] = _blank_frame()
            _ensure_output(sender, uni, frame_priority, mirrors)
        if pap_enabled and uni not in per_address_priority:
            per_address_priority[uni] = [DEFAULT_PER_CHANNEL_FLOOR] * 512
        return frames[uni]

    def mark_priority(uni, ch):
        if not pap_enabled:
            return
        if ch and 1 <= ch <= 512:
            if uni not in per_address_priority:
                per_address_priority[uni] = [DEFAULT_PER_CHANNEL_FLOOR] * 512
            per_address_priority[uni][ch - 1] = priority

    fixtures = settings.get("fixtures", [])
    if not fixtures:
        # fallback to legacy single-fixture fields
        uni = default_uni
        frame = get_frame(uni)
        pc = settings.get("ch_pan_coarse", 0)
        pf = settings.get("ch_pan_fine", 0)
        tc = settings.get("ch_tilt_coarse", 0)
        tf = settings.get("ch_tilt_fine", 0)
        dc = settings.get("ch_dimmer", 0)
        zc = settings.get("ch_zoom", 0)
        zf = settings.get("ch_zoom_fine", 0)

        pcv, pfv = to16(pan16)
        tcv, tfv = to16(tilt16)
        if pc>0:
            frame[pc-1] = pcv
            mark_priority(uni, pc)
        if pf>0:
            frame[pf-1] = pfv
            mark_priority(uni, pf)
        if tc>0:
            frame[tc-1] = tcv
            mark_priority(uni, tc)
        if tf>0:
            frame[tf-1] = tfv
            mark_priority(uni, tf)
        if dc>0:
            frame[dc-1] = clamp8(dimmer8)
            mark_priority(uni, dc)
        if zc>0:
            if zf>0:
                zc8, zf8 = to16(max(0, min(65535, int(zoom_val))))
                frame[zc-1] = zc8
                frame[zf-1] = zf8
                mark_priority(uni, zc)
                mark_priority(uni, zf)
            else:
                frame[zc-1] = clamp8(zoom_val)
                mark_priority(uni, zc)
        ch_temp = settings.get("ch_color_temp", 0)
        if ch_temp and ch_temp > 0:
            frame[ch_temp-1] = clamp8(settings.get("color_temp_value", 0))
            mark_priority(uni, ch_temp)
    else:
        for fx in fixtures:
            if not fx.get("enabled", False):
                continue
            uni = fx.get("universe", default_uni)
            if not use_multi:
                uni = default_uni
            frame = get_frame(uni)

            p16 = _apply_inv_bias(pan16,  fx.get("invert_pan", False),  fx.get("pan_bias", 0))
            t16 = _apply_inv_bias(tilt16, fx.get("invert_tilt", False), fx.get("tilt_bias", 0))

            # global soft limits
            p16 = max(settings["pan_min"],  min(settings["pan_max"],  p16))
            t16 = max(settings["tilt_min"], min(settings["tilt_max"], t16))

            # Pan/Tilt
            pc = _resolve_fixture_channel(fx, fx.get("pan_coarse", 0))
            pf = _resolve_fixture_channel(fx, fx.get("pan_fine", 0))
            tc = _resolve_fixture_channel(fx, fx.get("tilt_coarse", 0))
            tf = _resolve_fixture_channel(fx, fx.get("tilt_fine", 0))
            pC, pF = to16(p16)
            tC, tF = to16(t16)
            if pc>0:
                frame[pc-1] = pC
                mark_priority(uni, pc)
            if pf>0:
                frame[pf-1] = pF
                mark_priority(uni, pf)
            if tc>0:
                frame[tc-1] = tC
                mark_priority(uni, tc)
            if tf>0:
                frame[tf-1] = tF
                mark_priority(uni, tf)

            # Dimmer
            dC = _resolve_fixture_channel(fx, fx.get("dimmer", 0))
            if dC>0:
                frame[dC-1] = clamp8(dimmer8)
                mark_priority(uni, dC)

            # Zoom
            zC = _resolve_fixture_channel(fx, fx.get("zoom", 0))
            zF = _resolve_fixture_channel(fx, fx.get("zoom_fine", 0))
            if zC>0:
                if zF>0:
                    zC8, zF8 = to16(max(0, min(65535, int(zoom_val))))
                    frame[zC-1] = zC8
                    frame[zF-1] = zF8
                    mark_priority(uni, zC)
                    mark_priority(uni, zF)
                else:
                    frame[zC-1] = clamp8(zoom_val)
                    mark_priority(uni, zC)

            ch_temp = _resolve_fixture_channel(fx, fx.get("color_temp_channel", 0))
            if ch_temp and ch_temp > 0:
                frame[ch_temp-1] = clamp8(fx.get("color_temp_value", 0))
                mark_priority(uni, ch_temp)

    targets = []
    if sender:
        targets.append(sender)
    if mirrors:
        targets.extend(mirrors)

    # push per-universe + debug
    for uni, data in frames.items():
        _ensure_output(sender, uni, frame_priority, mirrors)
        if pap_enabled:
            pap = per_address_priority.get(uni)
            if pap is None:
                pap = [DEFAULT_PER_CHANNEL_FLOOR] * 512
            for s in targets:
                try:
                    s[uni].per_channel_priority = pap
                except Exception:
                    pass
        else:
            for s in targets:
                try:
                    s[uni].per_channel_priority = None
                except Exception:
                    pass
        for s in targets:
            try:
                s[uni].dmx_data = data
            except Exception:
                continue
        _maybe_log_sacn(uni, data)

    return set(frames.keys())

# ---------------- Normalization ----------------

def normalize_types(d):
    out = {}
    for k, default in DEFAULTS.items():
        if k not in d:
            out[k] = settings.get(k, default)
            continue
        v = d[k]
        if isinstance(default, bool):
            out[k] = str(v).lower() in ("1","true","yes","on")
        elif isinstance(default, int):
            try: out[k] = int(str(v))
            except: out[k] = settings.get(k, default)
        elif isinstance(default, float):
            try: out[k] = float(str(v))
            except: out[k] = settings.get(k, default)
        elif isinstance(default, list):
            # allow JSON text from the web form
            try:
                out[k] = json.loads(v) if isinstance(v, str) else list(v)
            except Exception:
                if isinstance(v, str):
                    parts = [p.strip() for p in v.split(',') if p.strip()]
                    if parts and all(p.lstrip("-+").isdigit() for p in parts):
                        out[k] = [int(p) for p in parts]
                    else:
                        out[k] = settings.get(k, default)
                else:
                    out[k] = settings.get(k, default)
        else:
            out[k] = str(v)
    return out

def normalize_fixture(fx):
    out = {}
    out["id"] = str(fx.get("id","")).strip()
    out["enabled"] = str(fx.get("enabled","True")).lower() in ("1","true","yes","on")
    for k in (
        "universe","start_addr",
        "pan_coarse","pan_fine","tilt_coarse","tilt_fine",
        "dimmer","zoom","zoom_fine",
        "color_temp_channel","color_temp_value",
        "pan_bias","tilt_bias"
    ):
        try: out[k] = int(str(fx.get(k, 0)))
        except: out[k] = 0
    out["invert_pan"]  = str(fx.get("invert_pan","False")).lower() in ("1","true","yes","on")
    out["invert_tilt"] = str(fx.get("invert_tilt","False")).lower() in ("1","true","yes","on")
    try:
        slot = int(str(fx.get("status_led", 0)))
    except Exception:
        slot = 0
    if slot < 1 or slot > FIXTURE_LIMIT:
        slot = 0
    out["status_led"] = slot
    return out

# ---------------- Sender Thread ----------------

class SenderThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.sender = None
        self.mirror_senders = []
        self.js = None

        self.pan_pos = 0
        self.tilt_pos = 0
        self.dimmer = 0
        self.zoom_val = 0
        self._btn_prev = {}   # index -> 0/1 for edge detection
        self._debug_prev_buttons = None
        self._debug_prev_axes = None
        self._active_universes = set()
        self._has_streamed = False
        self._last_stream_universes = set()

    def start_sender(self):
        if self.sender:
            try:
                self.sender.stop()
            except Exception:
                pass
        if self.mirror_senders:
            for extra in self.mirror_senders:
                try:
                    extra.stop()
                except Exception:
                    pass
            self.mirror_senders = []
        self.sender = sacn.sACNsender()
        bind_addrs = get_sacn_bind_addresses()
        used_addrs = []

        start_attempts = []
        if bind_addrs:
            start_attempts.append({"bind_addresses": bind_addrs})
            start_attempts.append({"bind_address": bind_addrs[0]})
        start_attempts.append({})

        start_kwargs = None
        for attempt in start_attempts:
            try:
                if attempt:
                    self.sender.start(**attempt)
                else:
                    self.sender.start()
                start_kwargs = attempt
                break
            except TypeError:
                continue
            except Exception as e:
                log(f"Sender start error: {e}")
                raise

        if start_kwargs is None:
            # Final safety net; shouldn't normally execute but keeps the sender usable
            self.sender.start()
            start_kwargs = {}

        configured = False
        if bind_addrs:
            if start_kwargs.get("bind_addresses"):
                used_addrs = list(bind_addrs)
                configured = True
            elif start_kwargs.get("bind_address"):
                used_addrs = [start_kwargs["bind_address"]]
                configured = True
            if not configured:
                configured, used_addrs = _apply_sender_bind_addresses(self.sender, bind_addrs)
            if not configured:
                if start_kwargs.get("bind_addresses"):
                    used_addrs = list(bind_addrs)
                elif start_kwargs.get("bind_address"):
                    used_addrs = [start_kwargs["bind_address"]]
                else:
                    used_addrs = bind_addrs[:1]
        extra_failures = []
        if bind_addrs and len(bind_addrs) > 1:
            missing = [addr for addr in bind_addrs if addr not in used_addrs]
            for addr in missing:
                extra, extra_used = self._create_mirror_sender(addr)
                if not extra:
                    extra_failures.append(addr)
                    continue
                self.mirror_senders.append(extra)
                for value in extra_used:
                    if value not in used_addrs:
                        used_addrs.append(value)
        if bind_addrs and len(bind_addrs) > 1 and len(used_addrs) <= 1:
            log("sACN library does not support multiple bind addresses; using first selection")
        if extra_failures:
            log(f"Failed to activate additional sACN sockets for: {', '.join(extra_failures)}")
        self._active_universes.clear()
        self._last_stream_universes.clear()
        self._has_streamed = False
        if bind_addrs:
            if used_addrs:
                log(f"Activated sACN (priority {settings['priority']}) via {', '.join(used_addrs)}")
            else:
                log(f"Activated sACN (priority {settings['priority']}) (default routing)")
        else:
            log(f"Activated sACN (priority {settings['priority']})")

    def _create_mirror_sender(self, addr):
        addr = str(addr or "").strip()
        if not addr:
            return None, []

        extra = sacn.sACNsender()
        attempts = [
            {"bind_addresses": [addr]},
            {"bind_address": addr},
            {},
        ]

        start_kwargs = None
        for attempt in attempts:
            try:
                if attempt:
                    extra.start(**attempt)
                else:
                    extra.start()
                start_kwargs = attempt
                break
            except TypeError:
                continue
            except Exception as exc:
                log(f"Additional sender start error on {addr}: {exc}")
                return None, []

        if start_kwargs is None:
            try:
                extra.start()
            except Exception as exc:
                log(f"Additional sender start error on {addr}: {exc}")
                return None, []
            start_kwargs = {}

        configured, used = _apply_sender_bind_addresses(extra, [addr])
        if not configured:
            if start_kwargs.get("bind_addresses"):
                used = [addr]
            elif start_kwargs.get("bind_address"):
                used = [start_kwargs["bind_address"]]
            else:
                used = [addr]
        return extra, used

    def stop_sender(self, terminate=True):
        if self.sender or self.mirror_senders:
            try:
                if terminate:
                    try:
                        if not self._has_streamed:
                            targets = set()
                        else:
                            targets = set(self._active_universes) or set(self._last_stream_universes)
                        pap_enabled = settings.get("per_address_priority_enabled", True)
                        all_senders = [s for s in [self.sender] + list(self.mirror_senders) if s]
                        for uni in targets:
                            _ensure_output(self.sender, uni, DEFAULT_PER_CHANNEL_FLOOR, self.mirror_senders)
                            for sender in all_senders:
                                if not sender:
                                    continue
                                if pap_enabled:
                                    try:
                                        sender[uni].per_channel_priority = [DEFAULT_PER_CHANNEL_FLOOR]*512
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        sender[uni].per_channel_priority = None
                                    except Exception:
                                        pass
                                try:
                                    sender[uni].dmx_data = [0]*512
                                except Exception:
                                    pass
                    except Exception:
                        pass
                if self.sender:
                    self.sender.stop()
                for extra in self.mirror_senders:
                    try:
                        extra.stop()
                    except Exception:
                        pass
                log("Stream terminated")
            except Exception as e:
                log(f"Sender stop error: {e}")
        self.sender = None
        self.mirror_senders = []
        self._active_universes.clear()
        self._last_stream_universes.clear()
        self._has_streamed = False

    def init_joystick(self):
        pygame.joystick.quit(); pygame.joystick.init()
        if pygame.joystick.get_count() == 0:
            return None
        j = pygame.joystick.Joystick(0); j.init()
        return j

    def axis(self, idx):
        # Virtual joystick overrides physical when enabled (or when no js)
        if settings.get("virtual_joystick_enabled", False) or not self.js:
            if idx == settings.get("ax_pan", 0):   return virtual_state.get("x", 0.0)
            if idx == settings.get("ax_tilt", 1):  return virtual_state.get("y", 0.0)
            if idx == settings.get("ax_throt", 2): return virtual_state.get("throttle", -1.0)
            if idx == settings.get("ax_zoom", -1): return virtual_state.get("zaxis", 0.0)
            return 0.0
        try:
            return self.js.get_axis(idx) if 0 <= idx < self.js.get_numaxes() else 0.0
        except Exception:
            return 0.0

    def btn(self, idx):
        if settings.get("virtual_joystick_enabled", False) or not self.js:
            return 1 if virtual_state["buttons"].get(int(idx), 0) else 0
        try:
            return self.js.get_button(idx) if 0 <= idx < self.js.get_numbuttons() else 0
        except Exception:
            return 0

    def _collect_button_states(self):
        states = {}
        if settings.get("virtual_joystick_enabled", False) or not self.js:
            for k, v in virtual_state["buttons"].items():
                try:
                    idx = int(k)
                except Exception:
                    continue
                try:
                    states[idx] = 1 if int(v) else 0
                except Exception:
                    states[idx] = 0
            return states
        try:
            count = self.js.get_numbuttons()
        except Exception:
            count = 0
        for idx in range(count):
            try:
                states[idx] = 1 if self.js.get_button(idx) else 0
            except Exception:
                states[idx] = 0
        return states

    def _collect_axis_states(self):
        axes = []
        mapping = [
            ("pan", settings.get("ax_pan", 0)),
            ("tilt", settings.get("ax_tilt", 1)),
            ("throttle", settings.get("ax_throt", 2)),
        ]

        try:
            zoom_idx = int(settings.get("ax_zoom", -1))
        except Exception:
            zoom_idx = -1
        if zoom_idx >= 0:
            mapping.append(("zoom", zoom_idx))

        for label, raw_idx in mapping:
            try:
                idx = int(raw_idx)
            except Exception:
                continue
            if idx < 0:
                continue
            try:
                val = float(self.axis(idx))
            except Exception:
                val = 0.0
            axes.append((label, max(-1.0, min(1.0, val))))
        return axes

    def _maybe_log_button_debug(self):
        if not settings.get("debug_controller_buttons", False):
            self._debug_prev_buttons = None
            self._debug_prev_axes = None
            return

        states = self._collect_button_states()
        axis_states = self._collect_axis_states()
        prev = self._debug_prev_buttons
        prev_axes = self._debug_prev_axes

        def axes_changed(previous, current):
            if previous is None:
                return True
            if set(previous.keys()) != set(current.keys()):
                return True
            for key, value in current.items():
                if key not in previous:
                    return True
                if abs(previous[key] - value) >= 0.01:
                    return True
            return False

        axis_dict = {label: value for label, value in axis_states}

        if prev is not None and states == prev and not axes_changed(prev_axes, axis_dict):
            return

        pressed = [str(i) for i, v in sorted(states.items()) if v]
        message = f"Controller debug: pressed [{', '.join(pressed) if pressed else 'none'}]"

        if prev is None:
            count = len(states)
            if count:
                message += f"; tracking {count} buttons"
            else:
                message += "; no buttons detected"
            if axis_states:
                message += f"; tracking axes: {', '.join(label for label, _ in axis_states)}"
        else:
            changes = []
            keys = sorted(set(states.keys()) | set(prev.keys()))
            for idx in keys:
                cur = states.get(idx, 0)
                before = prev.get(idx, 0)
                if cur != before:
                    changes.append(f"{idx}:{'DOWN' if cur else 'UP'}")
            if changes:
                message += f"; changes: {', '.join(changes)}"

        if axis_states:
            axis_summary = ", ".join(f"{label}={value:+.2f}" for label, value in axis_states)
            message += f"; axes: {axis_summary}"

        log(message)
        self._debug_prev_buttons = dict(states)
        self._debug_prev_axes = dict(axis_dict)

    def run(self):
        pygame.init()
        clock = pygame.time.Clock()

        with state_lock:
            self.pan_pos  = (settings["pan_min"] + settings["pan_max"])//2
            self.tilt_pos = (settings["tilt_min"] + settings["tilt_max"])//2
            self.dimmer   = 0
            self.zoom_val = 0

        last_activate = last_release = 0

        while not self._stop.is_set():
            flush_logs()
            try:
                use_virtual = settings.get("virtual_joystick_enabled", False)

                if not use_virtual and not self.js:
                    self.js = self.init_joystick()
                    if self.js:
                        status["joystick_name"] = self.js.get_name()
                        status["axes"] = self.js.get_numaxes()
                        status["buttons"] = self.js.get_numbuttons()
                        log(f"Joystick: {status['joystick_name']} (axes={status['axes']} buttons={status['buttons']})")
                        status["error"] = False
                        status["error_msg"] = ""
                        update_leds()
                    else:
                        status["joystick_name"] = ""
                        status["axes"] = status["buttons"] = 0
                        if not use_virtual:
                            status["error"] = True
                            status["error_msg"] = "No joystick detected"
                            update_leds()
                            time.sleep(0.5)
                            clock.tick(10)
                            continue

                pygame.event.pump()

                self._maybe_log_button_debug()

                # edges (debounce ~150ms)
                now = time.time()
                # ---- Button actions (rising/falling edges) ----
                actions = settings.get("button_actions", []) or []
                for act in actions:
                    try:
                        bidx = int(act.get("button", -1))
                    except Exception:
                        bidx = -1
                    if bidx < 0:
                        continue

                    cur = 1 if self.btn(bidx) else 0
                    prev = self._btn_prev.get(bidx, 0)
                    self._btn_prev[bidx] = cur

                    mode = str(act.get("mode", "toggle")).lower()  # 'toggle' or 'hold'
                    atype = str(act.get("type", "")).lower()       # 'toggle_fixture','enable_fixture','disable_fixture','toggle_group'
                    targets = act.get("targets", [])
                    if isinstance(targets, str):
                        targets = [targets]

                    # Rising edge
                    if cur == 1 and prev == 0:
                        if mode == "toggle":
                            if atype == "toggle_fixture":
                                for fid in targets:
                                    toggle_fixture_by_id(fid)
                            elif atype == "enable_fixture":
                                for fid in targets:
                                    set_fixture_enabled_by_id(fid, True)
                            elif atype == "disable_fixture":
                                for fid in targets:
                                    set_fixture_enabled_by_id(fid, False)
                            elif atype == "toggle_group":
                                for fid in targets:
                                    toggle_fixture_by_id(fid)
                        elif mode == "hold":
                            if atype in ("toggle_fixture", "enable_fixture", "toggle_group"):
                                for fid in targets:
                                    set_fixture_enabled_by_id(fid, True)
                            elif atype == "disable_fixture":
                                for fid in targets:
                                    set_fixture_enabled_by_id(fid, False)

                    # Falling edge (for momentary)
                    if cur == 0 and prev == 1 and mode == "hold":
                        if atype in ("toggle_fixture", "enable_fixture", "toggle_group"):
                            for fid in targets:
                                set_fixture_enabled_by_id(fid, False)

                if self.btn(settings["btn_activate"]) and now-last_activate>0.15 and not status["active"]:
                    with state_lock:
                        try:
                            captured = capture_initial_fixture_state()
                        except Exception:
                            captured = {}
                        if captured:
                            if "pan" in captured:
                                self.pan_pos = clamp16(captured["pan"])
                            if "tilt" in captured:
                                self.tilt_pos = clamp16(captured["tilt"])
                            if "dimmer" in captured:
                                self.dimmer = clamp8(captured["dimmer"])
                            if "zoom" in captured:
                                self.zoom_val = clamp16(captured["zoom"])
                        self.start_sender()
                        status["active"] = True
                        status["error"] = False
                        status["error_msg"] = ""
                    last_activate = now
                    log("Activate pressed → taking control")
                    update_leds()

                if self.btn(settings["btn_release"]) and now-last_release>0.15 and status["active"]:
                    with state_lock:
                        self.stop_sender(terminate=True)
                        status["active"] = False
                    last_release = now
                    log("Release pressed → stream terminated")
                    update_leds()

                if status["active"] and self.sender:
                    # axes: pan/tilt with expo + deadband
                    x = self.axis(settings["ax_pan"])
                    y = self.axis(settings["ax_tilt"])
                    if settings["invert_pan"]:  x = -x
                    if settings["invert_tilt"]: y = -y
                    x = expo_curve(x, settings["expo"], settings["deadband"])
                    y = expo_curve(y, settings["expo"], settings["deadband"])

                    spd = settings["speed"]
                    if self.btn(settings["btn_fine"]):
                        spd = max(1, spd // settings["fine_divisor"])

                    self.pan_pos  = clamp16(self.pan_pos  + x * spd)
                    self.tilt_pos = clamp16(self.tilt_pos + y * spd)
                    self.pan_pos  = max(settings["pan_min"],  min(settings["pan_max"],  self.pan_pos))
                    self.tilt_pos = max(settings["tilt_min"], min(settings["tilt_max"], self.tilt_pos))

                    # throttle -> DIMMER
                    t = self.axis(settings["ax_throt"])
                    if settings["throttle_invert"]: t = -t
                    t01 = (t+1.0)*0.5  # 0..1
                    if settings.get("ch_dimmer", 0) > 0:
                        self.dimmer = int((1.0 - t01) * 255)  # 8-bit

                    # --- Dedicated rocker zoom (incremental/latching) ---
                    zidx = settings.get("ax_zoom", -1)
                    if zidx is not None and zidx >= 0:
                        z = self.axis(zidx)
                        if settings.get("zoom_invert", False): z = -z
                        z = expo_curve(z, settings.get("zoom_expo", 0.4), settings.get("zoom_deadband", 0.05))
                        zspd = max(1, int(settings.get("zoom_speed", 3000)))
                        self.zoom_val = clamp16(self.zoom_val + z * zspd)
                    else:
                        # Fallback: legacy "hold zoom mod" ONLY if no rocker is configured
                        if self.btn(settings["btn_zoom_mod"]):
                            self.zoom_val = int(t01 * 65535)
                        # else: zoom_val unchanged (latched)

                    # overrides
                    if self.btn(settings["btn_flash10"]):
                        self.dimmer = settings["flash10_level"]
                    if self.btn(settings["btn_dim_off"]):
                        self.dimmer = 0

                    # send to all fixtures/universes
                    prev_universes = set(self._active_universes)
                    new_universes = send_frames_for_fixtures(
                        self.sender,
                        self.pan_pos,
                        self.tilt_pos,
                        self.dimmer,
                        self.zoom_val,
                        self.mirror_senders,
                    )
                    if new_universes:
                        self._has_streamed = True
                        self._last_stream_universes.update(new_universes)
                    pap_enabled = settings.get("per_address_priority_enabled", True)
                    for uni in prev_universes - new_universes:
                        try:
                            _ensure_output(self.sender, uni, DEFAULT_PER_CHANNEL_FLOOR, self.mirror_senders)
                            targets = [s for s in [self.sender] + list(self.mirror_senders) if s]
                            for sender in targets:
                                if pap_enabled:
                                    try:
                                        sender[uni].per_channel_priority = [DEFAULT_PER_CHANNEL_FLOOR]*512
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        sender[uni].per_channel_priority = None
                                    except Exception:
                                        pass
                                try:
                                    sender[uni].dmx_data = [0]*512
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    self._active_universes = new_universes
                    if new_universes:
                        self._last_stream_universes = set(new_universes)
                    status["last_frame_ts"] = time.time()

                clock.tick(settings["fps"])

            except Exception as e:
                status["error"] = True
                status["error_msg"] = f"Loop error: {e}"
                log(status["error_msg"])
                update_leds()
                time.sleep(0.2)

        # cleanup
        try:
            self.stop_sender(terminate=True)
        except Exception:
            pass
        pygame.quit()

    def stop(self):
        self._stop.set()

worker = SenderThread()

# ---------------- Web UI ----------------

UI_DIR = os.path.dirname(__file__)
UI_HTML_PATH = os.path.join(UI_DIR, "ui_main.html")
UI_CSS_PATH = os.path.join(UI_DIR, "ui_main.css")
UI_JS_PATH = os.path.join(UI_DIR, "ui_main.js")

# ---------------- Routes ----------------

@APP.route("/")
def index():
    return send_file(UI_HTML_PATH)


@APP.route("/ui_main.css")
def ui_css():
    return send_file(UI_CSS_PATH)


@APP.route("/ui_main.js")
def ui_js():
    return send_file(UI_JS_PATH)

@APP.route("/api/status")
def api_status():
    flush_logs()
    lf = "-"
    if status["last_frame_ts"]:
        delta = time.time() - status["last_frame_ts"]
        lf = f"{delta:.2f}s ago" if delta < 3600 else "long ago"
    fixtures = settings.get("fixtures", [])
    led_pin_count = len(settings.get("gpio_fixture_led_pins", []))
    led_limit = led_pin_count if led_pin_count > 0 else FIXTURE_LIMIT
    led_slots = [{"ids": [], "on": False} for _ in range(led_limit)]
    for fx in fixtures:
        try:
            slot = int(str(fx.get("status_led", 0)))
        except Exception:
            continue
        if 1 <= slot <= led_limit:
            entry = led_slots[slot-1]
            label = fx.get("id") or f"Fixture {slot}"
            entry.setdefault("ids", []).append(label)
            if fx.get("enabled", False):
                entry["on"] = True
    fixture_leds = []
    for idx, slot in enumerate(led_slots):
        labels = slot.get("ids", [])
        label = ", ".join(labels) if labels else f"Slot {idx+1}"
        fixture_leds.append({
            "label": label,
            "on": bool(slot.get("on", False))
        })
    return jsonify({
        "active": status["active"],
        "error": status["error"],
        "error_msg": status["error_msg"],
        "joystick_name": status["joystick_name"],
        "axes": status["axes"],
        "buttons": status["buttons"],
        "last_frame": lf,
        "virtual": settings.get("virtual_joystick_enabled", False),
        "power_led": bool(status["active"] and not status["error"]),
        "error_led": bool(status["error"]),
        "fixture_leds": fixture_leds
    })

@APP.route("/api/logs")
def api_logs():
    flush_logs()
    return Response("\n".join(log_store[-800:]) + "\n", mimetype="text/plain")

@APP.route("/api/network/adapters", methods=["GET"])
def api_network_adapters():
    return jsonify({
        "adapters": list_network_interfaces(),
        "selected": get_sacn_bind_addresses()
    })


@APP.route("/api/capture/status", methods=["GET"])
def api_capture_status():
    return jsonify(get_packet_capture_status())


def _resolve_capture_interface(payload):
    iface = str(payload.get("interface") or payload.get("name") or "").strip()
    if iface:
        return iface
    address = str(payload.get("address") or "").strip()
    if not address:
        return ""
    for adapter in list_network_interfaces():
        if str(adapter.get("address")) == address:
            return str(adapter.get("name", "")).strip()
    return ""


@APP.route("/api/capture/start", methods=["POST"])
def api_capture_start():
    payload = request.get_json(force=True, silent=True) or {}
    iface = _resolve_capture_interface(payload)
    if not iface:
        return jsonify({"error": "Interface is required"}), 400
    ok, error = start_packet_capture(iface)
    if not ok:
        return jsonify({"error": error}), 400
    return jsonify(get_packet_capture_status())


@APP.route("/api/capture/stop", methods=["POST"])
def api_capture_stop():
    ok, error = stop_packet_capture()
    if not ok and error:
        return jsonify({"error": error}), 400
    return jsonify(get_packet_capture_status())


@APP.route("/api/capture/download", methods=["GET"])
def api_capture_download():
    with capture_lock:
        path = packet_capture.get("path")
        filename = packet_capture.get("filename") or "capture.pcap"
        active = bool(packet_capture.get("active"))
    if not path or not os.path.exists(path):
        return jsonify({"error": "No capture available"}), 404
    if active:
        return jsonify({"error": "Stop the capture before downloading"}), 400
    return send_file(
        path,
        mimetype="application/vnd.tcpdump.pcap",
        as_attachment=True,
        download_name=filename,
        conditional=True,
    )

@APP.route("/api/settings", methods=["GET","POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(settings)
    data = request.get_json(force=True) or {}
    norm = normalize_types(data)
    with state_lock:
        settings.update(norm)
        save_settings()
    log("Settings saved")
    return jsonify({"ok": True, "message": "Settings saved"})

@APP.route("/api/fixtures", methods=["GET"])
def api_fixtures_list():
    return jsonify({
        "multi_universe_enabled": settings.get("multi_universe_enabled", False),
        "default_universe": settings.get("default_universe", 1),
        "fixtures": settings.get("fixtures", [])
    })

@APP.route("/api/fixtures", methods=["POST"])
def api_fixtures_create():
    fx = request.get_json(force=True) or {}
    fx = normalize_fixture(fx)
    if not fx["id"]:
        return jsonify({"error": "Fixture must have a non-empty 'id'"}), 400
    cur = settings.get("fixtures", [])
    if len(cur) >= FIXTURE_LIMIT:
        return jsonify({"error": f"Fixture limit of {FIXTURE_LIMIT} reached"}), 400
    if any(f.get("id")==fx["id"] for f in cur):
        return jsonify({"error": "Duplicate fixture id"}), 400
    with state_lock:
        settings["fixtures"] = clamp_fixtures(cur + [fx])
        save_settings()
    log(f"Fixture added: {fx['id']}")
    return jsonify({"ok": True})

@APP.route("/api/fixtures/<fid>", methods=["PUT","PATCH"])
def api_fixtures_update(fid):
    body = request.get_json(force=True) or {}
    with state_lock:
        arr = settings.get("fixtures", [])
        for i, f in enumerate(arr):
            if f.get("id")==fid:
                merged = f.copy()
                merged.update(normalize_fixture({**f, **body}))
                merged["id"] = fid
                arr[i] = merged
                settings["fixtures"] = clamp_fixtures(arr)
                save_settings()
                log(f"Fixture updated: {fid}")
                return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404

@APP.route("/api/fixtures/<fid>", methods=["DELETE"])
def api_fixtures_delete(fid):
    with state_lock:
        arr = settings.get("fixtures", [])
        new = [f for f in arr if f.get("id")!=fid]
        if len(new)==len(arr):
            return jsonify({"error":"Not found"}), 404
        settings["fixtures"] = clamp_fixtures(new)
        save_settings()
    log(f"Fixture removed: {fid}")
    return jsonify({"ok": True})

@APP.route("/api/fixtures/config", methods=["POST"])
def api_fixtures_config():
    body = request.get_json(force=True) or {}
    changed = False
    with state_lock:
        if "multi_universe_enabled" in body:
            settings["multi_universe_enabled"] = bool(body["multi_universe_enabled"])
            changed = True
        if "default_universe" in body:
            try:
                settings["default_universe"] = int(body["default_universe"])
                changed = True
            except Exception:
                pass
        if changed:
            save_settings()
            log("Fixture config updated")
    return jsonify({"ok": True})

@APP.route("/api/fixtures/export")
def api_fixtures_export():
    csv_txt = fixtures_to_csv(settings.get("fixtures", []))
    return Response(csv_txt, mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=fixtures.csv"})

@APP.route("/api/fixtures/import", methods=["POST"])
def api_fixtures_import():
    csv_txt = request.data.decode("utf-8", errors="ignore")
    try:
        fixtures_raw = csv_to_fixtures(csv_txt)
        if len(fixtures_raw) > FIXTURE_LIMIT:
            return jsonify({"error": f"Fixture limit is {FIXTURE_LIMIT}; received {len(fixtures_raw)}"}), 400
        fixtures = clamp_fixtures(fixtures_raw)
        with state_lock:
            settings["fixtures"] = fixtures
            save_settings()
        log(f"Imported {len(fixtures)} fixtures from CSV")
        return jsonify({"ok": True, "count": len(fixtures)})
    except Exception as e:
        return jsonify({"error": f"CSV parse failed: {e}"}), 400

@APP.route("/api/virtual", methods=["GET", "POST"])
def api_virtual():
    if request.method == "GET":
        return jsonify({
            "enabled": settings.get("virtual_joystick_enabled", False),
            "x": virtual_state["x"],
            "y": virtual_state["y"],
            "throttle": virtual_state["throttle"],
            "zaxis": virtual_state["zaxis"],
            "buttons": virtual_state["buttons"],
        })
    payload = request.get_json(force=True, silent=True) or {}
    with state_lock:
        if "enabled" in payload:
            settings["virtual_joystick_enabled"] = bool(payload["enabled"])
            save_settings()
        if "x" in payload:        virtual_state["x"] = vclamp(payload["x"])
        if "y" in payload:        virtual_state["y"] = vclamp(payload["y"])
        if "throttle" in payload: virtual_state["throttle"] = vclamp(payload["throttle"])
        if "zaxis" in payload:    virtual_state["zaxis"] = vclamp(payload["zaxis"])
        if "buttons" in payload and isinstance(payload["buttons"], dict):
            for k, v in payload["buttons"].items():
                try:
                    i = int(k)
                    virtual_state["buttons"][i] = 1 if int(v) else 0
                except Exception:
                    pass
    return jsonify({"ok": True})

@APP.route("/api/virtual/press", methods=["POST"])
def api_virtual_press():
    payload = request.get_json(force=True, silent=True) or {}
    i = int(payload.get("button", -1))
    if i >= 0:
        virtual_state["buttons"][i] = 1
        return jsonify({"ok": True})
    return jsonify({"error": "Missing button index"}), 400

@APP.route("/api/virtual/release", methods=["POST"])
def api_virtual_release():
    payload = request.get_json(force=True, silent=True) or {}
    i = int(payload.get("button", -1))
    if i >= 0:
        virtual_state["buttons"][i] = 0
        return jsonify({"ok": True})
    return jsonify({"error": "Missing button index"}), 400

@APP.route("/api/activate", methods=["POST"])
def api_activate():
    if not status["active"]:
        with state_lock:
            worker.start_sender()
            status["active"] = True
            status["error"] = False
            status["error_msg"] = ""
        log("Activated via UI")
        update_leds()
    return jsonify({"ok": True})

@APP.route("/api/release", methods=["POST"])
def api_release():
    if status["active"]:
        with state_lock:
            worker.stop_sender(terminate=True)
            status["active"] = False
        log("Released via UI")
        update_leds()
    return jsonify({"ok": True})

@APP.route("/api/restart", methods=["POST"])
def api_restart():
    log("Restart requested via UI")
    with state_lock:
        try:
            worker.stop_sender(terminate=True)
        except Exception:
            pass
        status["active"] = False
        status["error"] = False
        status["error_msg"] = ""
    update_leds()

    def _delayed_restart():
        time.sleep(0.5)
        argv = sys.argv[:]
        try:
            if getattr(sys, "frozen", False):
                executable = sys.executable
                os.execv(executable, [executable] + argv[1:])
            else:
                os.execv(sys.executable, [sys.executable] + argv)
        except Exception as exc:
            try:
                log(f"Restart exec failed ({exc}); spawning new process")
                subprocess.Popen([sys.executable] + argv)
            except Exception as spawn_exc:
                log(f"Restart spawn failed: {spawn_exc}; terminating")
            finally:
                os._exit(0)

    threading.Thread(target=_delayed_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Restarting service..."})

@APP.route("/api/discover")
def api_discover():
    try:
        pygame.event.pump()
        js = worker.js or worker.init_joystick()
        if not js:
            return jsonify({"error": "No joystick found"}), 400
        axes = [round(js.get_axis(i),3) for i in range(js.get_numaxes())]
        btns = [int(js.get_button(i)) for i in range(js.get_numbuttons())]
        return jsonify({"axes": axes, "buttons": btns})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------- Main ----------------

def main():
    global led_gpio, fixture_leds
    load_settings()
    log("FollowSpot server starting…")

    # GPIO LED manager
    led_gpio = LedGPIO(settings.get("gpio_enabled", True),
                       settings.get("gpio_green_pin", 17),
                       settings.get("gpio_red_pin", 27),
                       settings.get("gpio_active_low", False))
    fixture_leds = FixtureLedBank(settings.get("gpio_enabled", True) and settings.get("gpio_fixture_led_pins"),
                                  settings.get("gpio_fixture_led_pins", []),
                                  settings.get("gpio_active_low", False))
    update_leds()

    worker.start()
    # Run Flask (port 80; change to 8080 if preferred)
    APP.run(host="0.0.0.0", port=8080, threaded=True)

if __name__ == "__main__":
    main()
