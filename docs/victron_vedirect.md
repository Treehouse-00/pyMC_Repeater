# Victron VE.Direct serial sensor

`victron_vedirect` reads **local VE.Direct text telemetry** from a Victron charger or battery monitor through a compatible serial adapter. It does not support VE.Bus, query HEX registers, change charger settings, or create remote MeshCore/RF telemetry or RRD history. No serial bytes are transmitted by this plug-in.

**Hardware testing:** a community tester confirmed live readings on a Raspberry Pi, operation through `/dev/serial/by-id/`, an error when the USB cable was disconnected, and recovery with updating readings after reconnection. Restoring solar input also updated the readings to reflect charging. Automated tests separately use synthetic protocol fixtures and Linux pseudo-terminals. This validates the tested setup, not every Victron model, firmware, or adapter. Confirm that your connection is VE.Direct; use a suitable isolated VE.Direct-to-USB interface, not an unverified RS232/TTL cable. The official USB interface is described on protocol page 3. Port opening configures the host UART and uses the adapter/OS's normal control-line behavior; it is not a guarantee of electrically disconnected TX. For the official RS232 adapter, page 4 discusses DTR/RTS power requirements. Do not improvise controller pin wiring.

## Install and configure

Install into the **same Python environment used by the service**, not an unrelated global Python:

```sh
sudo /opt/openhop_repeater/venv/bin/python -m pip install pyserial
```

The standard package already declares pyserial; this command is useful for minimal/custom installations. Import and type discovery remain safe without it: reads report unavailable and never invoke runtime pip, even if global sensor auto-install is enabled.

On Raspberry Pi OS/Debian, check the device's ownership and grant the actual service account access to its group (normally `dialout`):

```sh
sudo usermod -a -G dialout repeater
# For an interactive reader, use your own login account instead:
# sudo usermod -a -G dialout "$USER"
```

Log out/in to refresh interactive group membership. A service must be restarted after its account's group membership or installed code changes. Use your installation's controlled restart procedure; do not solve permissions by running the daemon as root or making the port world-writable.

Stop `cat /dev/ttyUSB0`, serial monitors, Venus/GX readers, and any other program using the same port. Linux exclusive mode is advisory and cannot prevent a reader that ignores the lock from stealing bytes. Do not configure two sensor instances for one device. Each instance must have a unique name.

Add this under the existing `sensors` section (merge, do not replace other definitions):

```yaml
sensors:
  enabled: true
  poll_interval_seconds: 10.0
  auto_install_packages: false
  definitions:
    - type: victron_vedirect
      name: solar-charger
      enabled: true
      auto_install_packages: false
      settings:
        serial_device: "/dev/serial/by-id/REPLACE_WITH_YOUR_ADAPTER"
        read_timeout_seconds: 1.5
```

Use your real `/dev/serial/by-id/` path to survive USB enumeration changes. `/dev/ttyUSB0` is supported but can identify a different adapter after reboot. The settings API discovers this sensor automatically; installing the module does not detect hardware or add a definition. Restart after installing code, then add/configure it in Sensor Manager. Construction rejects a missing/nonlocal device path or invalid deadline, and SensorManager skips an invalid definition with a warning. The generic settings save API does not validate these plug-in-specific values before persistence.

`read_timeout_seconds` defaults to 1.5 and must be finite, between 0.05 and 2.0 seconds. Short values are useful in tests but can miss live frames, which arrive approximately once per second. Serial settings are fixed at **19200 baud, 8 data bits, no parity, 1 stop bit, no flow control**. No baud-rate or write-command setting is offered.

### Docker

Map the host adapter to a fixed device name inside the container and set the sensor's `serial_device` to that name. Grant the container process the host device group's **numeric GID**; do not assume `dialout` has the same GID inside and outside the image. Example Compose fragment, with placeholders replaced:

```yaml
services:
  repeater:
    devices:
      - "/dev/ttyUSB0:/dev/victron"
    group_add:
      - "REPLACE_WITH_HOST_DEVICE_GROUP_GID"
```

Resolve the host's by-id symlink before mapping, or use a stable host udev alias. Docker short device syntax uses colons as separators; do not use a by-id name containing colons unescaped. Use `/dev/victron` in container config. USB unplug/replug can require recreating the container's device mapping; the plug-in cannot repair Docker's host device binding. LXC likewise needs host device permissions and an explicit device bind/cgroup allowance.

## Reading behavior and availability

Every poll opens the configured local port, reads for the bounded acquisition window, and closes it, including failure paths. There is no background serial owner or SensorManager lifecycle change. Reads request at most 256 bytes with at most 100 ms timeout, constrained by a monotonic deadline and 16 KiB total byte budget. Parser memory retains at most 2 KiB of stream suffix plus the checksum byte; field labels/values are limited to 8/32 ASCII characters and blocks to 22 data fields. Noise, oversized/malformed blocks, partial frames and checksum failures never contribute telemetry. Asynchronous colon-to-LF HEX messages are skipped without including their bytes in the text checksum; the checksum itself is one arbitrary binary byte, including colon, tab, CR, LF, NUL or high-bit bytes.

All complete validated blocks observed **during this poll only** are merged; the last observed value for a repeated field wins. Blocks are not an atomic hardware snapshot. A partial block at the deadline is discarded. Fields in unobserved blocks are unavailable, not copied from a previous poll. A disconnect/permission/read/close failure makes the entire reading unavailable, even if a valid block preceded the failure. The next poll opens afresh and can recover after reconnection.

Read latency can consume the configured deadline even on success. Other sensors in the manager wait behind this poll, but it does not wait indefinitely for a controller. Multiple configured serial devices add their bounded acquisition times. This deadline bounds normal local serial acquisition, not pathological kernel/driver failure or the existing manager's scheduling interval.

The standard envelope contains `name`, `type`, `ok`, `timestamp`, flat `data`, optional `error`, and `metrics`. A validated text block gives `ok: true` even if no supported measurement is present. No valid frame gives `ok: false` and empty data. A present invalid field is `null`; a missing field is omitted. `---` is unavailable, and `TTG=-1` means infinite, represented as unavailable rather than a fabricated numeric duration. Previously observed metric descriptors remain with `available: false` and `reason: missing`, `invalid`, or `read_failed`; their values are never retained. IDs such as `/VPV` are pointers into the parsed text field mapping, and `data_key` points to the normalized flat key. Descriptors use the existing number/boolean kind, category, nullable unit and availability contract; generic UI clients should honor `unit` rather than guess from labels. Older clients can still use flat values and unit-bearing keys.

Only an explicit allowlist is published. Serial number (`SER#`), product/firmware strings, unknown labels, HEX payloads and raw frames are not returned or logged. Transport errors are deliberately sanitized.

## Fields and units

Fields depend on model, firmware and configured auxiliary input. This table describes supported conversions, **not** a promise that a particular MPPT emits every field. In particular battery-monitor SOC, consumed Ah and battery temperature are not fabricated for an MPPT.

| Text labels | Flat keys | Output units / conversion |
|---|---|---|
| `V`, `I` | `battery_voltage_v`, `battery_current_a` | mV→V, mA→A; current sign preserved |
| `VPV`, `PPV`, `IL` | `pv_voltage_v`, `pv_power_w`, `load_current_a` | mV→V, W, mA→A |
| `CS`, `ERR`, `MPPT` | `charge_state_code`, `error_code`, `tracker_state_code` | Numeric protocol codes; no guessed model-specific interpretation |
| `LOAD`, `Relay`, `Alarm` | `load_output`, `relay`, `alarm` | Case-insensitive ON/OFF→boolean; other values unavailable |
| `AR`, `OR`, `MON` | `alarm_reason`, `off_reason`, `monitor_mode_code` | Decimal alarm mask, hexadecimal off mask, signed decimal DC monitor mode |
| `VS`, `VM`, `DM` | `aux_voltage_v`, `midpoint_voltage_v`, `midpoint_deviation_percent` | mV→V, mV→V, per-mille→percent |
| `T`, `P`, `CE`, `SOC`, `TTG` | `battery_temperature_c`, `battery_power_w`, `consumed_ah`, `state_of_charge_percent`, `time_to_go_minutes` | °C, W, mAh→Ah, per-mille→percent, minutes |
| `H1`, `H2`, `H3`, `H6` | `deepest_discharge_ah`, `last_discharge_ah`, `average_discharge_ah`, `cumulative_drawn_ah` | mAh→Ah; signs preserved |
| `H4`, `H5`, `H10`, `H11`, `H12` | `charge_cycles`, `full_discharges`, `automatic_syncs`, `low_voltage_alarms`, `high_voltage_alarms` | Counts |
| `H7`, `H8`, `H15`, `H16` | `min_battery_voltage_v`, `max_battery_voltage_v`, `min_aux_voltage_v`, `max_aux_voltage_v` | mV→V |
| `H9` | `seconds_since_full_charge` | Seconds |
| `H17`, `H18` | `history_energy_h17_kwh`, `history_energy_h18_kwh` | 0.01 kWh→kWh; see mode caveat below |
| `H19`, `H20`, `H22` | `yield_total_kwh`, `yield_today_kwh`, `yield_yesterday_kwh` | 0.01 kWh→kWh; total is user-resettable |
| `H21`, `H23`, `HSDS` | `max_power_today_w`, `max_power_yesterday_w`, `day_sequence` | W, W, day sequence 0–364 |

`H17/H18` mean discharged/charged energy for a BMV, but produced/consumed energy in DC monitor mode; their keys intentionally avoid assigning a meaning that requires knowing the mode. `MON` is available only on supporting firmware. `SOC` outside 0–1000 per-mille and `HSDS` outside 0–364 are treated as invalid. Negative unsigned status codes/masks are invalid; `MON` remains signed.

### Readable Status cards

Raw numeric codes and masks remain unchanged in flat `data` for API/storage consumers. The corresponding descriptor carries optional `display_value` text, rendered directly in the existing **Status** card by compatible RepeaterUI versions. There is no extra flat `charge_state` string or duplicate **Legacy values** entry. Older UIs safely retain numeric status rendering.

- `CS`: official state of operation (p. 15), e.g. `3` → **Bulk**.
- `ERR`: official error code (p. 15), e.g. `0` → **No error**.
- `MPPT`: tracker operation (p. 16), e.g. `2` → **MPP Tracker active**.
- `MON`: signed DC monitor mode (p. 16), e.g. `-9` → **Solar charger**.
- `OR`: hexadecimal off-reason mask (p. 11), e.g. `0x9` → **No input power; Remote input**.
- `AR`: decimal alarm-reason mask (p. 11), e.g. `5` → **Low Voltage; Low SOC**.

Unknown enum values remain explicit as **Unknown (code)**. All active mask reasons are shown; unrecognized bits are appended as **Unknown bits (0xHEX)**, not silently discarded. A zero reason mask displays **None**, meaning no reason bits are set (not an inference about device health). Missing, invalid or failed readings omit `display_value` and show **n/a**, never the previous label. `day_sequence` remains numeric and LOAD/Relay/Alarm remain booleans. These interpretations follow the protocol, not a guessed physical model.

For MPPT 75/15 and 100/15, firmware 1.14 and earlier reports converter output current in `I`, not necessarily current into the battery. Protocol Table I footnote 5 documents the distinction. This plug-in preserves the reported current; confirm firmware before treating that key as battery current on older models.

## Authoritative protocol and implementation evidence

Source: [Victron VE.Direct Protocol 3.34](https://www.victronenergy.com/upload/documents/VE.Direct-Protocol-3.34.pdf), document history dated 14 April 2025. Page numbers below are PDF pages (including the contents page).

| Authority | Implementation / tests |
|---|---|
| p. 2, **Introduction**: text/HEX coexistence; asynchronous `:A…LF` may interrupt text | `VEDirectParser.feed`; synthetic fragmented HEX-interleaved fixtures |
| p. 4, **Serial port configuration** | Fixed 19200 8N1, all flow control disabled; serial constructor assertion and real pyserial PTYs |
| p. 5, **Message format**, **Data integrity**: CRLF/label/tab/value, binary single-byte Checksum, modulo-256 sum=0, multiple blocks | Incremental binary parser; all 256 checksum byte values, corruption/resync and multiple-block polling tests |
| pp. 6–9, **Fields**, **Table I Supported Text-mode fields** | Explicit `FIELDS` conversion allowlist and parameterized real-manager normalization tests |
| p. 6 footnotes 8–11; p. 7 footnote 12 | `---` invalid/unsynchronized/unsupported values and infinite `TTG=-1` tests |
| pp. 10–11, **I**, **Alarm**, **Relay**, **AR**, **OR**; pp. 15–16, **CS**, **ERR**, **HSDS**, **MPPT**, **MON** | Preserve raw current sign and numeric states/masks; official descriptor display text for all enum values and mask bits, unknown codes/bits and multi-reason tests in `tests/test_victron_status.py`; case-insensitive flags; no fabricated model fields |
| p. 17, **Implementation guidelines** | 8/32 character field payload bounds, 22 temporary data fields, validation before publication; bounded noise/malformed fixtures |
| `SensorBase.read/_reading_metrics`, registry decorator, `SensorManager.reload/read_all`, `APIEndpoints.sensors_types`, OpenAPI `SensorMetricDescriptor` | Real manager and settings-discovery tests; no new lifecycle hooks; metadata conforms to the existing optional contract |

All tests in `tests/test_victron_vedirect.py` are explicitly synthetic. PTYs exercise the real Linux/pyserial open/read/close path, fragmentation, timeout, disconnect/recovery and absence of transmitted serial data; they do **not** validate the friend's controller, cable, firmware or live values.
