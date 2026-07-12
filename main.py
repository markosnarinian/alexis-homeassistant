import asyncio

import aiohttp
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice

import settings


async def set_switch_state(id: int, on: bool):
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
            await device.call_rpc("Switch.Set", {"id": switch["channel"], "on": on})
        finally:
            await device.shutdown()


async def set_switch_states(states: list[dict]):
    await asyncio.gather(
        *(set_switch_state(state["id"], state["on"]) for state in states)
    )


def main():
    pass


if __name__ == "__main__":
    main()
