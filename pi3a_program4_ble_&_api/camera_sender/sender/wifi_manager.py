#!/usr/bin/env python3
"""
wifi_manager.py — Wi-Fi auto-connect + Bluetooth (BLE) setup + local control API

Behaviour
---------
1. On boot, give NetworkManager `boot_wait_sec` to join any SAVED Wi-Fi.
2. If nothing joined, scan and try every saved network in range (strongest first).
3. Still not connected -> SETUP MODE: Bluetooth is switched on and the camera
   advertises a BLE service ("MTCam-0042"). The phone pairs with it (system
   popup "Pair with MTCam-0042?", "Just Works" — no PIN), which encrypts the
   link; then the app reads the Wi-Fi list, writes {"ssid","password"} and gets
   the result as a BLE notification. The controller runs in LE-only mode, so no
   classic Bluetooth audio profile can ever be used.
4. Once the camera has Wi-Fi, Bluetooth is switched OFF again (after
   `bt_off_after_setup_sec`), so it stays out of the way of GPIO / audio work.
5. Wi-Fi lost for longer than `lost_grace_sec` -> saved networks are retried,
   then setup mode starts again. In setup mode saved networks are re-tried every
   `retry_known_sec` (covers "power cut, router boots slower than the camera").

Bluetooth can also be controlled by hand (local HTTP API on port 8085, or
start_pi.sh bt-on / bt-off / bt-auto / bt-status):
    on   -> Bluetooth on + advertising (setup available) until set otherwise
    off  -> Bluetooth off, even in setup mode — but if Wi-Fi is down for more than
            `lost_grace_sec` Bluetooth comes back on anyway, so the camera can
            never become unreachable
    auto -> the default rules above

Setup mode on demand:  touch /run/wifi_manager/force_ap  (start_pi.sh wifi-setup,
the local API, or the EC2 "wifi_setup" flag via filesender.py) -> BLE for 10 min,
while the current Wi-Fi keeps working.

Requirements: Raspberry Pi OS Bookworm (NetworkManager / nmcli, BlueZ),
python3-dbus, python3-gi; run as root.
"""

import argparse
import configparser
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Defaults (overridable from the [wifi] section of camera.ini) ────────────
DEFAULTS = {
    "interface": "wlan0",
    "ble_name": "MTCam-{id}",
    "boot_wait_sec": "60",
    "lost_grace_sec": "120",
    "retry_known_sec": "180",
    "bt_off_after_setup_sec": "30",
    "bluetooth_default": "off",          # Bluetooth when Wi-Fi is fine and nothing asked for it
    "control_port": "8085",
    "le_only": "yes",                    # BLE only: classic Bluetooth (audio) switched off
}

SAVED_PREFIX = "camwifi-"                # prefix for profiles created during setup
OLD_HOTSPOT_CON = "CamSetupHotspot"      # profile of the old hotspot mode, removed at start
RUN_DIR = "/run/wifi_manager"
FORCE_AP_FLAG = os.path.join(RUN_DIR, "force_ap")     # name kept for filesender.py
STATE_FILE = os.path.join(RUN_DIR, "state.json")
DNSMASQ_CAPTIVE_CONF = "/etc/NetworkManager/dnsmasq-shared.d/camsetup-captive.conf"
FORCED_SETUP_SEC = 600
API_VERSION = 2

# BLE GATT layout (fixed — the Flutter app uses these UUIDs)
BLE_SERVICE_UUID = "7d2a0001-5c3e-4b6a-9f1e-2a6c0d3e4b10"
BLE_INFO_UUID = "7d2a0002-5c3e-4b6a-9f1e-2a6c0d3e4b10"      # read
BLE_NETWORKS_UUID = "7d2a0003-5c3e-4b6a-9f1e-2a6c0d3e4b10"  # read
BLE_CONNECT_UUID = "7d2a0004-5c3e-4b6a-9f1e-2a6c0d3e4b10"   # write
BLE_STATUS_UUID = "7d2a0005-5c3e-4b6a-9f1e-2a6c0d3e4b10"    # read + notify
BLE_COMMAND_UUID = "7d2a0006-5c3e-4b6a-9f1e-2a6c0d3e4b10"   # write
MAX_NETWORKS = 20

log = logging.getLogger("wifi_manager")


# ════════════════════════════════════════════════════════════════════════════
#  Config
# ════════════════════════════════════════════════════════════════════════════

def load_config(path):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path)
    w = dict(DEFAULTS)
    if cfg.has_section("wifi"):
        for k, v in cfg.items("wifi"):
            w[k] = v.strip()

    # {id} -> last 4 chars of camera_uuid so each camera has a unique name
    cam_uuid = cfg.get("server", "camera_uuid", fallback="").strip()
    short_id = (cam_uuid.replace("-", "")[-4:] or "0000").upper()
    w["camera_uuid"] = cam_uuid
    w["ble_name"] = w["ble_name"].replace("{id}", short_id)[:20]

    for k in ("boot_wait_sec", "lost_grace_sec", "retry_known_sec",
              "bt_off_after_setup_sec", "control_port"):
        w[k] = int(w[k])
    w["bluetooth_default"] = "on" if w["bluetooth_default"].lower() == "on" else "off"
    w["le_only"] = w["le_only"].lower() not in ("no", "0", "false", "off")
    return w


# ════════════════════════════════════════════════════════════════════════════
#  nmcli helpers
# ════════════════════════════════════════════════════════════════════════════

def run(cmd, timeout=60, check=False):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("Timeout: %s", " ".join(cmd))
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "%s not found" % cmd[0]
    if check and p.returncode != 0:
        log.warning("Failed (%s): %s — %s", p.returncode, " ".join(cmd), p.stderr.strip())
    return p.returncode, p.stdout, p.stderr


def split_terse(line):
    """Split an `nmcli -t` line on ':' honouring '\\:' escapes."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class Net:
    def __init__(self, iface):
        self.iface = iface

    # ── state ───────────────────────────────────────────────────────────────
    def device_state(self):
        """Return (state, connection_name) for the Wi-Fi interface."""
        _, out, _ = run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device"])
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) >= 3 and parts[0] == self.iface:
                return parts[1], parts[2]
        return "unknown", ""

    def is_client_connected(self):
        state, con = self.device_state()
        return state == "connected" and con not in ("", OLD_HOTSPOT_CON)

    def current_ssid(self):
        _, out, _ = run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list",
                         "ifname", self.iface, "--rescan", "no"])
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) >= 2 and parts[0] == "yes":
                return parts[1]
        return ""

    def current_ip(self):
        _, out, _ = run(["nmcli", "-g", "IP4.ADDRESS", "device", "show", self.iface])
        first = out.strip().split("|")[0].strip()
        return first.split("/")[0] if first else ""

    # ── scanning / saved profiles ──────────────────────────────────────────
    def scan(self):
        """Return list of dicts {ssid, signal, security}, strongest first, unique SSIDs."""
        _, out, _ = run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi",
                         "list", "ifname", self.iface, "--rescan", "yes"], timeout=40)
        best = {}
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 3 or not parts[0]:
                continue
            ssid, sig, sec = parts[0], parts[1], parts[2]
            try:
                sig = int(sig)
            except ValueError:
                sig = 0
            if ssid not in best or sig > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": sig,
                              "security": sec if sec and sec != "--" else "open"}
        return sorted(best.values(), key=lambda n: -n["signal"])

    def saved_profiles(self):
        """Return {ssid: profile_name} for saved Wi-Fi client profiles."""
        _, out, _ = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
        profiles = {}
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 2 or parts[1] != "802-11-wireless" or parts[0] == OLD_HOTSPOT_CON:
                continue
            name = parts[0]
            _, mode, _ = run(["nmcli", "-g", "802-11-wireless.mode", "connection", "show", name])
            if mode.strip() == "ap":
                continue
            _, ssid, _ = run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", name])
            ssid = ssid.strip()
            if ssid:
                profiles[ssid] = name
        return profiles

    def connect_profile(self, name, wait=45):
        rc, _, err = run(["nmcli", "--wait", str(wait), "connection", "up", name,
                          "ifname", self.iface], timeout=wait + 15)
        if rc != 0:
            log.info("  ✗ %s: %s", name, err.strip().splitlines()[-1] if err.strip() else rc)
            return False
        return self.is_client_connected()

    def try_known_networks(self, scan_result=None):
        """Try every saved network that is currently visible. Returns True on success."""
        saved = self.saved_profiles()
        if not saved:
            log.info("No saved Wi-Fi networks")
            return False
        visible = scan_result if scan_result is not None else self.scan()
        candidates = [n for n in visible if n["ssid"] in saved]
        if not candidates:
            log.info("None of the saved networks are in range (%s)", ", ".join(saved))
            return False
        for n in candidates:
            log.info("Trying saved network '%s' (signal %d%%)…", n["ssid"], n["signal"])
            if self.connect_profile(saved[n["ssid"]]):
                log.info("✅ Connected to '%s'", n["ssid"])
                return True
        return False

    def add_and_connect(self, ssid, password, hidden=False):
        """Create a profile for ssid/password and connect. Returns (ok, message)."""
        name = SAVED_PREFIX + re.sub(r"[^A-Za-z0-9_.-]", "_", ssid)[:40]
        # Replace any earlier profile for the same SSID (e.g. wrong password)
        for s, prof in self.saved_profiles().items():
            if s == ssid:
                run(["nmcli", "connection", "delete", prof])
        cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", self.iface,
               "con-name", name, "ssid", ssid,
               "connection.autoconnect", "yes",
               "connection.autoconnect-priority", "10"]
        if hidden:
            cmd += ["802-11-wireless.hidden", "yes"]
        if password:
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        rc, _, err = run(cmd)
        if rc != 0:
            return False, "Could not save network: " + err.strip()

        log.info("Connecting to new network '%s'…", ssid)
        if self.connect_profile(name, wait=50):
            return True, "Connected to " + ssid
        # Wrong password / out of range: remove so it doesn't block next time
        run(["nmcli", "connection", "delete", name])
        return False, ("Could not connect to “%s”. Check the password and that the "
                       "network is 2.4 GHz and in range." % ssid)


# ════════════════════════════════════════════════════════════════════════════
#  Shared state (read by BLE + local API, written by the manager)
# ════════════════════════════════════════════════════════════════════════════

class SetupState:
    def __init__(self, cfg):
        self.camera_uuid = cfg["camera_uuid"]
        self.ble_name = cfg["ble_name"]
        self.lock = threading.Lock()
        self.networks = []
        self.scanned_at = 0
        self.connect_state = "idle"      # idle | connecting | connected | failed
        self.last_ssid = ""
        self.last_error = ""
        self.wifi_ssid = ""
        self.wifi_ip = ""
        self.wifi_connected = False
        self.setup_active = False
        self.bt_mode = "auto"            # auto | on | off  (manual override)
        self.bt_powered = False
        self.ble_clients = 0
        self.requests = queue.Queue()    # ("connect", ssid, pw, hidden) | ("rescan",)
        self.on_status_change = None     # set by the BLE server -> notify subscribers

    def info(self):
        return {"camera_uuid": self.camera_uuid, "name": self.ble_name,
                "api_version": API_VERSION,
                "wifi": {"connected": self.wifi_connected, "ssid": self.wifi_ssid}}

    def networks_payload(self):
        return {"networks": self.networks[:MAX_NETWORKS], "scanned_at": self.scanned_at}

    def status(self):
        return {"state": self.connect_state, "last_ssid": self.last_ssid,
                "last_error": self.last_error, "wifi_connected": self.wifi_connected,
                "wifi_ssid": self.wifi_ssid, "ip": self.wifi_ip}

    def set_connect(self, state, error=None, ssid=None):
        with self.lock:
            self.connect_state = state
            if error is not None:
                self.last_error = error
            if ssid is not None:
                self.last_ssid = ssid
        self.status_changed()

    def status_changed(self):
        cb = self.on_status_change
        if cb:
            try:
                cb()
            except Exception as e:      # never let a notify problem stop the manager
                log.debug("status notify failed: %s", e)

    def submit_connect(self, body):
        """Validate a setup request (from BLE). Returns None if accepted, else error text."""
        if not isinstance(body, dict):
            return "body must be a JSON object"
        ssid = str(body.get("ssid") or "").strip()
        if not ssid or len(ssid.encode("utf-8")) > 32:
            return "ssid must be 1-32 bytes"
        password = str(body.get("password") or "")
        if password and not (8 <= len(password) <= 63):
            return "password must be 8-63 characters"
        if self.connect_state == "connecting":
            return "already connecting"
        self.set_connect("connecting", error="", ssid=ssid)
        self.requests.put(("connect", ssid, password, bool(body.get("hidden", False))))
        return None

    def submit_command(self, body):
        if not isinstance(body, dict):
            return "body must be a JSON object"
        if body.get("cmd") == "rescan":
            self.requests.put(("rescan",))
            return None
        return "unknown cmd (use \"rescan\")"


# ════════════════════════════════════════════════════════════════════════════
#  Bluetooth power (rfkill) — works without D-Bus
# ════════════════════════════════════════════════════════════════════════════

def bt_set_power(on, le_only=True):
    run(["rfkill", "unblock" if on else "block", "bluetooth"])
    if on:
        if le_only and shutil.which("btmgmt"):
            # LE only (no classic BR/EDR -> no audio profiles), pairing allowed
            for args in (["power", "off"], ["bredr", "off"], ["le", "on"], ["sc", "on"],
                         ["bondable", "on"], ["power", "on"]):
                run(["btmgmt", "--index", "0"] + args, timeout=10)
        run(["bluetoothctl", "power", "on"], timeout=10)
    log.info("🔵 Bluetooth %s", "ON" if on else "OFF")


def bt_is_powered():
    _, out, _ = run(["rfkill", "list", "bluetooth"])
    if "Soft blocked: yes" in out or "Hard blocked: yes" in out:
        return False
    _, out, _ = run(["bluetoothctl", "show"], timeout=10)
    return "Powered: yes" in out


# ════════════════════════════════════════════════════════════════════════════
#  BLE GATT server (BlueZ over D-Bus)
# ════════════════════════════════════════════════════════════════════════════
#
#  Service 7d2a0001-…  advertised as e.g. "MTCam-0042"
#    info      (read)                    {"camera_uuid","name","api_version","wifi":{connected,ssid}}
#    networks  (paired read)             {"networks":[{"ssid","signal","security"}],"scanned_at"}
#    connect   (paired write)            {"ssid","password","hidden"}
#    status    (paired read, notify)     {"state","last_ssid","last_error","wifi_connected","wifi_ssid","ip"}
#    command   (paired write)            {"cmd":"rescan"}
#  All values are UTF-8 JSON. Long reads use offsets; ask for MTU 512 on Android.
#  A bad write is refused with org.bluez.Error.Failed + the reason.

class NullBle:
    """Used when BlueZ / python3-dbus are missing: logs instead of advertising."""
    available = False

    def start(self):
        log.error("BLE not available — install bluez python3-dbus python3-gi "
                  "(sudo ./install_dependencies.sh). Setup mode cannot work.")

    def set_advertising(self, on):
        pass

    def client_count(self):
        return 0


def make_ble_server(state):
    try:
        import dbus
        import dbus.exceptions
        import dbus.mainloop.glib
        import dbus.service
        from gi.repository import GLib
    except ImportError as e:
        log.error("BLE disabled: %s", e)
        return NullBle()

    BLUEZ = "org.bluez"
    OM_IFACE = "org.freedesktop.DBus.ObjectManager"
    PROP_IFACE = "org.freedesktop.DBus.Properties"
    GATT_MGR = "org.bluez.GattManager1"
    ADV_MGR = "org.bluez.LEAdvertisingManager1"
    SVC_IFACE = "org.bluez.GattService1"
    CHRC_IFACE = "org.bluez.GattCharacteristic1"
    ADV_IFACE = "org.bluez.LEAdvertisement1"
    APP_PATH = "/com/medtrain/camsetup"

    class Failed(dbus.exceptions.DBusException):
        _dbus_error_name = "org.bluez.Error.Failed"

    class InvalidArgs(dbus.exceptions.DBusException):
        _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"

    def to_bytes(obj):
        return dbus.Array([dbus.Byte(b) for b in json.dumps(obj, ensure_ascii=False,
                                                            separators=(",", ":")).encode()],
                          signature="y")

    class Application(dbus.service.Object):
        def __init__(self, bus):
            self.services = []
            dbus.service.Object.__init__(self, bus, APP_PATH)

        @dbus.service.method(OM_IFACE, out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            out = {}
            for s in self.services:
                out[s.get_path()] = s.get_properties()
                for c in s.chars:
                    out[c.get_path()] = c.get_properties()
            return out

    class Service(dbus.service.Object):
        def __init__(self, bus, uuid):
            self.path = APP_PATH + "/service0"
            self.uuid, self.chars = uuid, []
            dbus.service.Object.__init__(self, bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def get_properties(self):
            return {SVC_IFACE: {"UUID": self.uuid, "Primary": True,
                                "Characteristics": dbus.Array(
                                    [c.get_path() for c in self.chars], signature="o")}}

        @dbus.service.method(PROP_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface != SVC_IFACE:
                raise InvalidArgs()
            return self.get_properties()[SVC_IFACE]

    class Characteristic(dbus.service.Object):
        def __init__(self, bus, index, uuid, flags, service, read_fn=None, write_fn=None):
            self.path = service.path + "/char%d" % index
            self.uuid, self.flags, self.service = uuid, flags, service
            self.read_fn, self.write_fn = read_fn, write_fn
            self.notifying = False
            self.buf = bytearray()
            dbus.service.Object.__init__(self, bus, self.path)
            service.chars.append(self)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def get_properties(self):
            return {CHRC_IFACE: {"Service": self.service.get_path(), "UUID": self.uuid,
                                 "Flags": dbus.Array(self.flags, signature="s")}}

        @dbus.service.method(PROP_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface != CHRC_IFACE:
                raise InvalidArgs()
            return self.get_properties()[CHRC_IFACE]

        @dbus.service.method(CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
        def ReadValue(self, options):
            if not self.read_fn:
                raise Failed("not readable")
            data = to_bytes(self.read_fn())
            return data[int(options.get("offset", 0)):]

        @dbus.service.method(CHRC_IFACE, in_signature="aya{sv}")
        def WriteValue(self, value, options):
            if not self.write_fn:
                raise Failed("not writable")
            offset = int(options.get("offset", 0))
            if offset == 0:
                self.buf = bytearray()
            self.buf[offset:offset + len(value)] = bytes(value)
            if len(self.buf) > 1024:
                self.buf = bytearray()
                raise Failed("value too long")
            try:
                body = json.loads(self.buf.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return          # part of a long write — wait for the rest
            self.buf = bytearray()
            err = self.write_fn(body)
            if err:
                raise Failed(err)

        @dbus.service.method(CHRC_IFACE)
        def StartNotify(self):
            self.notifying = True

        @dbus.service.method(CHRC_IFACE)
        def StopNotify(self):
            self.notifying = False

        @dbus.service.signal(PROP_IFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        def notify(self):
            if self.notifying and self.read_fn:
                self.PropertiesChanged(CHRC_IFACE, {"Value": to_bytes(self.read_fn())}, [])

    AGENT_IFACE = "org.bluez.Agent1"

    class Rejected(dbus.exceptions.DBusException):
        _dbus_error_name = "org.bluez.Error.Rejected"

    class Agent(dbus.service.Object):
        """NoInputNoOutput agent: accepts "Just Works" pairing while setup is on."""
        def __init__(self, bus, allow):
            self.path = APP_PATH + "/agent"
            self.allow = allow
            dbus.service.Object.__init__(self, bus, self.path)

        def _check(self, what, device):
            if not self.allow():
                log.info("Pairing refused (%s) — setup is not active", what)
                raise Rejected("setup not active")
            log.info("🔐 Paired (%s) %s", what, str(device).split("/")[-1])

        @dbus.service.method(AGENT_IFACE)
        def Release(self):
            pass

        @dbus.service.method(AGENT_IFACE, in_signature="o")
        def RequestAuthorization(self, device):
            self._check("just works", device)

        @dbus.service.method(AGENT_IFACE, in_signature="ou")
        def RequestConfirmation(self, device, passkey):
            self._check("confirm", device)

        @dbus.service.method(AGENT_IFACE, in_signature="os")
        def AuthorizeService(self, device, uuid):
            self._check("service", device)

        @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            raise Rejected("no PIN on this device")

        @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            raise Rejected("no passkey on this device")

        @dbus.service.method(AGENT_IFACE, in_signature="ouq")
        def DisplayPasskey(self, device, passkey, entered):
            pass

        @dbus.service.method(AGENT_IFACE, in_signature="os")
        def DisplayPinCode(self, device, pincode):
            pass

        @dbus.service.method(AGENT_IFACE)
        def Cancel(self):
            pass

    class Advertisement(dbus.service.Object):
        def __init__(self, bus, name, on_release=None):
            self.path = APP_PATH + "/adv0"
            self.name = name
            self.on_release = on_release
            dbus.service.Object.__init__(self, bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        @dbus.service.method(PROP_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface != ADV_IFACE:
                raise InvalidArgs()
            return {"Type": "peripheral",
                    "ServiceUUIDs": dbus.Array([BLE_SERVICE_UUID], signature="s"),
                    "LocalName": dbus.String(self.name)}

        @dbus.service.method(ADV_IFACE)
        def Release(self):
            log.info("BLE advertisement released by BlueZ")
            if self.on_release:
                self.on_release()

    class BleServer:
        available = True

        def __init__(self):
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self.bus = dbus.SystemBus()
            self.loop = GLib.MainLoop()
            self.app = Application(self.bus)
            svc = Service(self.bus, BLE_SERVICE_UUID)
            self.app.services.append(svc)
            # info is readable before pairing; the rest needs a paired (encrypted)
            # link, so the phone shows its pairing popup on first access
            rd, wr = ["encrypt-read"], ["encrypt-write"]
            Characteristic(self.bus, 0, BLE_INFO_UUID, ["read"], svc, read_fn=state.info)
            Characteristic(self.bus, 1, BLE_NETWORKS_UUID, rd, svc,
                           read_fn=state.networks_payload)
            Characteristic(self.bus, 2, BLE_CONNECT_UUID, wr, svc,
                           write_fn=state.submit_connect)
            self.status_chr = Characteristic(self.bus, 3, BLE_STATUS_UUID, rd + ["notify"],
                                             svc, read_fn=state.status)
            Characteristic(self.bus, 4, BLE_COMMAND_UUID, wr, svc,
                           write_fn=state.submit_command)
            self.adv = Advertisement(self.bus, state.ble_name,
                                     on_release=lambda: setattr(self, "advertising", False))
            self.advertising = False
            self.app_registered = False
            self.adapter_path = None
            self.agent = Agent(self.bus, allow=lambda: self.advertising)
            self.agent_registered = False
            state.on_status_change = lambda: GLib.idle_add(self._notify)

        def _notify(self):
            self.status_chr.notify()
            return False

        # everything that touches D-Bus runs in the GLib thread
        def call(self, fn, timeout=10):
            done, box = threading.Event(), {}

            def job():
                try:
                    box["v"] = fn()
                except Exception as e:
                    box["e"] = e
                done.set()
                return False
            GLib.idle_add(job)
            if not done.wait(timeout):
                raise TimeoutError("D-Bus call timed out")
            if "e" in box:
                raise box["e"]
            return box.get("v")

        def _adapter(self):
            om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OM_IFACE)
            for path, ifaces in om.GetManagedObjects().items():
                if GATT_MGR in ifaces and ADV_MGR in ifaces:
                    return path
            raise RuntimeError("no Bluetooth LE adapter found")

        def start(self):
            threading.Thread(target=self.loop.run, daemon=True, name="glib").start()
            log.info("BLE server ready (service %s)", BLE_SERVICE_UUID)

        def set_advertising(self, on):
            # right after rfkill unblock the adapter can take a few seconds to appear
            for attempt in range(5 if on else 1):
                try:
                    self.call(lambda: self._set_adv(on), timeout=15)
                    return
                except Exception as e:
                    last = e
                    time.sleep(2)
            log.error("BLE %s failed: %s", "start" if on else "stop", last)
            self.advertising = self.app_registered = False
            self.adapter_path = None

        def _set_adv(self, on):
            # Every BlueZ call is async: the GLib thread must stay free, because
            # BlueZ calls back into our objects (GetManagedObjects, GetAll) meanwhile.
            if not self.adapter_path:
                self.adapter_path = self._adapter()
            obj = self.bus.get_object(BLUEZ, self.adapter_path)
            props = dbus.Interface(obj, PROP_IFACE)
            quiet = dict(reply_handler=lambda *a: None,
                         error_handler=lambda e: log.debug("BlueZ: %s", e))
            if on:
                props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True), **quiet)
                props.Set("org.bluez.Adapter1", "Alias", dbus.String(state.ble_name), **quiet)
                # LE data only: no classic pairing / discoverable -> no audio profiles
                # LE pairing allowed while setup is on; never classic-discoverable
                props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True), **quiet)
                props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(False), **quiet)
                if not self.agent_registered:
                    self.agent_registered = True
                    mgr = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"),
                                         "org.bluez.AgentManager1")
                    mgr.RegisterAgent(self.agent.path, "NoInputNoOutput",
                                      reply_handler=lambda: mgr.RequestDefaultAgent(
                                          self.agent.path, **quiet),
                                      error_handler=self._agent_failed)
                if not self.app_registered:
                    self.app_registered = True
                    dbus.Interface(obj, GATT_MGR).RegisterApplication(
                        dbus.ObjectPath(APP_PATH), {},
                        reply_handler=lambda: log.info("BLE GATT service registered"),
                        error_handler=self._gatt_failed)
                if not self.advertising:
                    self.advertising = True
                    dbus.Interface(obj, ADV_MGR).RegisterAdvertisement(
                        self.adv.get_path(), {},
                        reply_handler=lambda: log.info("📡 BLE advertising as '%s'",
                                                       state.ble_name),
                        error_handler=self._adv_failed)
            else:
                props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(False), **quiet)
                if self.advertising:
                    self.advertising = False
                    dbus.Interface(obj, ADV_MGR).UnregisterAdvertisement(
                        self.adv.get_path(), **quiet)
                if self.app_registered:
                    self.app_registered = False
                    dbus.Interface(obj, GATT_MGR).UnregisterApplication(
                        dbus.ObjectPath(APP_PATH), **quiet)

        def _agent_failed(self, e):
            if "AlreadyExists" not in str(e):
                log.error("Pairing agent register failed: %s", e)
                self.agent_registered = False

        def _gatt_failed(self, e):
            log.error("GATT register failed: %s", e)
            self.app_registered = False

        def _adv_failed(self, e):
            log.error("BLE advertise failed: %s", e)
            self.advertising = False

        def client_count(self):
            def count():
                om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OM_IFACE)
                return sum(1 for ifaces in om.GetManagedObjects().values()
                           if ifaces.get("org.bluez.Device1", {}).get("Connected", False))
            try:
                return int(self.call(count, timeout=5))
            except Exception:
                return 0

    try:
        return BleServer()
    except Exception as e:
        log.error("BLE disabled (is bluetoothd running?): %s", e)
        return NullBle()


# ════════════════════════════════════════════════════════════════════════════
#  Local control API (HTTP on port 8085, home network + localhost)
# ════════════════════════════════════════════════════════════════════════════
#
#   GET  /api/status        -> wifi, bluetooth, setup state
#   POST /api/bluetooth     <- {"state": "on" | "off" | "auto"}
#   POST /api/setup-mode    <- {"minutes": 10}            (1-60)

class ControlApi:
    def __init__(self, manager, port):
        self.manager, self.port = manager, port
        self.httpd = None

    def start(self):
        mgr = self.manager

        class Handler(BaseHTTPRequestHandler):
            server_version = "CamControl/%d" % API_VERSION

            def log_message(self, fmt, *args):
                log.debug("api: " + fmt, *args)

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError
                return body

            def do_GET(self):
                if self.path.split("?")[0].rstrip("/") == "/api/status":
                    return self._json(200, mgr.status_report())
                self._json(404, {"error": "not found"})

            def do_POST(self):
                path = self.path.split("?")[0].rstrip("/")
                try:
                    body = self._body()
                except (ValueError, UnicodeDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                if path == "/api/bluetooth":
                    st = str(body.get("state", "")).lower()
                    if st not in ("on", "off", "auto"):
                        return self._json(400, {"error": "state must be on, off or auto"})
                    mgr.request_bt_mode(st)
                    return self._json(200, mgr.status_report())
                if path == "/api/setup-mode":
                    try:
                        minutes = int(body.get("minutes", 10))
                    except (TypeError, ValueError):
                        minutes = 0
                    if not 1 <= minutes <= 60:
                        return self._json(400, {"error": "minutes must be 1-60"})
                    mgr.request_setup(minutes * 60)
                    return self._json(200, mgr.status_report())
                self._json(404, {"error": "not found"})

        for _ in range(10):
            try:
                self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
                break
            except OSError as e:
                log.warning("Control API bind failed (%s), retrying…", e)
                time.sleep(2)
        if not self.httpd:
            log.error("Could not start control API on port %d", self.port)
            return
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="api").start()
        log.info("🌐 Control API on port %d", self.port)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


# ════════════════════════════════════════════════════════════════════════════
#  Manager (state machine)
# ════════════════════════════════════════════════════════════════════════════

class WifiManager:
    def __init__(self, cfg, net=None, ble=None, bt_power=None):
        self.cfg = cfg
        self.net = net or Net(cfg["interface"])
        self.state = SetupState(cfg)
        self.ble = ble if ble is not None else make_ble_server(self.state)
        self.bt_power = bt_power or (lambda on: bt_set_power(on, cfg["le_only"]))
        self.api = ControlApi(self, cfg["control_port"])
        self.lock = threading.Lock()
        self.bt_lock = threading.Lock()   # API thread and loop both apply Bluetooth
        self.setup_until = 0          # forced / lingering setup window
        self.lost_since = None        # when Wi-Fi was last seen going down
        self.need_setup = False       # no Wi-Fi after retries -> setup indefinitely
        self.bt_on = None             # what we last applied (None = unknown)
        self.last_retry = time.time()

    # ── requests from the API / start_pi.sh / filesender ──────────────────
    def request_bt_mode(self, mode):
        with self.lock:
            self.state.bt_mode = mode
        log.info("Bluetooth mode set to '%s'", mode)
        self.apply_bluetooth()

    def request_setup(self, seconds=FORCED_SETUP_SEC):
        with self.lock:
            self.setup_until = time.time() + seconds
            if self.state.bt_mode == "off":
                self.state.bt_mode = "auto"   # asking for setup implies Bluetooth
        log.info("Setup mode requested for %d min", seconds // 60)
        self.apply_bluetooth()

    # ── Bluetooth policy ────────────────────────────────────────────────────
    def wifi_down_too_long(self):
        return (self.lost_since is not None and
                time.time() - self.lost_since > self.cfg["lost_grace_sec"])

    def setup_active(self):
        return self.need_setup or time.time() < self.setup_until

    def desired_bt(self):
        """(bluetooth on?, reason)"""
        mode = self.state.bt_mode
        if self.need_setup or self.wifi_down_too_long():
            return True, "no Wi-Fi"           # safety: always reachable for setup
        if mode in ("on", "off"):
            return mode == "on", "manual"
        if time.time() < self.setup_until:
            return True, "setup window"
        return self.cfg["bluetooth_default"] == "on", "default"

    def apply_bluetooth(self):
        with self.bt_lock:
            self._apply_bluetooth()

    def _apply_bluetooth(self):
        want, why = self.desired_bt()
        if want == self.bt_on:
            return
        log.info("Bluetooth → %s (%s)", "on" if want else "off", why)
        if want:
            self.bt_power(True)
            time.sleep(1)
            self.ble.set_advertising(True)
        else:
            self.ble.set_advertising(False)
            self.bt_power(False)
        self.bt_on = want
        self.state.bt_powered = want
        self.state.setup_active = want
        self.write_state()

    # ── housekeeping ────────────────────────────────────────────────────────
    def prepare_system(self):
        if not shutil.which("nmcli"):
            log.error("nmcli not found — this needs NetworkManager (Raspberry Pi OS Bookworm). "
                      "On Bullseye: sudo raspi-config → Advanced → Network Config → NetworkManager")
            sys.exit(1)
        os.makedirs(RUN_DIR, exist_ok=True)
        os.chmod(RUN_DIR, 0o1777)   # lets filesender.py (normal user) drop force_ap
        run(["rfkill", "unblock", "wifi"])
        run(["nmcli", "radio", "wifi", "on"])
        # Remove leftovers of the old hotspot mode
        run(["nmcli", "connection", "delete", OLD_HOTSPOT_CON])
        if os.path.exists(DNSMASQ_CAPTIVE_CONF):
            try:
                os.remove(DNSMASQ_CAPTIVE_CONF)
            except OSError:
                pass

    def refresh_wifi(self):
        connected = self.net.is_client_connected()
        changed = connected != self.state.wifi_connected
        self.state.wifi_connected = connected
        if connected:
            self.state.wifi_ssid = self.net.current_ssid()
            self.state.wifi_ip = self.net.current_ip()
            self.lost_since = None
            self.need_setup = False
        else:
            self.state.wifi_ssid = self.state.wifi_ip = ""
            if self.lost_since is None:
                self.lost_since = time.time()
        if changed:
            log.info("Wi-Fi %s%s", "connected" if connected else "disconnected",
                     " to '%s'" % self.state.wifi_ssid if connected else "")
            self.state.status_changed()
            self.write_state()
        return connected

    def rescan(self):
        self.state.networks = self.net.scan()
        self.state.scanned_at = int(time.time())
        return self.state.networks

    def write_state(self):
        data = self.status_report()
        data["time"] = int(time.time())
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def status_report(self):
        s = self.state
        return {
            "camera_uuid": s.camera_uuid,
            "wifi": {"connected": s.wifi_connected, "ssid": s.wifi_ssid, "ip": s.wifi_ip},
            "bluetooth": {"powered": bool(self.bt_on), "mode": s.bt_mode,
                          "advertising": bool(self.bt_on), "name": s.ble_name,
                          "reason": self.desired_bt()[1]},
            "setup": {"active": bool(self.bt_on), "state": s.connect_state,
                      "last_ssid": s.last_ssid, "last_error": s.last_error,
                      "until": int(self.setup_until) if time.time() < self.setup_until else None},
        }

    def force_ap_requested(self):
        if os.path.exists(FORCE_AP_FLAG):
            try:
                os.remove(FORCE_AP_FLAG)
            except OSError:
                pass
            return True
        return False

    # ── setup requests ──────────────────────────────────────────────────────
    def handle_connect(self, ssid, password, hidden):
        ok, msg = self.net.add_and_connect(ssid, password, hidden)
        self.refresh_wifi()
        if ok:
            log.info("✅ %s (%s)", msg, self.state.wifi_ip)
            self.need_setup = False
            # keep BLE up briefly so the app receives the "connected" notification
            self.setup_until = time.time() + self.cfg["bt_off_after_setup_sec"]
            self.state.set_connect("connected", error="")
        else:
            log.warning("✗ %s", msg)
            self.state.set_connect("failed", error=msg)
            if not self.state.wifi_connected and self.net.try_known_networks(self.rescan()):
                self.refresh_wifi()

    def process_request(self, req):
        if req[0] == "connect":
            self.handle_connect(*req[1:])
        elif req[0] == "rescan":
            self.rescan()
            log.info("Rescanned: %d networks", len(self.state.networks))
            self.state.status_changed()

    # ── main loop ───────────────────────────────────────────────────────────
    def step(self):
        """One pass of the loop (separate for testing)."""
        if self.force_ap_requested():
            self.rescan()
            self.request_setup(FORCED_SETUP_SEC)

        connected = self.refresh_wifi()

        if not connected and not self.need_setup and self.wifi_down_too_long():
            log.info("Wi-Fi down for %ds — trying saved networks",
                     int(time.time() - self.lost_since))
            if self.net.try_known_networks(self.rescan()):
                self.refresh_wifi()
            else:
                log.info("No Wi-Fi — starting Bluetooth setup mode")
                self.need_setup = True
            self.last_retry = time.time()

        if (self.need_setup and not self.state.wifi_connected and
                time.time() - self.last_retry > self.cfg["retry_known_sec"]):
            self.last_retry = time.time()
            if self.ble.client_count() == 0 and self.net.saved_profiles():
                log.info("Re-checking saved networks…")
                if self.net.try_known_networks(self.rescan()):
                    self.refresh_wifi()

        self.apply_bluetooth()

    def run(self):
        self.prepare_system()
        self.ble.start()
        self.api.start()
        self.bt_power(False)          # start from a known state
        self.bt_on = False
        self.write_state()

        log.info("Waiting up to %ds for a saved Wi-Fi…", self.cfg["boot_wait_sec"])
        deadline = time.time() + self.cfg["boot_wait_sec"]
        while time.time() < deadline and not self.net.is_client_connected():
            if os.path.exists(FORCE_AP_FLAG):
                break
            time.sleep(3)

        if self.refresh_wifi():
            log.info("✅ Connected to '%s' (%s)", self.state.wifi_ssid, self.state.wifi_ip)
        else:
            self.rescan()
            if self.net.try_known_networks(self.state.networks):
                self.refresh_wifi()
            else:
                log.info("No Wi-Fi — starting Bluetooth setup mode")
                self.need_setup = True
        self.last_retry = time.time()

        while True:
            self.step()
            try:
                req = self.state.requests.get(timeout=5)
            except queue.Empty:
                req = None
            if req:
                self.process_request(req)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Wi-Fi auto-connect with Bluetooth setup")
    ap.add_argument("--config", default=os.path.join(here, "camera.ini"))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s - wifi_manager - %(levelname)s - %(message)s")
    if os.geteuid() != 0:
        log.error("Run as root (sudo) — nmcli and Bluetooth need it")
        sys.exit(1)

    cfg = load_config(args.config)
    log.info("Interface=%s  BLE name='%s'  control API port=%d", cfg["interface"],
             cfg["ble_name"], cfg["control_port"])
    WifiManager(cfg).run()


if __name__ == "__main__":
    main()
