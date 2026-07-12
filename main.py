import asyncio
import time
from itertools import combinations

import aiohttp
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice

from energy import get_consumption

# Seconds between control loop updates
UPDATE_INTERVAL = 60

# How long to switch on an off inverter to measure its actual output
PROBE_SECONDS = 15

# Minimum time between probes of the same inverter; within this interval
# the last probed value is reused
PROBE_MIN_INTERVAL = 15 * 60

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


async def _device_rpc(device: dict, method: str, params: dict | None = None):
    auth = device.get("auth") or {}
    options = ConnectionOptions(
        device["ip"],
        username=auth.get("username"),
        password=auth.get("password"),
    )
    async with aiohttp.ClientSession() as session:
        shelly = await RpcDevice.create(session, None, options)
        await shelly.initialize()
        try:
            return await shelly.call_rpc(
                method, {"id": device["channel"], **(params or {})}
            )
        finally:
            await shelly.shutdown()


# Last probe per inverter id: (monotonic timestamp, measured power)
_last_probe: dict[int, tuple[float, float]] = {}


def _get_inverter(id: int) -> dict:
    return next(inv for inv in INVERTERS if inv["id"] == id)


async def set_switch_state(id: int, on: bool):
    await _device_rpc(_get_inverter(id)["switch"], "Switch.Set", {"on": on})


async def get_switch_state(id: int) -> bool:
    status = await _device_rpc(_get_inverter(id)["switch"], "Switch.GetStatus")
    return status["output"]


async def get_inverter_power(id: int) -> float:
    """Effective power of an inverter: the actual output measured by its
    monitor when one is configured, its rating otherwise. A monitored
    inverter that measures 0 W because it is switched off is probed: turned
    on for PROBE_SECONDS, measured, and turned back off."""
    inverter = _get_inverter(id)
    monitor = inverter.get("monitor")
    if monitor is None:
        return inverter["power"]

    status = await _device_rpc(monitor, "Switch.GetStatus")
    if apower := status.get("apower"):
        return apower
    if await get_switch_state(id):
        # On but producing nothing (e.g. after dark): believe the meter
        return 0
    if last := _last_probe.get(id):
        timestamp, power = last
        if time.monotonic() - timestamp < PROBE_MIN_INTERVAL:
            return power
    await set_switch_state(id, True)
    try:
        await asyncio.sleep(PROBE_SECONDS)
        status = await _device_rpc(monitor, "Switch.GetStatus")
    finally:
        await set_switch_state(id, False)
    power = status.get("apower") or 0
    _last_probe[id] = (time.monotonic(), power)
    return power


async def find_best_inverter_combination(consumption: float) -> list[dict]:
    """Find the inverters whose combined power gets closest to the given
    consumption without exceeding it, so no power is fed back to the grid."""
    powers = await asyncio.gather(
        *(get_inverter_power(inv["id"]) for inv in INVERTERS)
    )
    candidates = list(zip(INVERTERS, powers))

    best: list[dict] = []
    best_power = 0
    for r in range(1, len(candidates) + 1):
        for combo in combinations(candidates, r):
            total = sum(power for _, power in combo)
            if best_power < total <= consumption:
                best = [inverter for inverter, _ in combo]
                best_power = total
    return best


async def update():
    """One control cycle: match running inverters to current consumption."""
    consumption = get_consumption()
    combination = await find_best_inverter_combination(consumption)
    ids = {inverter["id"] for inverter in combination}
    print(f"Consumption {consumption} W -> switching on inverters {sorted(ids)}")
    await asyncio.gather(
        *(
            set_switch_state(inverter["id"], inverter["id"] in ids)
            for inverter in INVERTERS
        )
    )


async def run():
    while True:
        try:
            await update()
        except Exception as e:
            print(f"Update failed: {e}")
        await asyncio.sleep(UPDATE_INTERVAL)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
