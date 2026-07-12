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


def main():
    pass


if __name__ == "__main__":
    main()
