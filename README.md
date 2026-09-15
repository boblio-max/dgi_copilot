# Copilot Link — Beta Prototype (SIMULATED DATA ONLY)

A reliable communication layer between a **ground computer** (Raspberry Pi 4B / Pi 5)
and an **aircraft computer** (Raspberry Pi Zero 2 W) over **Wi-Fi/TCP**.

> **This beta prototype does NOT control an aircraft.** It exchanges only simulated
> command data between the two Pis and measures link quality. There is no motor
> control, no arming, no DShot, no MSP/Betaflight, no DJI HID, no AI, no computer
> vision, and no autonomous flight anywhere in this codebase — by design there is
> no code path that could touch a motor or flight controller.

```
RC3
 │
 │ future USB HID
 ▼
Pi 4B / Pi 5              (ground: pilot input + future copilot AI)
 │
 │ future copilot intent
 │
 │ ══════ CURRENT BETA ══════
 ▼
Wi-Fi / TCP  (versioned JSON lines, 50 Hz simulated COMMAND + HEARTBEAT)
 │
 ▼
Pi Zero 2 W               (aircraft: validation, arbitration-ready stats)
 │
 │ future MSP
 ▼
Betaflight FC             (NOT connected in this prototype)
```

## Responsibilities

| Side | Program | Role |
|---|---|---|
| Ground Pi 4B/5 | `run_ground.py` → `copilot_link/ground/client.py` | TCP **client**. Streams simulated COMMANDs at a configurable rate (default 50 Hz) plus periodic HEARTBEATs. Tracks ACKs/RTT, auto-reconnects. Optional chaos injection for stress tests. |
| Aircraft Zero 2 W | `run_aircraft.py` → `copilot_link/aircraft/server.py` | TCP **server**. Validates every packet, detects duplicates/gaps/reordering, tracks heartbeat timeout → `COPILOT_LINK_LOST`, ACKs commands, pushes STATUS. Never crashes on bad data. |

Shared code lives in `copilot_link/common/`: `protocol.py` (framing + validation),
`messages.py` (builders + simulated data), `config.py` (env/CLI configuration).
Only the socket send/recv loops know about TCP, so the transport can be replaced later.

## Install

Stdlib only (Python 3.10+). No dependencies to install.

```bash
git clone <this-repo> && cd <this-repo>
# optional, for pytest-style runs:
pip install -r requirements.txt
```

## Quick start (single computer, localhost)

Terminal 1 — aircraft server:

```bash
python run_aircraft.py --host 127.0.0.1 --port 5555
```

Terminal 2 — ground client (200 packets, then exit with a summary):

```bash
python run_ground.py --host 127.0.0.1 --port 5555 --rate 50 --count 200
```

On the real hardware, run the server on the Zero 2 W (it listens on `0.0.0.0` by
default) and point the client at it:

```bash
# Zero 2 W:
python run_aircraft.py
# Pi 4B/5 (replace with the Zero's Wi-Fi IP):
python run_ground.py --host 192.168.4.1
```

Ground output looks like:

```text
[ground] SEQ 00050 roll=1500 pitch=1648 yaw=1500 throttle=1400 yaw_correction=31 mode=HEADING_HOLD
...
200 packets transmitted
acked:          200
rejected acks:  0
duplicate acks: 0
unacked:        0
average RTT:    0.8 ms
max RTT:        1.9 ms
server: received=200 rejected=0 duplicates=0 out_of_order=0 lost_estimate=0 state=LINK_OK
```

Aircraft output looks like:

```text
RX COMMAND seq=142 age=0.5 ms pitch=1650 yaw=1500 yaw_correction=17 mode=HEADING_HOLD
[aircraft] HEARTBEAT TIMEOUT -> COPILOT_LINK_LOST   # only if the link actually dies
```

## Protocol

One packet = one newline-terminated JSON object. Envelope:

```json
{"version": 1, "type": "command", "sequence": 123, "timestamp": 1726330000.123, "payload": {}}
```

Message types: `command` (ground→air, pilot + copilot corrections + mode),
`heartbeat` (ground→air, liveness), `ack` (air→ground, per-packet accept/reject),
`status` (air→ground, pushed every 1 s: received/rejected/duplicates/
out_of_order/gaps/lost_estimate/latest_seq/latency/heartbeat/connection state).

Validation (server side, never crashes on bad input): version, type, field
presence/types, channel ranges (1000–2000), correction ranges (±500), mode
allowlist, sequence sanity, timestamp freshness (default max age 1 s, future-skew
guard 0.5 s). Rejections are logged with a reason and (when a seq is known)
negatively ACKed. Command and heartbeat sequence numbers live in separate spaces
(heartbeats start at 1 000 000 000) so their ACKs can never collide.

## Stress test

```bash
# preset chaos: 5% loss, 2% duplicates, 2% reorder, 20 ms delay on 5%:
python run_ground.py --host <zero-ip> --rate 50 --count 1000 --stress-test --malformed-rate 0.01
```

Fine-grained knobs: `--loss-rate`, `--delay` + `--delay-rate`,
`--duplicate-rate`, `--reorder-rate`, `--malformed-rate`, `--drop-every N`
+ `--drop-for M` (close the connection every N s for M s to test reconnect).

Example result (1000 attempted, chaos on):

```text
945 packets transmitted
acked:          937
duplicate acks: 17
unacked:        8            # the 7 malformed + 1 lost: server can never ACK garbage
average RTT:    1.4 ms
max RTT:        41.9 ms
server: received=938 rejected=7 duplicates=17 out_of_order=1 lost_estimate=63 state=LINK_OK
```

How to read it:

| Field | Meaning |
|---|---|
| `received` / `rejected` | valid vs malformed packets seen by the aircraft |
| `duplicates` | same seq seen twice (wasted bandwidth, harmless) |
| `out_of_order` | late seq arriving after a newer one |
| `lost_estimate` / `gaps` | sum/count of sequence jumps = packets that never arrived |
| `avg/max latency` | one-way `now - timestamp` at the receiver (needs synced clocks; otherwise treat as relative) |
| `RTT` (client) | true round-trip from ACK matching, no clock sync needed |
| `state` | `LINK_OK` vs `COPILOT_LINK_LOST` (no heartbeat for `HEARTBEAT_TIMEOUT`, default 0.5 s) |

**DoD checklist:** two Pis streaming 50 Hz while reporting sent / received / lost /
duplicates / out-of-order / avg+max latency / heartbeat + connection state, link
loss detected within the configured timeout, and automatic reconnect — all
demonstrated above.

## Configuration

`run_ground.py`: `--host --port --rate --count --duration --verbose --log-file`
plus the `--stress-test` chaos knobs above.
`run_aircraft.py`: `--host --port --hb-timeout --max-age --verbose --log-file`.

Or via environment: `COPILOT_HOST COPILOT_PORT COPILOT_RATE_HZ
COPILOT_HB_INTERVAL_S COPILOT_HB_TIMEOUT_S COPILOT_MAX_AGE_S COPILOT_LOG_LEVEL
COPILOT_LOG_FILE`. CLI flags override env. Structured logs go to stderr, and to
a file with `--log-file out.log` (per-packet lines only at `--verbose`/DEBUG).

## Testing

```bash
py -m unittest discover -s copilot_link/tests -v
```

Covers serialization round-trips, version/type/field/range/freshness rejection,
duplicate/gap/reorder handling, builder validity, plus a real localhost TCP
round-trip (`TestIntegrationLoopback`: server thread + 10 commands + heartbeat,
asserts every ACK).

## Roadmap (out of scope for this beta)

DJI RC3 USB HID → copilot intent on the ground Pi; command arbitration + MSP to
Betaflight on the Zero 2 W. Do not start either until this link passes the
stress tests above.
