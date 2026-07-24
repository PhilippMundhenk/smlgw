# HTTP API reference

The web UI is built entirely on this JSON API, so anything the UI does can be
scripted. All bodies and responses are JSON unless noted. When a UI password is
enabled (see [`auth`](CONFIGURATION.md#auth)), every route except `/login`,
`/api/login`, `/logout`, `/health` and `/static/*` requires an authenticated
session cookie; unauthenticated API calls return `401`.

Base URL: `http://<host>:<port>`.

---

## Status & discovery

### `GET /api/status`
Per-meter runtime state and MQTT connectivity.
```json
{
  "meters": [
    {"id":"heating","name":"Heating","port":"/dev/ttyUSB0","enabled":true,
     "running":true,"state":"reading","last_error":null,"server_id":"0a...",
     "discovered_count":7,"last_seen":1712345678.9,"has_data":true}
  ],
  "mqtt": {"connected": true}
}
```
`state` is one of `stopped`, `connecting`, `connected`, `reading`, `error`.

### `GET /api/ports`
Enumerate serial ports available on the host (best effort).

### `GET /api/sources`
Every `(meter, obis)` currently available — used by the panel editor.
```json
{"sources":[{"meter":"heating","meter_name":"Heating","obis":"1-0:1.8.0*255",
             "name":"Positive active energy total (A+)","unit":"kWh","value":"73.4512"}]}
```

### `GET /api/history`
Downsampled time series for one source.

| Query param | Default | Meaning |
|---|---|---|
| `meter` | — | meter id (required) |
| `obis` | — | OBIS code (required) |
| `since` | `3600` | seconds of history |
| `points` | `500` | max points (bucket-averaged) |

```json
{"meter":"heating","obis":"1-0:1.8.0*255",
 "points":[[1712345600.0, 73.44],[1712345660.0, 73.45]],
 "latest": 73.45}
```

---

## Meters

### `POST /api/meters`  → `201`
Create a meter. Body: `{id, name?, port?, baudrate?, enabled?, verify_crc?, pin?}`.
`409` if the id already exists.

### `PUT /api/meters/{id}`
Update fields (all optional): `{name?, port?, baudrate?, enabled?, verify_crc?, pin?}`.

### `DELETE /api/meters/{id}`
Remove a meter.

### `GET /api/meters/{id}/discovered`
Discovered values plus current mappings.
```json
{"meter":"heating","has_data":true,"state":"reading",
 "values":[{"code":"1-0:1.8.0*255","name":"...","value":"73.4512","unit":"kWh",
            "last_seen":1712345678.9,"count":42,"mapped_topic":"power/heating/total","mapped_enabled":true}],
 "mappings":[{"obis":"1-0:1.8.0*255","topic":"power/heating/total","enabled":true}]}
```

### `PUT /api/meters/{id}/mappings`
Replace the meter's OBIS→topic mappings. Each mapping may include an optional
`unit` selecting the output unit (one of the reading's `unit_options`, e.g.
`"Wh"`); omit it for the default. The chosen unit applies to the MQTT payload,
the UI value and history alike.
Body: `{"mappings":[{"obis":"1-0:1.8.0*255","topic":"power/heating/total","enabled":true,"unit":"kWh"}]}`.

The `discovered` response above includes `unit_options` (selectable labels) and
`mapped_unit` (the currently chosen one) per value to drive the unit dropdown.

---

## Settings

### `GET /api/settings`
Current MQTT, history and auth (enabled flag only) settings, plus stored sample
count.

### `PUT /api/settings/mqtt`
Body: `{host, port?, username?, password?, client_id?, tls?, retain?}`.
Reconnects the publisher live. Omit `password` to leave the stored one unchanged
from the UI (the API replaces the whole MQTT block).

### `PUT /api/settings/history`
Body: `{enabled, retention_hours, sample_interval}`. Applied to the running store
immediately.

### `POST /api/settings/password`
Body: `{password}`. Enables UI protection and stores a PBKDF2 hash.

### `DELETE /api/settings/password`
Disables UI protection and clears the current session.

---

## Backup & restore

### `GET /api/config/export`
Download the entire configuration (meters, mappings, MQTT, dashboard, history,
`pin`, and `auth` including the password hash) as a YAML file
(`Content-Disposition: attachment; filename="smlgw-config.yaml"`). Treat the
file as a secret — it contains credentials.

### `POST /api/config/import`
Replace the whole configuration from a YAML (or JSON) body — the same shape as
`config.yaml`. Applied to the running gateway immediately (meters restart, MQTT
reconnects, history retention updates) and persisted. Invalid input returns
`400`. The running session-signing secret is preserved so the current admin is
not logged out by the import.

```json
{"ok": true, "meters": 2, "panels": 1}
```

---

## Dashboard

### `GET /api/dashboard`
```json
{"panels":[{"id":"p1","title":"House power","type":"line","span":2,"unit":"",
            "time_range":3600,"gauge_min":0,"gauge_max":100,
            "series":[{"meter":"house","obis":"1-0:16.7.0*255","label":"","color":""}]}]}
```

### `PUT /api/dashboard`
Replace all panels. Body: `{"panels":[ <panel>, ... ]}` (same shape as above).

---

## PIN operations

### `POST /api/meters/{id}/pin`
Body: `{pin}` (digits). Starts a background job that enters the PIN and reports
whether the meter unlocked. `409` if a PIN job is already running for that meter.

### `POST /api/meters/{id}/bruteforce`
Body: `{length?=4, start?=0, end?=null}`. Starts a background sweep.

### `GET /api/meters/{id}/pin`
Poll the current PIN job's progress.
```json
{"active":true,"kind":"bruteforce",
 "progress":{"running":true,"tried":128,"total":10000,"current":"0128",
             "found":null,"finished":false,"cancelled":false,"error":null,"percent":1.28}}
```

### `DELETE /api/meters/{id}/pin`
Cancel a running **bruteforce** (a single `send` cannot be interrupted).

---

## Auth & misc

### `GET /login` · `POST /api/login` · `GET|POST /logout`
Login form, form-encoded login (`password` field), and logout. Only meaningful
when a UI password is enabled.

### `GET /health`
Liveness probe: `{"ok": true}`. Always open.
