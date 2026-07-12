import asyncio
import time
from itertools import combinations

import aiohttp
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice

import settings


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
    return next(inv for inv in settings.INVERTERS if inv["id"] == id)


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
        if time.monotonic() - timestamp < settings.PROBE_MIN_INTERVAL:
            return power
    await set_switch_state(id, True)
    try:
        await asyncio.sleep(settings.PROBE_SECONDS)
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
        *(get_inverter_power(inv["id"]) for inv in settings.INVERTERS)
    )
    candidates = list(zip(settings.INVERTERS, powers))

    best: list[dict] = []
    best_power = 0
    for r in range(1, len(candidates) + 1):
        for combo in combinations(candidates, r):
            total = sum(power for _, power in combo)
            if best_power < total <= consumption:
                best = [inverter for inverter, _ in combo]
                best_power = total
    return best


def main():
    pass


if __name__ == "__main__":
    main()
