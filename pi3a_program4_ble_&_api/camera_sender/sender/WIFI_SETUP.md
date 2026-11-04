# Wi-Fi setup over Bluetooth (BLE) + Bluetooth control

The camera no longer makes a Wi-Fi hotspot. When it has no Wi-Fi, it switches
Bluetooth on and advertises a **BLE** service. The Flutter app connects over BLE,
reads the Wi-Fi list, sends the network name and password, and gets the result
back as a notification. Once the camera is online, Bluetooth switches **off**
again, so it stays out of the way of GPIO and audio work.

BLE is data only: no pairing and no classic Bluetooth audio profile, so
connecting a phone never moves the Pi's audio over to the phone.

## Install / update (once per camera)

    sudo ~/install_dependencies.sh        # installs bluez, python3-dbus, python3-gi too
    ./start_pi.sh qr                       # print the new QR sticker (format changed: v2)

`camera.ini` is no longer in the zip. The zip ships `camera.ini.example`; the
camera's own `camera.ini` is created from it only if missing and is never
overwritten. A new camera: set `[camera] type` and `[server] camera_uuid`.

Old hotspot keys in an existing `[wifi]` section are simply ignored.

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
    ./start_pi.sh wifi-setup   # Bluetooth setup mode for 10 min
    ./start_pi.sh qr           # QR sticker
    ./start_pi.sh logs-wifi

## QR sticker (v2)

    {"v":2,"uuid":"<camera_uuid>","ble":"MTCam-0042","key":"<setup key>"}

`key` is created once per camera in `/etc/wifi_manager/setup_key` (an existing
hotspot password is reused, so the secret on old stickers stays the same).

## BLE service (for the Flutter app)

Advertised name `MTCam-XXXX` (last 4 of camera_uuid), service UUID
`7d2a0001-5c3e-4b6a-9f1e-2a6c0d3e4b10`. All values are UTF-8 JSON.

| Characteristic | UUID | Props | Value |
|---|---|---|---|
| info | `7d2a0002-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read | `{"camera_uuid","name","api_version":2,"wifi":{"connected","ssid"}}` |
| networks | `7d2a0003-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read | `{"networks":[{"ssid","signal","security"}],"scanned_at"}` (max 20, strongest first) |
| connect | `7d2a0004-5c3e-4b6a-9f1e-2a6c0d3e4b10` | write | `{"ssid","password","hidden":false,"key"}` |
| status | `7d2a0005-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read, notify | `{"state","last_ssid","last_error","wifi_connected","wifi_ssid","ip"}` |
| command | `7d2a0006-5c3e-4b6a-9f1e-2a6c0d3e4b10` | write | `{"cmd":"rescan","key"}` → re-scan; read networks again after ~10 s |

`state`: `idle`, `connecting`, `connected` (with `ip`) or `failed` (with `last_error`).

A refused write returns a GATT error: `org.bluez.Error.NotAuthorized` for a wrong
`key`, `org.bluez.Error.Failed` with the reason for bad input (SSID 1–32 bytes;
password empty for an open network, else 8–63 characters; already connecting).
After 5 wrong keys, setup writes are ignored for 60 s.

App flow:
1. Scan the QR code → `uuid`, `ble`, `key`.
2. Scan for BLE devices advertising the service UUID, connect, read **info**, and
   check `camera_uuid` matches the QR (try the next device if it doesn't).
3. Android: request MTU 512. Subscribe to **status** notifications.
4. Read **networks**, let the user pick one and type the password.
5. Write **connect** with the `key`. Wait for a **status** notification with
   `state` `connected` (show `ip`) or `failed` (show `last_error`), about 10–50 s.
   The BLE link stays up during this, so no EC2 polling is needed.

If the camera already has Wi-Fi (setup started from `wifi-setup` or EC2), it keeps
working while the app sends a new network; the camera switches when the new one
connects.

## Local control API (home network)

`http://<camera-ip>:8085` — the camera's IP is in `./start_pi.sh wifi`.
Header `X-Api-Key: <setup key>` is required, except from the Pi itself.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/api/status` | — | `{"camera_uuid","wifi":{connected,ssid,ip},"bluetooth":{powered,mode,advertising,name,reason},"setup":{active,state,last_ssid,last_error,until}}` |
| POST | `/api/bluetooth` | `{"state":"on"}` / `"off"` / `"auto"` | `200` + status |
| POST | `/api/setup-mode` | `{"minutes":10}` (1–60) | `200` + status |

Errors: `400 {"error":…}` bad body or value, `401` missing/wrong key, `404` unknown path.

## EC2 contract (unchanged)
- `GET /camera/mode/<uuid>` may include `"wifi_setup": true` → camera calls
  `POST /camera/wifi-setup/ack` and starts Bluetooth setup for 10 min.
