# Copilot Link — simulated UAV datalink (beta)

**SIMULATED DATA ONLY — nothing here touches motors, flight controllers, or real aircraft.**

the idea: a reliable command link between a ground computer (Raspberry Pi 4B/5) and an aircraft computer (Pi Zero 2W) over Wi-Fi/TCP. this repo is the beta prototype proving the protocol works before any hardware gets involved.

## how it actually works

two ends, stdlib-only Python, newline-delimited JSON protocol v1:
- ground side (`run_ground.py` → `copilot_link/ground/client.py`) — TCP client pushing `COMMAND` frames at 50 Hz plus `HEARTBEAT`
- aircraft side (`run_aircraft.py` → `copilot_link/aircraft/server.py`) — TCP server doing frame validation, ACKs, `STATUS` replies, and link-loss failsafe behavior
- `copilot_link/common/{protocol,messages,config}.py` — the shared wire format
- chaos-injection knobs let you simulate packet loss/latency and measure validation metrics under stress

```bash
# localhost demo (two terminals)
python run_aircraft.py
python run_ground.py
pytest  # only dep in requirements.txt
```

## stack

Python 3.10+ stdlib only (plus pytest). explicit non-goals: motors, flight control, AI — this is a comms-layer prototype and it stays in its lane.
