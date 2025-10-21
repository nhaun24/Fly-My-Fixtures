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

def _ensure_output(sender, uni, priority, mirrors=None):
    targets = []
    if sender:
        targets.append(sender)
    if mirrors:
        targets.extend(mirrors)
    if not targets:
        return
    for s in targets:
        try:
            s.activate_output(uni)
            s[uni].priority = priority
            s[uni].multicast = True
        except Exception:
            continue

DEFAULT_PER_CHANNEL_FLOOR = 1  # PAP fallback for channels we do not control


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
    priority = settings.get("priority", 150)

    def get_frame(uni):
        if uni not in frames:
            frames[uni] = _blank_frame()
            _ensure_output(sender, uni, priority, mirrors)
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
        _ensure_output(sender, uni, priority, mirrors)
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
                        targets = set(self._active_universes)
                        if not targets:
                            targets.add(settings.get("default_universe", 1))
                        pap_enabled = settings.get("per_address_priority_enabled", True)
                        all_senders = [s for s in [self.sender] + list(self.mirror_senders) if s]
                        for uni in targets:
                            _ensure_output(self.sender, uni, settings.get("priority", 150), self.mirror_senders)
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
                    pap_enabled = settings.get("per_address_priority_enabled", True)
                    for uni in prev_universes - new_universes:
                        try:
                            _ensure_output(self.sender, uni, settings.get("priority", 150), self.mirror_senders)
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

INDEX_HTML = """
    <!doctype html>
    <html>
    <head>
    <meta charset="utf-8">
    <title>Fly My Fixtures</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
     :root{color-scheme:dark}
     *,*::before,*::after{box-sizing:border-box}
     body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 12px;background:#0f1115;color:#e5e7eb;line-height:1.45}
     h1{margin-bottom:6px}
     h3{margin:0 0 12px}
     h4{margin:8px 0 12px}
     .panel-grid{display:flex;gap:16px;flex-wrap:wrap}
     .card{flex:1 1 320px;border:1px solid #2f3541;border-radius:14px;padding:20px;background:#111827;box-shadow:0 4px 16px rgba(8,10,20,.35)}
     .card.wide{flex-basis:100%}
     label{display:block;font-size:14px;font-weight:600;color:#f3f4f6}
     .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px;align-items:start}
     .grid > div{display:flex;flex-direction:column;gap:6px;padding:14px;border-radius:12px;background:#0f141d;border:1px solid #2b3140}
     .grid > .cbrow{flex-direction:row;align-items:center;gap:10px;margin-top:0;padding:14px}
     .grid > .cbrow label{margin:0}
     .checklist{display:flex;flex-direction:column;gap:10px;margin-top:6px}
     .checklist-option{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:10px;background:#101826;border:1px solid #2b3140}
     .checklist-option input{margin-top:4px}
     .checklist-text{display:flex;flex-direction:column;gap:4px}
     .checklist-title{font-weight:600;font-size:14px;color:#f3f4f6}
     input[type=number],input[type=text],select{width:100%;padding:10px 12px;border:1px solid #303845;border-radius:10px;background:#1a1d25;color:#e5e7eb;font-size:14px;transition:border-color .15s ease,box-shadow .15s ease}
     input[type=number]:focus,input[type=text]:focus,select:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.25)}
     input[type=checkbox]{width:auto;height:auto;accent-color:#2563eb}
     input[type=range]{width:100%}
     textarea{width:100%;min-height:220px;border:1px solid #303845;border-radius:12px;padding:12px;background:#0b1020;color:#dfe7ff;font-family:ui-monospace,Consolas,monospace;font-size:14px}
     textarea:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.25)}
     .cbrow{display:flex;gap:8px;align-items:center;margin-top:6px;font-weight:500}
     .btn{padding:10px 16px;border-radius:10px;border:1px solid #4b5563;background:#1f2937;color:#e5e7eb;cursor:pointer;font-weight:600;transition:background .15s ease,border-color .15s ease,transform .1s ease}
     .btn:hover{background:#273244}
     .btn:active{transform:scale(.98)}
     .btn.primary{background:#2563eb;border-color:#2563eb}
     .btn.primary:hover{background:#1e4fc7}
     .btn.danger{background:#dc2626;border-color:#dc2626}
     .btn.danger:hover{background:#b91c1c}
     .tabbar{display:flex;gap:8px;flex-wrap:wrap;margin:24px 0 20px}
     .tab-btn{padding:10px 18px;border-radius:999px;border:1px solid #2f3541;background:#111827;color:#e5e7eb;font-weight:600;cursor:pointer;transition:background .15s ease,border-color .15s ease,box-shadow .15s ease}
     .tab-btn.active{background:#2563eb;border-color:#2563eb;box-shadow:0 6px 18px rgba(37,99,235,.35)}
     .tab-panel{display:none}
     .tab-panel.active{display:block}
     .switch{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0}
     .form-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}
     .form-actions .btn{min-width:120px;justify-content:center;text-align:center}
     .pill{width:12px;height:12px;border-radius:999px;background:#1f2937;border:1px solid #374151}
     .pill.ok{background:#22c55e;border-color:#16a34a}
     .pill.err{background:#ef4444;border-color:#b91c1c}
     .pill.off{background:#374151;border-color:#4b5563}
     .badge{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600}
     .ok{background:#065f46;color:#d1fae5}
     .err{background:#991b1b;color:#fee2e2}
     .warn{background:#92400e;color:#fef3c7}
     small{color:#9ca3af}
     .muted{color:#9ca3af}
     .small{font-size:12px}
     .led-bank{display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start;margin-top:12px}
     .led-bank.split{flex-direction:column;gap:16px}
     .led-row{display:flex;gap:18px;flex-wrap:wrap;align-items:center}
     .led{display:flex;flex-direction:column;align-items:center;gap:6px;min-width:70px}
     .led-label{font-size:12px;color:#9ca3af;text-align:center;word-break:break-word}
     .led-bulb{width:28px;height:28px;border-radius:50%;position:relative;box-shadow:inset 0 0 6px rgba(0,0,0,.65);background:#1f2937;transition:box-shadow .2s ease,opacity .2s ease}
     .led-bulb::after{content:"";position:absolute;inset:3px;border-radius:50%;background:radial-gradient(circle at 30% 30%,rgba(255,255,255,.35),rgba(255,255,255,0));opacity:0;transition:opacity .2s ease}
     .led-bulb.green{background:#064e3b}
     .led-bulb.red{background:#7f1d1d}
     .led-bulb.on{opacity:1;box-shadow:0 0 14px rgba(96,165,250,.35),inset 0 0 6px rgba(0,0,0,.65)}
     .led-bulb.green.on{background:#16a34a;box-shadow:0 0 16px rgba(16,185,129,.55),inset 0 0 6px rgba(0,0,0,.5)}
     .led-bulb.red.on{background:#dc2626;box-shadow:0 0 16px rgba(220,38,38,.6),inset 0 0 6px rgba(0,0,0,.5)}
     .led-bulb.on::after{opacity:1}
     details.section{border:1px solid #2f3541;border-radius:12px;margin-bottom:12px;overflow:hidden;background:#0f141d}
     details.section > summary{cursor:pointer;padding:12px 14px;background:#101826;font-weight:600;border-bottom:1px solid #2f3541;list-style:none}
     details.section[open] > summary{background:#142033}
     details.section > .inner{padding:14px}
     .fxgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:18px;align-items:start}
     .fxgrid > div{display:flex;flex-direction:column;gap:6px;padding:14px;border-radius:12px;background:#0f141d;border:1px solid #2b3140}
     .fxgrid > .cbrow{flex-direction:row;align-items:center;gap:10px;margin-top:0;padding:14px}
     .fxgrid > .cbrow label{margin:0}
     .fixture-card{border:1px solid #2f3541;border-radius:12px;padding:16px;background:#101826;display:flex;flex-direction:column;gap:8px}
     .fixture-card details{background:#0f141d;border-radius:10px;border:1px solid #2b3140;padding:0}
     .fixture-card details > summary{padding:10px 14px;cursor:pointer;font-weight:600}
     .fixture-card details[open] > summary{background:#142033}
     .fixture-card details > .fxgrid{padding:16px}
     .fixture-card details > .form-actions{padding:0 16px 16px}
     #fixture-list{display:flex;flex-direction:column;gap:12px;margin-top:12px}
     .subcard{border:1px solid #2b3140;border-radius:12px;padding:18px;background:#0f141d;margin-top:20px}
     #import-area{margin-top:16px}
     #logs{min-height:240px}
     .virtual-layout{display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start;margin-top:16px}
     .virtual-pad{flex:0 0 auto}
     .virtual-controls{flex:1 1 240px;display:flex;flex-direction:column;gap:12px}
     /* XY pad */
     #pad{user-select:none;-webkit-user-select:none;touch-action:none;cursor:crosshair}
    </style>
    </head>
    <body>
      <h1>Fly My Fixtures</h1>
      <p>
        <span class="switch"><span>Status:</span><span id="status-pill" class="pill off"></span><small id="status-text">Idle</small></span>
        <span class="switch"><span>Health:</span><span id="health-pill" class="pill off"></span><small id="health-text">Unknown</small></span>
      </p>

      <div class="tabbar" role="tablist">
        <button type="button" class="tab-btn active" data-tab="dashboard">Dashboard</button>
        <button type="button" class="tab-btn" data-tab="settings">Settings</button>
      </div>

      <section class="tab-panel active" data-tab="dashboard">
        <div class="panel-grid">
          <div class="card wide">
            <h3>Controls</h3>
            <div class="switch">
              <button class="btn primary" onclick="activate()">Activate</button>
              <button class="btn danger" onclick="release()">Release</button>
            </div>
            <p class="small muted">Joystick: <span id="joy-name">-</span> • Axes: <span id="joy-axes">0</span> • Buttons: <span id="joy-buttons">0</span></p>
            <p class="small muted">Last frame: <span id="last-frame">-</span></p>
          </div>

          <div class="card wide">
            <h3>Virtual LEDs</h3>
            <div class="led-bank split">
              <div class="led-row">
                <div class="led">
                  <div class="led-bulb green" id="led-power"></div>
                  <span class="led-label">Power</span>
                </div>
                <div class="led">
                  <div class="led-bulb red" id="led-error"></div>
                  <span class="led-label">Error</span>
                </div>
              </div>
              <div class="led-row" id="fixture-led-bank">
                <div class="led" data-index="0">
                  <div class="led-bulb red" id="fx-led-0"></div>
                  <span class="led-label" id="fx-label-0">Slot 1</span>
                </div>
                <div class="led" data-index="1">
                  <div class="led-bulb red" id="fx-led-1"></div>
                  <span class="led-label" id="fx-label-1">Slot 2</span>
                </div>
                <div class="led" data-index="2">
                  <div class="led-bulb red" id="fx-led-2"></div>
                  <span class="led-label" id="fx-label-2">Slot 3</span>
                </div>
                <div class="led" data-index="3">
                  <div class="led-bulb red" id="fx-led-3"></div>
                  <span class="led-label" id="fx-label-3">Slot 4</span>
                </div>
                <div class="led" data-index="4">
                  <div class="led-bulb red" id="fx-led-4"></div>
                  <span class="led-label" id="fx-label-4">Slot 5</span>
                </div>
                <div class="led" data-index="5">
                  <div class="led-bulb red" id="fx-led-5"></div>
                  <span class="led-label" id="fx-label-5">Slot 6</span>
                </div>
              </div>
            </div>
          </div>

          <div class="card wide">
            <h3>Virtual HOTAS</h3>
            <div class="switch">
              <label for="vjoy-en">Enable</label>
              <input type="checkbox" id="vjoy-en" onchange="vjoyEnable(this.checked)">
              <small class="muted">Use this when hardware isn’t connected</small>
            </div>

            <div class="virtual-layout">
              <div class="virtual-pad">
                <div id="pad" style="position:relative;width:220px;height:220px;border:1px solid #374151;border-radius:12px;background:radial-gradient(circle at center, #1a1d25, #0f141e);">
                  <div id="pad-dot" style="position:absolute;width:18px;height:18px;border-radius:999px;border:2px solid #60a5fa;transform:translate(-50%,-50%);left:110px;top:110px;background:#0f1115"></div>
                </div>
                <div class="switch" style="justify-content:space-between;margin-top:6px">
                  <small class="muted">X: <span id="vx">0.00</span>  Y: <span id="vy">0.00</span></small>
                </div>
              </div>

              <div class="virtual-controls">
                <label>Dimmer</label>
                <input type="range" id="vth" min="0" max="100" value="0" oninput="vjoyThrottle(this.value)" />
                <small class="muted">When “Virtual Throttle Invert” is True: 0% = full (axis -1), 100% = empty (axis +1)</small>

                <div style="margin-top:12px">
                  <label>Zoom Rocker</label>
                  <input type="range" id="vzoom" min="-100" max="100" value="0" oninput="vjoyZoom(this.value)" />
                  <small class="muted">Center = hold; push ± to adjust zoom (latches when released)</small>
                </div>

                <div style="margin-top:12px">
                  <div class="switch" style="gap:8px;flex-wrap:wrap">
                    <button class="btn" onpointerdown="vpress(BTN_ACTIVATE)" onpointerup="vrelease(BTN_ACTIVATE)">Activate</button>
                    <button class="btn danger" onpointerdown="vpress(BTN_RELEASE)" onpointerup="vrelease(BTN_RELEASE)">Release</button>
                    <button class="btn" onpointerdown="vpress(BTN_FLASH10)" onpointerup="vrelease(BTN_FLASH10)">Flash 10%</button>
                    <button class="btn" onpointerdown="vpress(BTN_DIMOFF)" onpointerup="vrelease(BTN_DIMOFF)">Blackout</button>
                    <button class="btn" onpointerdown="vpress(BTN_FINE)" onpointerup="vrelease(BTN_FINE)">Fine</button>
                    <button class="btn" onpointerdown="vpress(BTN_ZOOM)" onpointerup="vrelease(BTN_ZOOM)">Zoom Mod</button>
                  </div>
                  <small class="muted">If AX Zoom is set, “Zoom Mod” is ignored (rocker controls zoom incrementally).</small>
                </div>
              </div>
            </div>
          </div>

          <div class="card wide">
            <h3>Packet Capture</h3>
            <label for="pcap-interface">Interface</label>
            <select id="pcap-interface" disabled>
              <option value="">Loading…</option>
            </select>
            <small class="muted" id="pcap-interface-hint">Captures UDP 5568 traffic (sACN). Requires tcpdump with sufficient privileges.</small>
            <p class="small muted" id="pcap-no-ifaces" style="display:none;margin-top:4px;">No network adapters detected.</p>
            <div class="switch" style="margin-top:12px">
              <button class="btn" id="pcap-start" onclick="startPacketCapture()">Start Capture</button>
              <button class="btn danger" id="pcap-stop" onclick="stopPacketCapture()" disabled>Stop</button>
              <a class="btn" id="pcap-download" href="#" style="display:none">Download PCAP</a>
            </div>
            <p class="small muted" id="pcap-status">Idle</p>
            <p class="small" id="pcap-error" style="display:none;background:#4c1d1d;color:#fecaca;padding:6px 10px;border-radius:8px;margin-top:4px;"></p>
          </div>

          <div class="card wide">
            <h3>Logs</h3>
            <textarea id="logs" readonly></textarea>
          </div>
        </div>
      </section>

      <section class="tab-panel" data-tab="settings">
        <div class="panel-grid">
          <div class="card wide">
            <h3>Settings</h3>
            <form id="settings-form" onsubmit="saveSettings();return false;">

              <!-- sACN / Output -->
              <details class="section" open>
                <summary>Output (sACN)</summary>
                <div class="inner">
                  <div class="grid">
                    <div><label>Priority</label><input type="number" name="priority"></div>
                    <div><label>FPS</label><input type="number" name="fps"></div>
                    <div><label>Default Universe</label><input type="number" name="default_universe"></div>
                    <div class="cbrow"><input type="checkbox" id="per_address_priority_enabled" name="per_address_priority_enabled"><label for="per_address_priority_enabled">Enable Per-Address Priority</label></div>
                    <small class="muted" style="grid-column:1 / -1;">When enabled, only patched channels use the configured priority. Disable to send a uniform frame priority.</small>
                    <div style="grid-column:1 / -1">
                      <label>Network Adapters</label>
                      <div id="sacn-iface-list" class="checklist">
                        <p class="small muted" id="sacn-iface-loading">Loading adapters…</p>
                      </div>
                      <input type="hidden" name="sacn_bind_addresses" id="sacn_bind_addresses">
                      <small class="muted">Select adapters for sACN output. Leave empty to let the OS choose routing.</small>
                    </div>
                  </div>
                </div>
              </details>

              <!-- Input mapping -->
              <details class="section" open>
                <summary>Input Mapping (Axes &amp; Buttons)</summary>
                <div class="inner">
                  <div class="grid">
                    <div><label>AX Pan</label><input type="number" name="ax_pan"></div>
                    <div><label>AX Tilt</label><input type="number" name="ax_tilt"></div>
                    <div><label>AX Throttle</label><input type="number" name="ax_throt"></div>
                    <div><label>AX Zoom</label><input type="number" name="ax_zoom" placeholder="-1 to disable"></div>

                    <div><label>BTN Activate</label><input type="number" name="btn_activate"></div>
                    <div><label>BTN Release</label><input type="number" name="btn_release"></div>
                    <div><label>BTN Flash10</label><input type="number" name="btn_flash10"></div>
                    <div><label>BTN Dim Off</label><input type="number" name="btn_dim_off"></div>
                    <div><label>BTN Fine</label><input type="number" name="btn_fine"></div>
                    <div><label>BTN Zoom Mod</label><input type="number" name="btn_zoom_mod"></div>
                  </div>
                </div>
              </details>

              <details class="section">
                <summary>Button → Fixture Actions</summary>
                <div class="inner">
                  <p class="small muted">Define joystick button mappings (JSON).
                  Supported <code>type</code> values: <b>toggle_fixture</b>, <b>enable_fixture</b>, <b>disable_fixture</b>, <b>toggle_group</b>.
                  Optional <code>"mode":"hold"</code> for momentary press.</p>

                  <textarea name="button_actions" id="button_actions"></textarea>

                  <pre style="white-space:pre-wrap;background:#0b1020;border:1px solid #374151;border-radius:8px;padding:8px;margin-top:6px">
Example:
[
  {"button":7, "type":"toggle_fixture", "targets":["Left"]},
  {"button":8, "type":"toggle_fixture", "targets":["Right"]},
  {"button":9, "type":"toggle_group",   "targets":["Left","Right"]},
  {"button":10,"type":"enable_fixture", "targets":["Left","Right"], "mode":"hold"}
]
                  </pre>
                </div>
              </details>

              <!-- Motion / Behavior -->
              <details class="section" open>
                <summary>Movement &amp; Behavior</summary>
                <div class="inner">
                  <div class="grid">
                    <div class="cbrow"><input type="checkbox" id="invert_pan" name="invert_pan"><label for="invert_pan">Invert Pan</label></div>
                    <div class="cbrow"><input type="checkbox" id="invert_tilt" name="invert_tilt"><label for="invert_tilt">Invert Tilt</label></div>
                    <div class="cbrow"><input type="checkbox" id="throttle_invert" name="throttle_invert"><label for="throttle_invert">Throttle Invert</label></div>

                    <div><label>Deadband</label><input type="text" name="deadband"></div>
                    <div><label>Expo</label><input type="text" name="expo"></div>
                    <div><label>Speed</label><input type="number" name="speed"></div>

                    <div><label>Pan Min</label><input type="number" name="pan_min"></div>
                    <div><label>Pan Max</label><input type="number" name="pan_max"></div>
                    <div><label>Tilt Min</label><input type="number" name="tilt_min"></div>
                    <div><label>Tilt Max</label><input type="number" name="tilt_max"></div>

                    <div><label>Fine Divisor</label><input type="number" name="fine_divisor"></div>
                    <div><label>Flash10 Level</label><input type="number" name="flash10_level"></div>
                  </div>
                </div>
              </details>

              <!-- Zoom Axis -->
              <details class="section">
                <summary>Zoom Axis (Rocker)</summary>
                <div class="inner">
                  <div class="grid">
                    <div class="cbrow"><input type="checkbox" id="zoom_invert" name="zoom_invert"><label for="zoom_invert">Zoom Invert</label></div>
                    <div><label>Zoom Deadband</label><input type="text" name="zoom_deadband" placeholder="0.0–0.2"></div>
                    <div><label>Zoom Expo</label><input type="text" name="zoom_expo" placeholder="0.0–1.0"></div>
                    <div><label>Zoom Speed</label><input type="number" name="zoom_speed" placeholder="e.g. 3000"></div>
                  </div>
                  <small class="muted">If AX Zoom ≥ 0, the rocker adjusts zoom incrementally and the value latches when released. “Zoom Mod” is ignored.</small>
                </div>
              </details>

              <!-- GPIO LEDs -->
              <details class="section">
                <summary>GPIO LEDs (Raspberry Pi)</summary>
                <div class="inner">
                  <div class="grid">
                    <div class="cbrow"><input type="checkbox" id="gpio_enabled" name="gpio_enabled"><label for="gpio_enabled">Enable GPIO LEDs</label></div>
                    <div><label>GPIO Green Pin</label><input type="number" name="gpio_green_pin"></div>
                    <div><label>GPIO Red Pin</label><input type="number" name="gpio_red_pin"></div>
                    <div class="cbrow"><input type="checkbox" id="gpio_active_low" name="gpio_active_low"><label for="gpio_active_low">Active Low</label></div>
                    <div><label>Fixture LED Pins (max 6)</label><input type="text" name="gpio_fixture_led_pins" placeholder="e.g. 5,6,13,19,26,12"></div>
                    <small class="muted" style="grid-column: 1 / -1;">Enter BCM pin numbers, comma-separated. LEDs light when fixtures are enabled.</small>
                  </div>
                </div>
              </details>

              <!-- Virtual Joystick -->
              <details class="section">
                <summary>Virtual Joystick</summary>
                <div class="inner">
                  <div class="grid">
                    <div class="cbrow"><input type="checkbox" id="virtual_joystick_enabled" name="virtual_joystick_enabled"><label for="virtual_joystick_enabled">Enable Virtual HOTAS</label></div>
                    <div class="cbrow"><input type="checkbox" id="virtual_throttle_invert" name="virtual_throttle_invert"><label for="virtual_throttle_invert">Virtual Throttle Invert</label></div>
                  </div>
                </div>
              </details>

              <!-- Debug -->
              <details class="section">
                <summary>Debug Logging</summary>
                <div class="inner">
                  <div class="grid">
                    <div class="cbrow"><input type="checkbox" id="debug_log_sacn" name="debug_log_sacn"><label for="debug_log_sacn">Log sACN Frames</label></div>
                    <div class="cbrow"><input type="checkbox" id="debug_controller_buttons" name="debug_controller_buttons"><label for="debug_controller_buttons">Controller Button Debug</label></div>
                    <div><label>Debug Interval (ms)</label><input type="number" name="debug_log_interval_ms"></div>
                    <div class="cbrow"><input type="checkbox" id="debug_log_only_changes" name="debug_log_only_changes"><label for="debug_log_only_changes">Only Log Changes</label></div>
                    <div>
                      <label for="debug_log_mode">Debug Mode</label>
                      <select name="debug_log_mode" id="debug_log_mode">
                        <option value="summary">Summary</option>
                        <option value="nonzero">Nonzero</option>
                        <option value="full">Full</option>
                      </select>
                    </div>
                    <div><label>Nonzero Limit</label><input type="number" name="debug_log_nonzero_limit"></div>
                  </div>
                </div>
              </details>

              <div class="form-actions">
                <button class="btn primary" type="submit">Save Settings</button>
                <button class="btn danger" type="button" id="restart-btn" onclick="restartService(this)">Restart Service</button>
              </div>
            </form>
          </div>

          <div class="card wide">
            <h3>Fixtures</h3>
            <div class="switch">
              <label for="multi-universe">Multi-Universe Mode</label>
              <input type="checkbox" id="multi-universe" onchange="toggleMU()">
              <span class="small muted">When off, all fixtures use Default Universe</span>
            </div>

            <div id="fixture-list"></div>

            <div class="subcard">
              <h4>Add Fixture</h4>
              <form id="fx-form" onsubmit="addFixture();return false;">
                <div class="fxgrid">
                  <div><label>ID</label><input type="text" name="id" required></div>
                  <div class="cbrow"><input type="checkbox" id="fx_enabled" checked><label for="fx_enabled">Enabled</label></div>
                  <div><label>Universe</label><input type="number" name="universe"></div>
                  <div><label>Start Addr (info)</label><input type="number" name="start_addr"></div>

                  <div><label>Pan Coarse</label><input type="number" name="pan_coarse"></div>
                  <div><label>Pan Fine</label><input type="number" name="pan_fine"></div>
                  <div><label>Tilt Coarse</label><input type="number" name="tilt_coarse"></div>
                  <div><label>Tilt Fine</label><input type="number" name="tilt_fine"></div>

                  <div><label>Dimmer</label><input type="number" name="dimmer"></div>
                  <div><label>Zoom</label><input type="number" name="zoom"></div>
                  <div><label>Zoom Fine</label><input type="number" name="zoom_fine"></div>

                  <div><label>Color Temp Channel (DMX 11)</label><input type="number" name="color_temp_channel" value="11"></div>
                  <div><label>Color Temp Value</label><input type="number" name="color_temp_value" value="0" min="0" max="255"></div>

                  <div><label>Pan Bias</label><input type="number" name="pan_bias" value="0"></div>
                  <div><label>Tilt Bias</label><input type="number" name="tilt_bias" value="0"></div>

                  <div><label>Status LED (1-6)</label><input type="number" name="status_led" min="0" max="6" placeholder="1-6"></div>
                  <div style="grid-column:1/-1"><small class="muted">Leave blank or 0 to disable the status LED mapping.</small></div>

                  <div class="cbrow"><input type="checkbox" id="fx_invert_pan"><label for="fx_invert_pan">Invert Pan</label></div>
                  <div class="cbrow"><input type="checkbox" id="fx_invert_tilt"><label for="fx_invert_tilt">Invert Tilt</label></div>
                </div>

                <!-- hidden compat fields sent to backend -->
                <input type="hidden" name="enabled" id="fx_enabled_hidden" value="True">
                <input type="hidden" name="invert_pan" id="fx_invert_pan_hidden" value="False">
                <input type="hidden" name="invert_tilt" id="fx_invert_tilt_hidden" value="False">

                <div class="form-actions">
                  <p id="fixture-limit-msg" class="small muted" style="margin:0 0 0.5rem 0;"></p>
                  <button class="btn primary" type="submit" id="fx-add-btn">Add Fixture</button>
                </div>
              </form>
            </div>

            <div class="subcard">
              <h4>Import &amp; Export</h4>
              <div class="form-actions">
                <a class="btn" href="/api/fixtures/export" target="_blank">Export CSV</a>
                <button class="btn" type="button" onclick="showImport()">Import CSV</button>
              </div>
              <div id="import-area" style="display:none">
                <textarea id="csvtext" placeholder="Paste CSV here..."></textarea>
                <div class="form-actions">
                  <button class="btn primary" type="button" onclick="doImport()">Import</button>
                  <button class="btn danger" type="button" onclick="hideImport()">Cancel</button>
                </div>
                <p class="small muted">Columns: id,enabled,universe,start_addr,pan_coarse,pan_fine,tilt_coarse,tilt_fine,dimmer,zoom,zoom_fine,color_temp_channel,color_temp_value,invert_pan,invert_tilt,pan_bias,tilt_bias,status_led</p>
              </div>
            </div>
          </div>
        </div>
      </section>

    <script>
    async function fetchJSON(url, opts){ const r = await fetch(url, opts); const ct = r.headers.get('content-type')||''; if(!r.ok) throw new Error(await r.text()); return ct.includes('application/json') ? await r.json() : await r.text(); }

    const TAB_STORAGE_KEY = 'td.activeTab';
    const FIXTURE_LIMIT = 6;

    let NETWORK_ADAPTERS = null;
    let CAPTURE_STATE = null;
    let CAPTURE_POLL_TIMER = null;

    async function ensureNetworkAdapters(){
      if(Array.isArray(NETWORK_ADAPTERS)) return NETWORK_ADAPTERS;
      try{
        const resp = await fetchJSON('/api/network/adapters');
        NETWORK_ADAPTERS = Array.isArray(resp.adapters) ? resp.adapters : [];
      }catch(e){
        NETWORK_ADAPTERS = [];
        console.error('Failed to load network adapters', e);
      }
      return NETWORK_ADAPTERS;
    }

    function syncSacnInterfaces(){
      const container = document.getElementById('sacn-iface-list');
      const hidden = document.getElementById('sacn_bind_addresses');
      if(!hidden) return;
      const selected = [];
      if(container){
        container.querySelectorAll('input[type="checkbox"][data-addr]').forEach(cb => {
          if(cb.checked){
            selected.push(cb.dataset.addr);
          }
        });
      }
      hidden.value = JSON.stringify(selected);
    }

    function renderCaptureInterfaceOptions(selectedIface){
      const select = document.getElementById('pcap-interface');
      const noMsg = document.getElementById('pcap-no-ifaces');
      if(!select){
        if(noMsg) noMsg.style.display = 'none';
        return;
      }
      const adapters = Array.isArray(NETWORK_ADAPTERS) ? NETWORK_ADAPTERS : [];
      const seen = new Set();
      const previous = select.value;
      const target = selectedIface || previous || '';
      select.innerHTML = '';

      if(!adapters.length){
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No adapters available';
        select.appendChild(opt);
        select.value = '';
        select.disabled = true;
        if(noMsg) noMsg.style.display = 'block';
        return;
      }

      if(noMsg) noMsg.style.display = 'none';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Select interface…';
      select.appendChild(placeholder);

      adapters.forEach(adapter => {
        const name = adapter && adapter.name ? String(adapter.name) : '';
        if(!name || seen.has(name)) return;
        seen.add(name);
        const option = document.createElement('option');
        option.value = name;
        option.textContent = adapter.address ? `${name} – ${adapter.address}` : name;
        select.appendChild(option);
      });

      if(target && seen.has(target)){
        select.value = target;
      }else{
        select.value = '';
      }
      select.disabled = false;
    }

    function renderNetworkAdapters(selected){
      const container = document.getElementById('sacn-iface-list');
      const hidden = document.getElementById('sacn_bind_addresses');
      if(!container){
        if(hidden) hidden.value = JSON.stringify(selected || []);
        return;
      }
      const adapters = Array.isArray(NETWORK_ADAPTERS) ? NETWORK_ADAPTERS : [];
      const selectedSet = new Set((selected || []).map(v => String(v)));
      container.innerHTML = '';
      if(!adapters.length){
        const msg = document.createElement('p');
        msg.className = 'small muted';
        msg.textContent = 'No network adapters detected.';
        container.appendChild(msg);
      }else{
        adapters.forEach((adapter, idx) => {
          const id = `iface-${idx}`;
          const wrapper = document.createElement('label');
          wrapper.className = 'checklist-option';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.id = id;
          cb.dataset.addr = adapter.address;
          cb.checked = selectedSet.has(String(adapter.address));
          wrapper.appendChild(cb);
          const textWrap = document.createElement('div');
          textWrap.className = 'checklist-text';
          const title = document.createElement('div');
          title.className = 'checklist-title';
          title.textContent = adapter.label || `${adapter.name} – ${adapter.address}`;
          textWrap.appendChild(title);
          if(adapter.description){
            const desc = document.createElement('small');
            desc.className = 'muted';
            desc.textContent = adapter.description;
            textWrap.appendChild(desc);
          }else if(adapter.is_loopback){
            const note = document.createElement('small');
            note.className = 'muted';
            note.textContent = 'Loopback';
            textWrap.appendChild(note);
          }
          wrapper.appendChild(textWrap);
          container.appendChild(wrapper);
        });
      }
      if(!container.dataset.bound){
        container.addEventListener('change', syncSacnInterfaces);
        container.dataset.bound = 'true';
      }
      syncSacnInterfaces();
      renderCaptureInterfaceOptions(CAPTURE_STATE && CAPTURE_STATE.interface ? CAPTURE_STATE.interface : '');
    }

    async function refreshNetworkAdapters(selected){
      await ensureNetworkAdapters();
      renderNetworkAdapters(selected);
    }

    function formatBytes(bytes){
      const value = Number(bytes);
      if(!Number.isFinite(value) || value <= 0){
        return '0 B';
      }
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let val = value;
      let idx = 0;
      while(val >= 1024 && idx < units.length - 1){
        val /= 1024;
        idx += 1;
      }
      const decimals = val >= 10 || idx === 0 ? 0 : 1;
      return `${val.toFixed(decimals)} ${units[idx]}`;
    }

    function formatDuration(seconds){
      const total = Math.max(0, Number(seconds) || 0);
      if(total >= 3600){
        const hours = Math.floor(total / 3600);
        const mins = Math.floor((total % 3600) / 60);
        return `${hours}h ${mins}m`;
      }
      if(total >= 60){
        const mins = Math.floor(total / 60);
        const secs = Math.floor(total % 60);
        return `${mins}m ${secs}s`;
      }
      return `${Math.floor(total)}s`;
    }

    function showCaptureError(message){
      const el = document.getElementById('pcap-error');
      if(!el) return;
      if(message){
        el.textContent = message;
        el.style.display = 'block';
      }else{
        el.textContent = '';
        el.style.display = 'none';
      }
    }

    function parseErrorMessage(err){
      if(!err) return 'Unexpected error';
      if(typeof err === 'string') return err;
      if(err.error) return err.error;
      if(err.message){
        try{
          const data = JSON.parse(err.message);
          if(data && data.error) return data.error;
        }catch(_){ }
        return err.message;
      }
      return String(err);
    }

    function updateCaptureUI(state){
      CAPTURE_STATE = state || {};
      const select = document.getElementById('pcap-interface');
      const startBtn = document.getElementById('pcap-start');
      const stopBtn = document.getElementById('pcap-stop');
      const statusEl = document.getElementById('pcap-status');
      const downloadLink = document.getElementById('pcap-download');
      const adapters = Array.isArray(NETWORK_ADAPTERS) ? NETWORK_ADAPTERS : [];
      renderCaptureInterfaceOptions(CAPTURE_STATE.interface || '');

      const active = !!CAPTURE_STATE.active;
      const iface = CAPTURE_STATE.interface || (select ? select.value : '');
      const size = Number(CAPTURE_STATE.filesize || 0);

      if(select){
        const hasAdapters = adapters.length > 0;
        if(iface && hasAdapters && !select.value){
          select.value = iface;
        }
        select.disabled = active || !hasAdapters;
      }

      if(startBtn){
        const ready = select && select.value;
        startBtn.disabled = active || !ready;
      }

      if(stopBtn){
        stopBtn.disabled = !active;
      }

      if(downloadLink){
        if(CAPTURE_STATE.download_ready){
          downloadLink.style.display = 'inline-block';
          downloadLink.href = `/api/capture/download?ts=${Date.now()}`;
        }else{
          downloadLink.style.display = 'none';
          downloadLink.href = '#';
        }
      }

      if(statusEl){
        let text = 'Idle';
        if(active){
          text = `Capturing on ${iface || 'selected interface'}`;
          if(CAPTURE_STATE.started_at){
            try{
              const started = new Date(CAPTURE_STATE.started_at);
              const seconds = (Date.now() - started.getTime()) / 1000;
              text += ` • ${formatDuration(seconds)}`;
            }catch(_){ }
          }
          if(size > 0){
            text += ` • ${formatBytes(size)}`;
          }
        }else if(CAPTURE_STATE.download_ready){
          text = `Capture ready (${formatBytes(size)})`;
          if(iface) text += ` from ${iface}`;
        }else if(iface){
          text = `Last capture on ${iface}`;
        }
        statusEl.textContent = text;
      }

      if(CAPTURE_STATE.error){
        showCaptureError(CAPTURE_STATE.error);
      }else{
        showCaptureError('');
      }
    }

    async function refreshCaptureState(){
      try{
        await ensureNetworkAdapters();
        const state = await fetchJSON('/api/capture/status');
        updateCaptureUI(state);
      }catch(err){
        console.error('Failed to refresh capture state', err);
      }
    }

    async function startPacketCapture(){
      const select = document.getElementById('pcap-interface');
      const startBtn = document.getElementById('pcap-start');
      if(!select || !select.value){
        showCaptureError('Select an interface before starting a capture.');
        if(startBtn) startBtn.disabled = false;
        return;
      }
      showCaptureError('');
      if(startBtn) startBtn.disabled = true;
      try{
        const state = await fetchJSON('/api/capture/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ interface: select.value })
        });
        updateCaptureUI(state);
      }catch(err){
        const message = parseErrorMessage(err);
        const state = Object.assign({}, CAPTURE_STATE || {});
        state.error = message;
        updateCaptureUI(state);
      }finally{
        if(startBtn){
          const ready = select && select.value;
          const active = CAPTURE_STATE && CAPTURE_STATE.active;
          startBtn.disabled = !!active || !ready;
        }
      }
    }

    async function stopPacketCapture(){
      const stopBtn = document.getElementById('pcap-stop');
      if(stopBtn) stopBtn.disabled = true;
      try{
        const state = await fetchJSON('/api/capture/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'}
        });
        updateCaptureUI(state);
      }catch(err){
        const message = parseErrorMessage(err);
        const state = Object.assign({}, CAPTURE_STATE || {});
        state.error = message;
        updateCaptureUI(state);
      }finally{
        if(stopBtn){
          const active = CAPTURE_STATE && CAPTURE_STATE.active;
          stopBtn.disabled = !active;
        }
      }
    }

    function setActiveTab(tab){
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
      });
      document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.dataset.tab === tab);
      });
      try{ localStorage.setItem(TAB_STORAGE_KEY, tab); }catch(_){ }
    }

    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
    });

    (function initTab(){
      let initial = 'dashboard';
      try{
        const stored = localStorage.getItem(TAB_STORAGE_KEY);
        if(stored && document.querySelector(`.tab-btn[data-tab="${stored}"]`)){
          initial = stored;
        }
      }catch(_){ }
      setActiveTab(initial);
    })();

    function setPill(id, ok, off=false){
      const el = document.getElementById(id);
      el.className = 'pill ' + (off ? 'off' : (ok ? 'ok' : 'err'));
    }

    function setLed(id, on){
      const el = document.getElementById(id);
      if(!el) return;
      if(on){
        el.classList.add('on');
      }else{
        el.classList.remove('on');
      }
    }

    function updateFixtureLeds(list){
      const rows = document.querySelectorAll('#fixture-led-bank .led');
      rows.forEach((row, idx) => {
        const data = Array.isArray(list) ? list[idx] : null;
        const bulb = row.querySelector('.led-bulb');
        const label = row.querySelector('.led-label');
        const on = data && !!data.on;
        if(bulb){ bulb.classList.toggle('on', on); }
        if(label){ label.textContent = data && data.label ? data.label : `Slot ${idx+1}`; }
      });
    }

    async function refresh(){
      try{
        const s = await fetchJSON('/api/status');
        document.getElementById('joy-name').innerText = s.joystick_name || (s.virtual ? 'Virtual HOTAS' : '-');
        document.getElementById('joy-axes').innerText = s.axes;
        document.getElementById('joy-buttons').innerText = s.buttons;
        document.getElementById('last-frame').innerText = s.last_frame || '-';

        setPill('status-pill', s.active, !s.active);
        document.getElementById('status-text').innerText = s.active ? 'Active' : 'Idle';

        setPill('health-pill', !s.error, s.joystick_name=='' && !s.error);
        document.getElementById('health-text').innerText = s.error ? ('Error: ' + s.error_msg) : 'Good';

        setLed('led-power', !!s.power_led);
        setLed('led-error', !!s.error_led);
        updateFixtureLeds(s.fixture_leds || []);

        const l = await fetch('/api/logs'); const t = await l.text();
        const ta = document.getElementById('logs'); ta.value = t; ta.scrollTop = ta.scrollHeight;
      }catch(e){ console.error(e); }
    }

    /* ---------- Settings load/save with checkboxes ---------- */
    function isCheckbox(el){ return el && el.type === 'checkbox'; }

    async function loadSettings(){
      const data = await fetchJSON('/api/settings');
      const form = document.getElementById('settings-form');
      for(const k in data){
        if(!form[k]) continue;
        const el = form[k];
        if(isCheckbox(el)){
          el.checked = !!data[k];
        }else{
          if(Array.isArray(data[k])){
            el.value = data[k].join(', ');
          }else{
            el.value = data[k];
            if(el.tagName === 'SELECT'){
              const target = String(data[k] ?? '');
              let matched = false;
              for(const opt of el.options){
                if(opt.value === target){
                  matched = true;
                  break;
                }
              }
              if(!matched && el.options.length){
                el.value = el.options[0].value;
              }
            }
          }
        }
      }
      const sacnSelected = Array.isArray(data.sacn_bind_addresses) ? data.sacn_bind_addresses : [];
      await refreshNetworkAdapters(sacnSelected);
      const sacnHidden = document.getElementById('sacn_bind_addresses');
      if(sacnHidden){
        sacnHidden.value = JSON.stringify(sacnSelected);
      }
      if(form["button_actions"]){
        try{
          form["button_actions"].value = JSON.stringify(data.button_actions || [], null, 2);
        }catch{
          form["button_actions"].value = "[]";
        }
      }
      // sync virtual throttle invert flag into the slider
      const inv = !!data.virtual_throttle_invert;
      const th = document.getElementById('vth');
      if(th) th.dataset.invert = String(inv);
    }

    async function saveSettings(){
      // for fixtures "add", mirror the visible checkboxes into compat text fields
      syncFixtureCompat();
      syncSacnInterfaces();

      const form = document.getElementById('settings-form');
      const data = {};
      for(const el of form.elements){
        if(!el.name) continue;
        data[el.name] = isCheckbox(el) ? el.checked : el.value;
      }
      if('gpio_fixture_led_pins' in data){
        const pins = String(data.gpio_fixture_led_pins || '')
          .split(',')
          .map(p => p.trim())
          .filter(p => p.length)
          .map(p => Number(p))
          .filter(p => Number.isInteger(p));
        data.gpio_fixture_led_pins = pins;
      }
      if(typeof data.sacn_bind_addresses === 'string'){
        try{
          const parsed = JSON.parse(data.sacn_bind_addresses);
          data.sacn_bind_addresses = Array.isArray(parsed) ? parsed : [];
        }catch{
          data.sacn_bind_addresses = [];
        }
      }
      const resp = await fetchJSON('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
      alert((resp && resp.message) || 'Saved');
      loadFixtures();
      readBtnIndicesFromForm();
      await loadSettings(); // refresh slider invert flag
    }

    /* ---------- Restart service (two-step confirm) ---------- */
    let restartConfirmTimer = null;

    async function restartService(btn){
      if(!btn) return;
      if(btn.dataset.confirm === 'true'){
        btn.disabled = true;
        btn.textContent = 'Restarting...';
        btn.classList.add('danger');
        clearTimeout(restartConfirmTimer);
        restartConfirmTimer = null;
        try{
          const resp = await fetchJSON('/api/restart', {method:'POST'});
          alert((resp && resp.message) || 'Restarting service...');
        }catch(e){
          alert(e.message || e);
          btn.disabled = false;
          btn.textContent = 'Restart Service';
          btn.classList.add('danger');
          btn.dataset.confirm = '';
          return;
        }
        // don't reset text; service will restart shortly
        return;
      }
      btn.dataset.confirm = 'true';
      btn.textContent = 'Click again to confirm';
      btn.classList.add('danger');
      clearTimeout(restartConfirmTimer);
      restartConfirmTimer = setTimeout(() => {
        btn.dataset.confirm = '';
        btn.textContent = 'Restart Service';
        btn.disabled = false;
      }, 5000);
    }

    /* ---------- Fixtures helper to mirror checkboxes into compat fields ---------- */
    function syncFixtureCompat(){
      const on = document.getElementById('fx_enabled').checked;
      const invP = document.getElementById('fx_invert_pan').checked;
      const invT = document.getElementById('fx_invert_tilt').checked;
      document.getElementById('fx_enabled_hidden').value = on ? 'True' : 'False';
      document.getElementById('fx_invert_pan_hidden').value = invP ? 'True' : 'False';
      document.getElementById('fx_invert_tilt_hidden').value = invT ? 'True' : 'False';
    }


    /* ---------- Existing endpoints ---------- */
    async function activate(){ await fetchJSON('/api/activate', {method:'POST'}); }
    async function release(){ await fetchJSON('/api/release', {method:'POST'}); }

    function showImport(){ document.getElementById('import-area').style.display='block'; }
    function hideImport(){ document.getElementById('import-area').style.display='none'; }
    async function doImport(){
      const csv = document.getElementById('csvtext').value;
      try{
        await fetchJSON('/api/fixtures/import', {method:'POST', headers:{'Content-Type':'text/plain'}, body: csv});
        hideImport(); loadFixtures();
      }catch(e){
        alert(e.message || e);
      }
    }

    async function loadFixtures(){
      const data = await fetchJSON('/api/fixtures');
      document.getElementById('multi-universe').checked = !!data.multi_universe_enabled;
      const form = document.getElementById('fx-form');
      const addBtn = document.getElementById('fx-add-btn');
      const limitMsg = document.getElementById('fixture-limit-msg');
      const count = data.fixtures.length;
      const remaining = Math.max(0, FIXTURE_LIMIT - count);
      if(form){ form.dataset.remaining = String(remaining); }
      if(addBtn){ addBtn.disabled = remaining <= 0; }
      if(limitMsg){
        if(remaining <= 0){
          limitMsg.textContent = `Fixture limit reached (${FIXTURE_LIMIT}). Delete one to add another.`;
        }else if(remaining === 1){
          limitMsg.textContent = 'You can add 1 more fixture.';
        }else{
          limitMsg.textContent = `You can add ${remaining} more fixtures.`;
        }
      }
      const wrap = document.getElementById('fixture-list');
      wrap.innerHTML = '';
      if(!count){ wrap.innerHTML = '<small>No fixtures yet.</small>'; return; }
      for(const f of data.fixtures.slice(0, FIXTURE_LIMIT)){
        const card = document.createElement('div');
        card.className = 'fixture-card';
        card.innerHTML = `
          <div><b>${f.id}</b> ${f.enabled ? '<span class="badge ok">Enabled</span>' : '<span class="badge warn">Disabled</span>'}</div>
          <div class="small muted">Uni ${f.universe} • Pan ${f.pan_coarse}/${f.pan_fine||0} • Tilt ${f.tilt_coarse}/${f.tilt_fine||0} • Dim ${f.dimmer||0} • Zoom ${f.zoom||0}${f.zoom_fine?('/'+f.zoom_fine):''}${colorTempSummary(f)}</div>
          <div class="small muted">Invert P:${f.invert_pan? 'Y':'N'} T:${f.invert_tilt? 'Y':'N'} • Bias P:${f.pan_bias||0} T:${f.tilt_bias||0}${statusLedSummary(f)}</div>
          <details class="fixture-details">
            <summary>Edit</summary>
            <div class="fxgrid">
              ${editInput('Enabled','enabled',f.enabled)}
              ${editInput('Universe','universe',f.universe,'number')}
              ${editInput('Start Addr','start_addr',f.start_addr,'number')}
              ${editInput('Pan Coarse','pan_coarse',f.pan_coarse,'number')}
              ${editInput('Pan Fine','pan_fine',f.pan_fine,'number')}
              ${editInput('Tilt Coarse','tilt_coarse',f.tilt_coarse,'number')}
              ${editInput('Tilt Fine','tilt_fine',f.tilt_fine,'number')}
              ${editInput('Dimmer','dimmer',f.dimmer,'number')}
              ${editInput('Zoom','zoom',f.zoom,'number')}
              ${editInput('Zoom Fine','zoom_fine',f.zoom_fine,'number')}
              ${editInput('Color Temp Ch','color_temp_channel',f.color_temp_channel,'number')}
              ${editInput('Color Temp Val','color_temp_value',f.color_temp_value,'number')}
              ${editInput('Invert Pan','invert_pan',f.invert_pan)}
              ${editInput('Invert Tilt','invert_tilt',f.invert_tilt)}
              ${editInput('Pan Bias','pan_bias',f.pan_bias,'number')}
              ${editInput('Tilt Bias','tilt_bias',f.tilt_bias,'number')}
              ${editInput('Status LED','status_led',f.status_led,'number')}
            </div>
            <div class="form-actions">
              <button class="btn primary" onclick="saveFixture('${f.id}', this.closest('.form-actions').previousElementSibling)">Save</button>
              <button class="btn" onclick="toggleFixture('${f.id}', ${!f.enabled})">${f.enabled?'Disable':'Enable'}</button>
              <button class="btn danger" onclick="deleteFixture('${f.id}')">Delete</button>
            </div>
          </details>`;
        wrap.appendChild(card);
      }
    }

    function editInput(label, name, value, type){
      if(type==='number'){
        return `<div><label>${label}</label><input type="number" name="${name}" value="${value??''}"></div>`;
      }
      const raw = value === undefined || value === null ? '' : String(value);
      const rawLower = raw.toLowerCase();
      const boolish = (typeof value === 'boolean') || ['true','false','1','0','yes','no','on','off',''].includes(rawLower);
      if(boolish){
        const truthy = ['1','true','yes','on'];
        const boolVal = (typeof value === 'boolean') ? value : truthy.includes(rawLower);
        return `<div><label>${label}</label><select name="${name}"><option value="True"${boolVal?' selected':''}>True</option><option value="False"${!boolVal?' selected':''}>False</option></select></div>`;
      }
      return `<div><label>${label}</label><input type="text" name="${name}" value="${raw}"></div>`;
    }

    function colorTempSummary(f){
      const chan = Number(f.color_temp_channel || 0);
      if(chan > 0){
        const valRaw = f.color_temp_value;
        const val = (valRaw === undefined || valRaw === null || valRaw === '') ? '' : `=${valRaw}`;
        return ` • Color Temp ${chan}${val}`;
      }
      return '';
    }

    function statusLedSummary(f){
      const led = Number(f.status_led || 0);
      if(led > 0){
        return ` • Status LED #${led}`;
      }
      return '';
    }

    async function toggleFixture(id, enabled){
      await fetchJSON('/api/fixtures/'+encodeURIComponent(id), {
        method:'PATCH', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({enabled})
      });
      loadFixtures();
    }

    async function deleteFixture(id){
      if(!confirm('Delete fixture '+id+'?')) return;
      await fetchJSON('/api/fixtures/'+encodeURIComponent(id), {method:'DELETE'});
      loadFixtures();
    }

    async function saveFixture(id, gridEl){
      const fields = {};
      for(const el of gridEl.querySelectorAll('input, select')){
        fields[el.name] = el.value;
      }
      await fetchJSON('/api/fixtures/'+encodeURIComponent(id), {
        method:'PATCH', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(fields)
      });
      loadFixtures();
    }

    async function addFixture(){
      const form = document.getElementById('fx-form');
      const remaining = Number(form?.dataset?.remaining || '0');
      if(remaining <= 0){
        alert(`Fixture limit of ${FIXTURE_LIMIT} reached. Delete a fixture before adding another.`);
        return;
      }
      syncFixtureCompat();
      const data = {};
      for(const el of form.elements){ if(el.name) data[el.name]=el.value; }
      try{
        await fetchJSON('/api/fixtures', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
      }catch(e){
        alert(e.message || e);
        return;
      }
      form.reset();
      // reset compat defaults
      document.getElementById('fx_enabled').checked = true;
      document.getElementById('fx_invert_pan').checked = false;
      document.getElementById('fx_invert_tilt').checked = false;
      if(form.enabled) form.enabled.value = 'True';
      if(form.invert_pan) form.invert_pan.value = 'False';
      if(form.invert_tilt) form.invert_tilt.value = 'False';
      if(form.status_led) form.status_led.value = '';
      loadFixtures();
    }

    async function toggleMU(){
      const checked = document.getElementById('multi-universe').checked;
      await fetchJSON('/api/fixtures/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({multi_universe_enabled: checked})});
      loadFixtures();
    }

    /* -------- Virtual HOTAS JS -------- */
    let BTN_ACTIVATE, BTN_RELEASE, BTN_FLASH10, BTN_DIMOFF, BTN_FINE, BTN_ZOOM;
    function readBtnIndicesFromForm(){
      const f = document.getElementById('settings-form');
      const g = k => f && f[k] ? parseInt(f[k].value||'0') : 0;
      BTN_ACTIVATE = g('btn_activate');
      BTN_RELEASE  = g('btn_release');
      BTN_FLASH10  = g('btn_flash10');
      BTN_DIMOFF   = g('btn_dim_off');
      BTN_FINE     = g('btn_fine');
      BTN_ZOOM     = g('btn_zoom_mod');
    }

    async function vjoySyncEnabled(){
      const s = await fetchJSON('/api/virtual');
      document.getElementById('vjoy-en').checked = !!s.enabled;
      setPadDot(s.x, s.y);
      document.getElementById('vx').innerText = Number(s.x).toFixed(2);
      document.getElementById('vy').innerText = Number(s.y).toFixed(2);
      const sv = Math.round((s.throttle + 1) * 50); // -1..1 → 0..100 display
      const th = document.getElementById('vth'); if(th) th.value = sv;
      const vz = document.getElementById('vzoom'); if(vz) vz.value = Math.round((s.zaxis || 0)*100);
    }

    async function vjoyEnable(on){
      await fetchJSON('/api/virtual', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:on})});
    }

    function setPadDot(x, y){
      const pad = document.getElementById('pad');
      const dot = document.getElementById('pad-dot');
      const w = pad.clientWidth, h = pad.clientHeight;
      const cx = (x*0.5 + 0.5) * w;
      const cy = (1 - (y*0.5 + 0.5)) * h;
      dot.style.left = cx + 'px';
      dot.style.top  = cy + 'px';
    }

    function padSend(x, y){
      document.getElementById('vx').innerText = x.toFixed(2);
      document.getElementById('vy').innerText = y.toFixed(2);
      fetch('/api/virtual', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({x,y})
      }).catch(()=>{});
    }

    function padPointToXY(ev){
      const pad = document.getElementById('pad');
      const r = pad.getBoundingClientRect();
      const px = Math.max(0, Math.min(r.width,  (ev.clientX - r.left)));
      const py = Math.max(0, Math.min(r.height, (ev.clientY - r.top)));
      const x = (px / r.width) * 2 - 1;
      const y = -((py / r.height) * 2 - 1);
      return { x: Math.max(-1, Math.min(1, x)), y: Math.max(-1, Math.min(1, y)) };
    }

    function padCenter(){
      const x = 0, y = 0;
      setPadDot(x, y);
      padSend(x, y);
    }

    (function initPad(){
      const pad = document.getElementById('pad');
      if(!pad) return;

      let down = false;
      let pointerId = null;

      pad.addEventListener('pointerdown', ev => {
        ev.preventDefault();
        down = true;
        pointerId = ev.pointerId;
        try{ pad.setPointerCapture(pointerId); }catch(_){}
        const {x, y} = padPointToXY(ev);
        setPadDot(x, y);
        padSend(x, y);
      });

      pad.addEventListener('pointermove', ev => {
        if(!down) return;
        ev.preventDefault();
        const {x, y} = padPointToXY(ev);
        setPadDot(x, y);
        padSend(x, y);
      });

      function end(ev){
        if(!down) return;
        down = false;
        try{ pad.releasePointerCapture(pointerId); }catch(_){}
        pointerId = null;
        padCenter(); // spring to center on release
      }

      pad.addEventListener('pointerup', end);
      pad.addEventListener('pointercancel', end);
      pad.addEventListener('pointerleave', end);

      setPadDot(0, 0);
    })();

    (() => {
      const zoom = document.getElementById('vzoom');
      if(!zoom) return;

      let engaged = false;
      let pointerId = null;

      const centerZoom = () => {
        if(!engaged) return;
        engaged = false;
        if(pointerId !== null){
          try{ zoom.releasePointerCapture(pointerId); }catch(_){ }
          pointerId = null;
        }
        zoom.value = '0';
        vjoyZoom(0);
      };

      zoom.addEventListener('pointerdown', ev => {
        engaged = true;
        pointerId = ev.pointerId;
        try{ zoom.setPointerCapture(pointerId); }catch(_){ }
      });

      ['pointerup','pointercancel','lostpointercapture'].forEach(evt => {
        zoom.addEventListener(evt, centerZoom);
      });

      zoom.addEventListener('pointerleave', ev => {
        if(!ev.buttons) centerZoom();
      });

      zoom.addEventListener('keydown', () => { engaged = true; });
      zoom.addEventListener('keyup', centerZoom);
      zoom.addEventListener('blur', centerZoom);
    })();

    // Slider 0..100 → axis -1..1, honoring "virtual_throttle_invert"
    function vjoyThrottle(val){
      const inv = document.getElementById('vth').dataset.invert === 'true';
      const v = parseFloat(val);
      const axis = inv ? (v/50 - 1.0) : (1.0 - v/50);
      fetch('/api/virtual', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({throttle: axis})});
    }

    // Zoom rocker slider: -100..100 → -1..1
    function vjoyZoom(val){
      const axis = Math.max(-1, Math.min(1, parseFloat(val)/100));
      fetch('/api/virtual', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ zaxis: axis })});
    }

    async function vpress(i){ await fetchJSON('/api/virtual/press',   {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({button:i})}); }
    async function vrelease(i){ await fetchJSON('/api/virtual/release', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({button:i})}); }

    const _orig_loadSettings = loadSettings;
    loadSettings = async function(){
      await _orig_loadSettings();
      readBtnIndicesFromForm();
      vjoySyncEnabled();
    };

    vjoySyncEnabled();
    /* ----------------------------------- */

    loadSettings();
    loadFixtures();
    setInterval(refresh, 1000);
    refresh();
    refreshCaptureState();
    if(!CAPTURE_POLL_TIMER){
      CAPTURE_POLL_TIMER = setInterval(refreshCaptureState, 5000);
    }
    </script>
    </body></html>
    """

# ---------------- Routes ----------------

@APP.route("/")
def index():
    return INDEX_HTML

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
