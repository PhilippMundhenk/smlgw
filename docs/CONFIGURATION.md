# Configuration reference

smlgw is configured from a single YAML file. Its location is, in order of
precedence:

1. `--config <path>` on the command line
2. the `SMLGW_CONFIG` environment variable
3. `config.yaml` in the working directory (default)

The file is written back **atomically** whenever you change something in the web
UI, so hand edits and UI edits can coexist. A missing file yields built-in
defaults. Below is every section and field.

```yaml
mqtt: { ... }        # broker connection + publish behaviour
web: { ... }         # bind address of the web UI
history: { ... }     # time-series storage / retention
pin: { ... }         # optical PIN entry timing (PIN-locked meters only)
auth: { ... }        # optional UI password
dashboard: { ... }   # saved dashboard panels
meters: [ ... ]      # the meters to read
```

---

## `mqtt`

Connection to the MQTT broker and how values are published.

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | string | `localhost` | Broker hostname/IP. |
| `port` | int | `1883` | Broker port. |
| `username` | string \| null | `null` | Optional username. |
| `password` | string \| null | `null` | Optional password. |
| `client_id` | string | `smlgw` | MQTT client id. |
| `tls` | bool | `false` | Use TLS (`tls_set()` defaults). |
| `retain` | bool | `false` | Publish messages with the retain flag. |

Changing MQTT settings from the Settings page reconnects the publisher live.

---

## `web`

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address (overridden by `run --host`). |
| `port` | int | `8000` | Port (overridden by `run --port`). |

---

## `history`

Time-series storage. Every **numeric** OBIS value is recorded so the dashboard
can plot it.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Turn recording on/off. |
| `retention_hours` | float | `168` | How long to keep data (7 days). Older rows are pruned. |
| `sample_interval` | float | `10` | Minimum seconds between stored samples **per (meter, obis)**. Prevents one row/second. |
| `db_path` | string \| null | `null` | SQLite file. Default: `history.db` next to the config file. `--simulate` uses an in-memory DB. |

Retention and sample spacing are editable on the Settings page and applied live.

---

## `pin`

Optical PIN entry parameters — only relevant for PIN-locked meters. A "pulse" is
a block of bytes written to toggle the IR LED once.

| Field | Type | Default | Notes |
|---|---|---|---|
| `pulse` | hex string | 30 × `00` | Bytes written for a single LED flash. |
| `digit_gap` | float | `1.0` | Seconds between pulses within one digit. |
| `group_gap` | float | `3.0` | Seconds between digits. |
| `settle` | float | `2.0` | Seconds after the reset pulse. |
| `detect_timeout` | float | `20.0` | Seconds to wait for the meter to unlock. |
| `detect_obis` | string | `1-0:1.8.0*255` | The register whose non-zero value proves an unlock. |

---

## `auth`

Optional password protection for the whole UI (pages **and** API).

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Whether a password is required. |
| `password_hash` | string \| null | `null` | `pbkdf2$iterations$salt$hash`. Never a plaintext password. |
| `secret` | string \| null | auto | Random session-cookie signing secret, generated and persisted on first run. |

Set/disable the password from the Settings page (do not edit `password_hash` by
hand).

---

## `dashboard`

Saved dashboard panels. Usually edited through the UI, but fully declarative.

```yaml
dashboard:
  panels:
    - id: p1                 # stable unique id
      title: House power
      type: line             # line | stat | gauge
      span: 2                # 1 = half width, 2 = full width
      unit: ""               # optional unit override (else taken from source)
      time_range: 3600       # seconds of history shown (line panels)
      gauge_min: 0           # gauge lower bound (gauge panels)
      gauge_max: 250         # gauge upper bound (gauge panels)
      series:
        - meter: house       # meter id
          obis: "1-0:16.7.0*255"
          label: ""          # optional legend label
          color: ""          # optional hex colour, else auto
```

| Panel field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | — | Unique, stable id. |
| `title` | string | `""` | Panel heading. |
| `type` | string | `line` | `line`, `stat`, or `gauge`. |
| `span` | int | `1` | `1` half width, `2` full width. |
| `unit` | string | `""` | Override the auto-detected unit. |
| `time_range` | float | `3600` | Seconds of history (line panels). |
| `gauge_min` / `gauge_max` | float | `0` / `100` | Gauge bounds. |
| `series[]` | list | `[]` | Sources. `stat`/`gauge` use the first entry. |

---

## `meters`

The list of meters to read.

```yaml
meters:
  - id: heating              # used in default topic suggestions; must be unique
    name: Heating            # display name (defaults to id)
    port: /dev/ttyUSB0       # serial device (COM3 on Windows)
    baudrate: 9600
    enabled: true
    verify_crc: false        # drop frames with a bad CRC (default: tolerate)
    pin: null                # optional stored PIN
    mappings:                # OBIS -> MQTT topic
      - { obis: "1-0:1.8.0*255", topic: power/heating/total, enabled: true }
```

| Meter field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | — | Unique id. |
| `name` | string | = `id` | Display name. |
| `port` | string | `""` | Serial device path. |
| `baudrate` | int | `9600` | Typically 9600 for SML. |
| `enabled` | bool | `true` | Disabled meters are not read. |
| `verify_crc` | bool | `false` | If true, frames failing CRC are dropped. |
| `pin` | string \| null | `null` | Optional stored PIN. |
| `mappings[]` | list | `[]` | `{obis, topic, enabled, unit}` — publish this OBIS code to this topic. |

Each mapping's optional **`unit`** selects the output unit for that reading
(e.g. `Wh` instead of the default `kWh`, or `kW` instead of `W`); omit it (or set
`null`) to use the reading's default unit. The chosen unit applies consistently
to the MQTT payload, the value shown in the UI, and what is recorded to history.

Only OBIS codes with an **enabled** mapping are published to MQTT; every numeric
value is still recorded to history and shown in the UI.
