# alexis-homeassistant

Controls solar micro inverters so their combined output follows the house
consumption as closely as possible **without exceeding it**, preventing
power from being fed back to the grid.

## Hardware assumptions

- Each inverter is fed through its own **Shelly switch** (Gen2+ RPC device,
  e.g. Plus 1PM or Plug S).
- Optionally, an inverter's actual output is measured by a **Shelly power
  monitor** (any device reporting `apower` via `Switch.GetStatus`).
- A **Shelly energy meter** (EM1 component, e.g. EM Mini Gen3) sits at the
  grid connection point: positive readings are import, negative export.

## How it works

Every `UPDATE_INTERVAL` seconds the control loop:

1. Reads the grid meter and all switch and monitor states.
2. Computes house consumption = grid power + current inverter production
   (monitored inverters use their live measurement; unmonitored running
   inverters are estimated at their rating).
3. Determines each inverter's effective capacity:
   - live measurement when its monitor reports power;
   - a monitored inverter that is off is **probed**: switched on for
     `PROBE_SECONDS`, measured, switched back off — at most once per
     `PROBE_MIN_INTERVAL`, one inverter at a time;
   - for `STARTUP_GRACE` seconds after switch-on, a 0 W reading is treated
     as grid-sync startup and the last probe (or rating) is used instead;
   - unmonitored inverters and unreachable monitors fall back to the rating.
4. Searches all inverter combinations for the highest total capacity that
   stays **at or below** consumption.
5. Applies the result, but only when it improves the match by more than
   `HYSTERESIS` watts — unless power is currently being exported, which
   always forces a reconfiguration. Only switches that need to change are
   touched.

Unreachable devices degrade gracefully: the affected inverter is skipped
(or estimated) for the cycle and the loop carries on.

## Configuration

All settings live at the top of `main.py`:

| Setting | Default | Meaning |
|---|---|---|
| `UPDATE_INTERVAL` | 60 s | Time between control cycles |
| `PROBE_SECONDS` | 15 s | How long a probe runs an inverter |
| `PROBE_MIN_INTERVAL` | 15 min | Minimum time between probes of one inverter |
| `STARTUP_GRACE` | 120 s | How long a fresh inverter may read 0 W |
| `HYSTERESIS` | 100 W | Minimum improvement before switching |
| `CONSUMPTION_METER` | — | Grid meter: `{"ip", "channel", "auth"}` |
| `INVERTERS` | — | See below |

Each entry in `INVERTERS`:

```python
{
    "id": 1,                # unique inverter id
    "power": 800,           # rating / max power in watts
    "switch":  {"ip": "192.168.1.101", "channel": 0, "auth": None},
    "monitor": {"ip": "192.168.1.111", "channel": 0, "auth": None},  # or None
}
```

`auth` is `None` or `{"username": ..., "password": ...}` for password
protected devices.

## Running

```bash
uv run main.py
```

Logs go to stdout only.

### As a service on a Raspberry Pi

```bash
mkdir -p ~/.config/systemd/user
cp alexis-homeassistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alexis-homeassistant
loginctl enable-linger $USER   # keep it running while logged out
journalctl --user -u alexis-homeassistant -f
```

The unit assumes the repo at `~/alexis-homeassistant` and uv at
`~/.local/bin/uv`.

## Development

```bash
uv run pytest
```

Tests run against simulated Shelly devices; no hardware needed.
