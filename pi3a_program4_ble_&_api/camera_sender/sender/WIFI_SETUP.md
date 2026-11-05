# Wi-Fi setup over Bluetooth (BLE) + Bluetooth control

When the camera has no Wi-Fi, it switches Bluetooth on and advertises a **BLE**
service. The phone **pairs** with it (system popup "Pair with MTCam-0042?", no
PIN), which encrypts the Bluetooth link. Then the app reads the Wi-Fi list, sends
the network name and password as plain JSON, and gets the result back as a
notification. Once the
camera is online, Bluetooth switches **off** again.

The Pi runs Bluetooth **Low Energy only** — classic Bluetooth, where phone audio
lives, is switched off — so a phone connecting can never move the Pi's audio.

The camera does **not** appear in the phone's Bluetooth settings: BLE devices are
found by apps (the Flutter app, or nRF Connect for testing).

> Simple first version: any phone near the camera can pair and set its Wi-Fi while
> Bluetooth is on (pairing is refused when setup is off), and anyone on the same
> network can use the control API.

## Install / update (once per camera)

    sudo ~/install_dependencies.sh        # installs bluez, python3-dbus, python3-gi too

`camera.ini` is not in the zip. The zip ships `camera.ini.example`; the camera's
own `camera.ini` is created from it only if missing and is never overwritten.
A new camera: set `[camera] type` and `[server] camera_uuid`.

## When Bluetooth is on

| Situation | Bluetooth |
|---|---|
| Wi-Fi works, nothing asked for Bluetooth | **off** |
| No saved Wi-Fi works (about 60 s after boot, or Wi-Fi down for more than 2 min) | on, until Wi-Fi works |
| `./start_pi.sh wifi-setup`, `POST /api/setup-mode`, or EC2 `wifi_setup` flag | on for 10 min; the current Wi-Fi keeps working |
| Setup succeeded | stays on 30 s (so the app gets the result), then off |
| `bt-on` | on until `bt-off` / `bt-auto` |
| `bt-off` | off, **except** when Wi-Fi is down for more than 2 min: then it comes back on, so the camera can't end up unreachable |
| `bt-auto` | back to the rules above |

The manual mode is not kept across a reboot (it starts as `auto`).

## start_pi.sh

    ./start_pi.sh wifi         # Wi-Fi + Bluetooth status
    ./start_pi.sh bt-on        # Bluetooth on (setup available)
    ./start_pi.sh bt-off       # Bluetooth off
    ./start_pi.sh bt-auto      # automatic
    ./start_pi.sh bt-status    # full status as JSON
    ./start_pi.sh bt-forget    # remove all phones paired with the camera
    ./start_pi.sh wifi-setup   # Bluetooth setup mode for 10 min
    ./start_pi.sh logs-wifi

## BLE service (for the Flutter app)

Advertised name `MTCam-XXXX` (last 4 of camera_uuid), service UUID
`7d2a0001-5c3e-4b6a-9f1e-2a6c0d3e4b10`. All values are UTF-8 JSON.

| Characteristic | UUID | Props | Value |
|---|---|---|---|
| info | `7d2a0002-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read | `{"camera_uuid","name","api_version":2,"wifi":{"connected","ssid"}}` |
| networks | `7d2a0003-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read (paired) | `{"networks":[{"ssid","signal","security"}],"scanned_at"}` (max 20, strongest first) |
| connect | `7d2a0004-5c3e-4b6a-9f1e-2a6c0d3e4b10` | write (paired) | `{"ssid":"Megh 300","password":"…","hidden":false}` |
| status | `7d2a0005-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read, notify (paired) | `{"state","last_ssid","last_error","wifi_connected","wifi_ssid","ip"}` |
| command | `7d2a0006-5c3e-4b6a-9f1e-2a6c0d3e4b10` | write (paired) | `{"cmd":"rescan"}` → re-scan, then a status notification |

"Paired" characteristics make the phone pair on first access: the OS shows its
popup automatically (Just Works, LE Secure Connections). On Android the user can
also pair first from Settings → Pair new device. A paired phone reconnects later
without the popup.

`state`: `idle`, `connecting`, `connected` (with `ip`) or `failed` (with `last_error`).

**connect** fields: `ssid` (required, 1–32 bytes), `password` (empty or left out for
an open network, otherwise 8–63 characters), `hidden` (optional, default `false`).

A refused write returns `org.bluez.Error.Failed` with the reason: `ssid must be
1-32 bytes`, `password must be 8-63 characters`, `already connecting`,
`body must be a JSON object` or `value too long` (over 1024 bytes).

### App flow
1. Scan for BLE devices advertising the service UUID (or named `MTCam-…`); connect.
2. Read **info** to see which camera it is (`camera_uuid`).
3. Subscribe to **status** notifications → the phone shows the pairing popup → user
   taps **Pair**. Android: request MTU 512.
4. Read **networks**; the user picks one and types the password.
5. Write **connect**. Wait for a **status** notification with `state` `connected`
   (show `ip`) or `failed` (show `last_error`), about 10–50 s.

## Local control API (home network)

`http://<camera-ip>:8085` — the camera's IP is in `./start_pi.sh wifi`.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/api/status` | — | `{"camera_uuid","wifi":{connected,ssid,ip},"bluetooth":{powered,mode,advertising,name,reason},"setup":{active,state,last_ssid,last_error,until}}` |
| POST | `/api/bluetooth` | `{"state":"on"}` / `"off"` / `"auto"` | `200` + status |
| POST | `/api/setup-mode` | `{"minutes":10}` (1–60) | `200` + status |

Errors: `400 {"error":…}` bad body or value, `404` unknown path.

## EC2 contract (unchanged)
- `GET /camera/mode/<uuid>` may include `"wifi_setup": true` → camera calls
  `POST /camera/wifi-setup/ack` and starts Bluetooth setup for 10 min.
