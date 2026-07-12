from itertools import combinations

import aiohttp
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice

import settings


async def _switch_rpc(id: int, method: str, params: dict | None = None):
    inverter = next(inv for inv in settings.INVERTERS if inv["id"] == id)
    switch = inverter["switch"]

    auth = switch.get("auth") or {}
    options = ConnectionOptions(
        switch["ip"],
        username=auth.get("username"),
        password=auth.get("password"),
    )
    async with aiohttp.ClientSession() as session:
        device = await RpcDevice.create(session, None, options)
        await device.initialize()
        try:
            return await device.call_rpc(
                method, {"id": switch["channel"], **(params or {})}
            )
        finally:
            await device.shutdown()


async def set_switch_state(id: int, on: bool):
    await _switch_rpc(id, "Switch.Set", {"on": on})


async def get_switch_state(id: int) -> bool:
    status = await _switch_rpc(id, "Switch.GetStatus")
    return status["output"]


def find_best_inverter_combination(consumption: float) -> list[dict]:
    """Find the inverters whose combined power gets closest to the given
    consumption without exceeding it, so no power is fed back to the grid."""
    best: list[dict] = []
    best_power = 0
    for r in range(1, len(settings.INVERTERS) + 1):
        for combo in combinations(settings.INVERTERS, r):
            total = sum(inv["power"] for inv in combo)
            if best_power < total <= consumption:
                best, best_power = list(combo), total
    return best


def main():
    pass


if __name__ == "__main__":
    main()
