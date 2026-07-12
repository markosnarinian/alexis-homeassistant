"""Match solar inverter output to house consumption.

Switches micro inverters on and off so their combined output follows the
house consumption as closely as possible without exceeding it, preventing
power from being fed back to the grid.
"""

import asyncio
import contextlib
import logging
import sys
import time
from itertools import combinations

import aiohttp
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice

log = logging.getLogger("alexis")

# Seconds between control loop updates
UPDATE_INTERVAL = 60

# How long to switch on an off inverter to measure its actual output
PROBE_SECONDS = 15

# Minimum time between probes of the same inverter; within this interval
# the last probed value is reused
PROBE_MIN_INTERVAL = 15 * 60

# For this long after an inverter is switched on, a 0 W reading from its
# monitor is attributed to grid-sync startup rather than believed
STARTUP_GRACE = 120

# Minimum improvement in matched watts before inverters are switched;
# ignored when power is being exported, which always forces a reconfiguration
HYSTERESIS = 100

# Shelly energy meter (EM1 component) at the grid connection point;
# positive readings are import from the grid, negative are export
CONSUMPTION_METER = {"ip": "192.168.1.100", "channel": 0, "auth": None}

INVERTERS = [
    {
        "id": 1,
        "power": 800,
        "switch": {"ip": "192.168.1.101", "channel": 0, "auth": None},
        "monitor": {"ip": "192.168.1.111", "channel": 0, "auth": None},
    },
    {
        "id": 2,
        "power": 800,
        "switch": {"ip": "192.168.1.102", "channel": 0, "auth": None},
        "monitor": {"ip": "192.168.1.112", "channel": 0, "auth": None},
    },
    {
        "id": 3,
        "power": 600,
        "switch": {"ip": "192.168.1.103", "channel": 0, "auth": None},
        "monitor": None,
    },
    {
        "id": 4,
        "power": 1200,
        "switch": {"ip": "192.168.1.104", "channel": 0, "auth": None},
        "monitor": None,
    },
    {
        "id": 5,
        "power": 2000,
        "switch": {"ip": "192.168.1.105", "channel": 0, "auth": None},
        "monitor": None,
    },
]


_session: aiohttp.ClientSession | None = None
# Connected RPC devices by ip, reused across calls
_devices: dict[str, RpcDevice] = {}
# Last probe per inverter id: (monotonic timestamp, measured power)
_last_probe: dict[int, tuple[float, float]] = {}
# Monotonic timestamp of the last switch-on per inverter id
_last_on: dict[int, float] = {}


async def _get_device(config: dict) -> RpcDevice:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    device = _devices.get(config["ip"])
    if device is None or not device.connected:
        auth = config.get("auth") or {}
        options = ConnectionOptions(
            config["ip"],
            username=auth.get("username"),
            password=auth.get("password"),
        )
        device = await RpcDevice.create(_session, None, options)
        await device.initialize()
        _devices[config["ip"]] = device
    return device


async def _device_rpc(config: dict, method: str, params: dict | None = None):
    device = await _get_device(config)
    try:
        return await device.call_rpc(
            method, {"id": config["channel"], **(params or {})}
        )
    except Exception:
        _devices.pop(config["ip"], None)
        with contextlib.suppress(Exception):
            await device.shutdown()
        raise


async def _shutdown_devices():
    for device in _devices.values():
        with contextlib.suppress(Exception):
            await device.shutdown()
    _devices.clear()
    if _session is not None and not _session.closed:
        await _session.close()


def _get_inverter(id: int) -> dict:
    return next(inv for inv in INVERTERS if inv["id"] == id)


async def set_switch_state(id: int, on: bool):
    await _device_rpc(_get_inverter(id)["switch"], "Switch.Set", {"on": on})
    if on:
        _last_on[id] = time.monotonic()


async def get_switch_state(id: int) -> bool:
    status = await _device_rpc(_get_inverter(id)["switch"], "Switch.GetStatus")
    return status["output"]


async def get_grid_power() -> float:
    """Power at the grid connection point: positive = import, negative = export."""
    status = await _device_rpc(CONSUMPTION_METER, "EM1.GetStatus")
    return status["act_power"]


async def _read_switch_states() -> dict[int, bool]:
    """Switch state per inverter id; unreachable switches are left out."""
    results = await asyncio.gather(
        *(get_switch_state(inv["id"]) for inv in INVERTERS),
        return_exceptions=True,
    )
    states = {}
    for inverter, result in zip(INVERTERS, results):
        if isinstance(result, BaseException):
            log.warning("Switch of inverter %s unreachable: %r", inverter["id"], result)
        else:
            states[inverter["id"]] = result
    return states


async def _read_monitors() -> dict[int, float | None]:
    """Measured output per monitored inverter id; None when unreachable."""
    monitored = [inv for inv in INVERTERS if inv["monitor"] is not None]
    results = await asyncio.gather(
        *(_device_rpc(inv["monitor"], "Switch.GetStatus") for inv in monitored),
        return_exceptions=True,
    )
    readings = {}
    for inverter, result in zip(monitored, results):
        if isinstance(result, BaseException):
            log.warning("Monitor of inverter %s unreachable: %r", inverter["id"], result)
            readings[inverter["id"]] = None
        else:
            readings[inverter["id"]] = result.get("apower") or 0.0
    return readings


async def _probe_inverter(inverter: dict) -> float:
    """Switch an off inverter on briefly and measure its actual output."""
    id = inverter["id"]
    log.info("Probing inverter %s for %s s", id, PROBE_SECONDS)
    await set_switch_state(id, True)
    try:
        await asyncio.sleep(PROBE_SECONDS)
        status = await _device_rpc(inverter["monitor"], "Switch.GetStatus")
    finally:
        await set_switch_state(id, False)
    # A 0 W probe usually means the inverter had not synced with the grid
    # yet, which says nothing about its potential, so assume the rating
    power = status.get("apower") or inverter["power"]
    _last_probe[id] = (time.monotonic(), power)
    log.info("Inverter %s probed at %.0f W", id, power)
    return power


def _expected_power(inverter: dict) -> float:
    """Best guess for an inverter whose meter cannot be believed right now."""
    if last := _last_probe.get(inverter["id"]):
        return last[1]
    return inverter["power"]


async def _effective_capacities(
    states: dict[int, bool], readings: dict[int, float | None]
) -> list[tuple[dict, float]]:
    """Power each controllable inverter can contribute right now.

    Monitored inverters use their live measurement; off ones are probed
    (sequentially, at most once per PROBE_MIN_INTERVAL). Unmonitored
    inverters and unreadable monitors fall back to the rating.
    """
    capacities = []
    for inverter in INVERTERS:
        id = inverter["id"]
        if id not in states:
            continue  # switch unreachable: cannot control it this cycle
        if inverter["monitor"] is None or readings.get(id) is None:
            power = inverter["power"]
        elif reading := readings[id]:
            power = reading
        elif states[id]:
            if time.monotonic() - _last_on.get(id, float("-inf")) < STARTUP_GRACE:
                power = _expected_power(inverter)
            else:
                # Running for a while yet producing nothing: believe the meter
                power = 0
        elif (last := _last_probe.get(id)) and (
            time.monotonic() - last[0] < PROBE_MIN_INTERVAL
        ):
            power = last[1]
        else:
            try:
                power = await _probe_inverter(inverter)
            except Exception as e:
                log.warning("Probing inverter %s failed: %r", id, e)
                power = inverter["power"]
        capacities.append((inverter, power))
    return capacities


def find_best_inverter_combination(
    consumption: float, capacities: list[tuple[dict, float]]
) -> list[dict]:
    """Find the inverters whose combined power gets closest to the given
    consumption without exceeding it, so no power is fed back to the grid."""
    best: list[dict] = []
    best_power = 0
    for r in range(1, len(capacities) + 1):
        for combo in combinations(capacities, r):
            total = sum(power for _, power in combo)
            if best_power < total <= consumption:
                best = [inverter for inverter, _ in combo]
                best_power = total
    return best


async def update():
    """One control cycle: match running inverters to current consumption."""
    grid, states, readings = await asyncio.gather(
        get_grid_power(), _read_switch_states(), _read_monitors()
    )

    production = 0.0
    for inverter in INVERTERS:
        id = inverter["id"]
        if inverter["monitor"] is not None and readings.get(id) is not None:
            production += readings[id]
        elif states.get(id):
            production += inverter["power"]
    consumption = grid + production

    capacities = await _effective_capacities(states, readings)
    power_of = {inv["id"]: power for inv, power in capacities}

    best_ids = {
        inv["id"] for inv in find_best_inverter_combination(consumption, capacities)
    }
    current_ids = {id for id, on in states.items() if on}
    best_total = sum(power_of[id] for id in best_ids)
    current_total = sum(power_of[id] for id in current_ids)

    log.info(
        "Grid %.0f W, production %.0f W, consumption %.0f W, inverters on %s",
        grid, production, consumption, sorted(current_ids),
    )

    if best_ids == current_ids:
        return
    exporting = grid < 0
    if not exporting and best_total - current_total <= HYSTERESIS:
        log.info(
            "Keeping inverters %s: %.0f W improvement is within hysteresis",
            sorted(current_ids), best_total - current_total,
        )
        return

    log.info(
        "Switching to inverters %s: %.0f W of %.0f W%s",
        sorted(best_ids), best_total, consumption,
        " (exporting)" if exporting else "",
    )
    changes = [
        (id, id in best_ids)
        for id in states
        if (id in best_ids) != (id in current_ids)
    ]
    results = await asyncio.gather(
        *(set_switch_state(id, on) for id, on in changes), return_exceptions=True
    )
    for (id, on), result in zip(changes, results):
        if isinstance(result, BaseException):
            log.warning(
                "Failed to switch inverter %s %s: %r", id, "on" if on else "off", result
            )


async def run():
    try:
        while True:
            try:
                await update()
            except Exception:
                log.exception("Update failed")
            await asyncio.sleep(UPDATE_INTERVAL)
    finally:
        await _shutdown_devices()


def main():
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
