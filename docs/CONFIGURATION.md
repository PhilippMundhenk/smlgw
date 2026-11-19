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
    port: usb:serial=A50285BI  # pinned reader (or /dev/serial/by-id/..., /dev/ttyUSB0, COM3)
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
| `port` | string | `""` | Which reader to open: a **`usb:` selector** pinning the reader by serial number or USB socket (`usb:serial=A50285BI`, `usb:location=1-1.2`; see [Pinning readers](#pinning-readers-to-stable-ids)), a device path (`/dev/serial/by-id/…`, `/dev/ttyUSB0`, `COM3`), **or** a pyserial URL for a networked meter (`socket://host:5000`, `rfc2217://host:5000`). |
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

### Pinning readers to stable IDs

Kernel names like `/dev/ttyUSB0` are handed out in plug-in order. With more than
one IR reader, a reboot, an unplug or a USB hub can swap them, and each meter's
values silently end up on the other meter's topics. Pin each meter to its
**reader** instead:

| `port` value | Matches | Use when |
|---|---|---|
| `usb:serial=A50285BI` | the adapter's USB serial number | the reader has a unique serial (FTDI, CP210x and most branded IR heads). Follows the reader to any socket. |
| `usb:location=1-1.2` | the physical USB socket (bus-port path) | the reader has no serial number, or several share one (cheap CH340 clones). Stays valid as long as the reader stays in the same socket of the same hub. |
| `usb:vid=0403,pid=6001` | vendor/product id | only one adapter of that model is attached. |
| `/dev/serial/by-id/usb-FTDI_…-if00-port0` | udev symlink by serial | you prefer plain paths (Linux with udev). |
| `/dev/serial/by-path/…-usb-0:1.2:1.0-port0` | udev symlink by socket | same, by socket. |

Keys combine with commas and all must match, e.g.
`usb:vid=10c4,pid=ea60,serial=0001`. `vid`/`pid` are hex; `serial` is exact and
case-sensitive; `location` may include the interface suffix pyserial reports
(`1-1.2:1.0`) or omit it (`1-1.2`).

The selector is resolved to the current `/dev/tty*` name **every time the port
is opened**, so after an unplug the reconnect loop finds the reader again under
its new name. If no reader or more than one reader matches, the meter goes to
`error` with a message saying so (`no serial device matches …` /
`… is ambiguous, it matches /dev/ttyUSB0, /dev/ttyUSB1`); it never guesses.
Malformed selectors are rejected when saved from the UI/API or imported.

To find the right value, plug in the readers and either open a meter page (the
**Detected serial adapters** table has a *use* button per reader) or run:

```console
$ smlgw ports
/dev/ttyUSB0  FT232R USB UART  [0403:6001]
    serial number: A50285BI
    usb location:  1-1.2:1.0
    symlink:       /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0
    use as port:   usb:serial=A50285BI

configured meters:
  heating: usb:serial=A50285BI -> /dev/ttyUSB0
```

`smlgw ports` exits non-zero if a configured meter's selector matches no reader
or several, or if two meters share a reader. Meters still using a kernel name
are marked **⚠ unstable** in the meter list.

### One reader per meter

Two **enabled** meters cannot use the same reader: both would read the one byte
stream and each get torn halves of the frames. Readers are compared by the
device they resolve to, so `/dev/ttyUSB0`, its `/dev/serial/by-id/…` symlink and
a `usb:serial=…` selector for the same adapter count as the same reader (two
identical `socket://` URLs too). Disabled meters are exempt, so a spare
configuration for the same reader can be kept.

- Adding or editing a meter (port or *enabled*) in the UI/API so that it would
  share a reader is refused (`409`, naming the other meter); importing such a
  configuration is refused (`400`).
- A `config.yaml` edited by hand can still contain a conflict. Both meters are
  then marked **⚠ same reader as …** in the meter list and on the meter page, and
  `smlgw ports` reports it.
- At runtime local ports are opened **exclusively** (a lock on Linux/macOS;
  Windows ports are always exclusive). Only the first meter gets the port; the
  other one shows `error: /dev/ttyUSB0 is already in use by another meter or
  program` and keeps retrying. The same applies to other programs:
  `smlgw bruteforce` for a meter the running service is reading fails with this
  message, so stop the service or use the PIN tools in the web UI instead.
