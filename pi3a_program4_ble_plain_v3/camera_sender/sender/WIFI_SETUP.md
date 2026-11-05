# Wi-Fi setup over Bluetooth (BLE) + Bluetooth control

The camera no longer makes a Wi-Fi hotspot. When it has no Wi-Fi, it switches
Bluetooth on and advertises a **BLE** service. The Flutter app connects over BLE,
reads the Wi-Fi list, sends the network name and password, and gets the result
back as a notification. Once the camera is online, Bluetooth switches **off**
again, so it stays out of the way of GPIO and audio work.

The phone pairs with the camera over BLE (a system popup: "Pair with
MTCam-0042?"), so the link is encrypted. The app then sends the Wi-Fi name and
password as plain JSON. The Pi
runs Bluetooth **Low Energy only** — classic Bluetooth, where phone audio lives,
is switched off — so pairing can never move the Pi's audio over to the phone.

The camera does **not** appear in the phone's Bluetooth settings: BLE devices are
found by apps (the Flutter app, or nRF Connect for testing).

## Install / update (once per camera)

    sudo ~/install_dependencies.sh        # installs bluez, python3-dbus, python3-gi too

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
    ./start_pi.sh bt-forget    # remove all phones paired with the camera
    ./start_pi.sh wifi-setup   # Bluetooth setup mode for 10 min
    ./start_pi.sh logs-wifi

## BLE service (for the Flutter app)

Advertised name `MTCam-XXXX` (last 4 of camera_uuid), service UUID
`7d2a0001-5c3e-4b6a-9f1e-2a6c0d3e4b10`. All values are UTF-8 JSON.

| Characteristic | UUID | Props | Value |
|---|---|---|---|
| info | `7d2a0002-5c3e-4b6a-9f1e-2a6c0d3e4b10` | read (no pairing needed) | `{"camera_uuid","name","api_version":2,"wifi":{"connected","ssid"}}` |
| networks | `7d2a0003-5c3e-4b6a-9f1e-2a6c0d3e4b10` | encrypted read | `{"networks":[{"ssid","signal","security"}],"scanned_at"}` (max 20, strongest first) |
| connect | `7d2a0004-5c3e-4b6a-9f1e-2a6c0d3e4b10` | encrypted write | see below |
| status | `7d2a0005-5c3e-4b6a-9f1e-2a6c0d3e4b10` | encrypted read, notify | `{"state","last_ssid","last_error","wifi_connected","wifi_ssid","ip"}` |
| command | `7d2a0006-5c3e-4b6a-9f1e-2a6c0d3e4b10` | encrypted write | `{"cmd":"rescan"}` → re-scan, then a status notification |

"Encrypted" characteristics make the phone pair first: the OS shows its pairing
popup automatically on the first access ("Just Works", LE Secure Connections).
Pairing is accepted only while setup mode is on.

`state`: `idle`, `connecting`, `connected` (with `ip`) or `failed` (with `last_error`).

### connect — what the app writes

    {"ssid":"Megh 300","password":"yourWifiPassword","hidden":false}

- `ssid` required, 1–32 bytes
- `password` empty or left out for an open network, otherwise 8–63 characters
- `hidden` optional, default `false`

(No key yet: any phone that pairs while setup is on can set the Wi-Fi. A key and
payload encryption will be added in a later step.)

A refused write returns `org.bluez.Error.Failed` with the reason: `ssid must be
1-32 bytes`, `password must be 8-63 characters`, `already connecting`,
`body must be a JSON object` or `value too long` (over 1024 bytes).

### App flow
1. Scan for BLE devices advertising the service UUID (or named `MTCam-…`); connect.
2. Read **info** to see which camera it is (`camera_uuid`).
3. Subscribe to **status** notifications → the phone shows the pairing popup → user taps Pair.
4. Android: request MTU 512. Read **networks**; the user picks one and types the password.
5. Write **connect**. Wait for a **status** notification with
   `state` `connected` (show `ip`) or `failed` (show `last_error`), about 10–50 s.
   The BLE link stays up during this, so no EC2 polling is needed.

If the camera already has Wi-Fi (setup started from `wifi-setup` or EC2), it keeps
working while the app sends a new network; the camera switches when the new one
connects. A phone paired once reconnects without the popup; `./start_pi.sh bt-forget`
clears all pairings.

## Local control API (home network)

`http://<camera-ip>:8085` — the camera's IP is in `./start_pi.sh wifi`.
No key needed.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/api/status` | — | `{"camera_uuid","wifi":{connected,ssid,ip},"bluetooth":{powered,mode,advertising,name,reason},"setup":{active,state,last_ssid,last_error,until}}` |
| POST | `/api/bluetooth` | `{"state":"on"}` / `"off"` / `"auto"` | `200` + status |
| POST | `/api/setup-mode` | `{"minutes":10}` (1–60) | `200` + status |

Errors: `400 {"error":…}` bad body or value, `404` unknown path.

## EC2 contract (unchanged)
- `GET /camera/mode/<uuid>` may include `"wifi_setup": true` → camera calls
  `POST /camera/wifi-setup/ack` and starts Bluetooth setup for 10 min.
