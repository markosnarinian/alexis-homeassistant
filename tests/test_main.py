import asyncio

import pytest

import main

METER = "192.168.1.100"
SWITCH = {1: "192.168.1.101", 2: "192.168.1.102", 3: "192.168.1.103",
          4: "192.168.1.104", 5: "192.168.1.105"}
MONITOR = {1: "192.168.1.111", 2: "192.168.1.112"}


class FakeShelly:
    """Simulated grid meter, switches and monitors behind _device_rpc."""

    def __init__(self, grid=0.0, on=(), producing=None):
        self.grid = grid
        self.on = {id: id in on for id in SWITCH}
        # What each monitored inverter produces while switched on
        self.producing = producing or {}
        self.set_calls = []
        self.unreachable = set()

    async def rpc(self, device, method, params=None):
        ip = device["ip"]
        if ip in self.unreachable:
            raise ConnectionError(ip)
        if ip == METER:
            return {"act_power": self.grid}
        for id, monitor_ip in MONITOR.items():
            if ip == monitor_ip:
                return {"apower": self.producing.get(id, 0.0) if self.on[id] else 0.0}
        id = next(id for id, switch_ip in SWITCH.items() if ip == switch_ip)
        if method == "Switch.Set":
            self.set_calls.append((id, params["on"]))
            self.on[id] = params["on"]
            return {}
        return {"output": self.on[id]}


@pytest.fixture
def shelly(monkeypatch):
    fake = FakeShelly()
    monkeypatch.setattr(main, "_device_rpc", fake.rpc)
    monkeypatch.setattr(main, "PROBE_SECONDS", 0)
    monkeypatch.setattr(main, "_last_probe", {})
    monkeypatch.setattr(main, "_last_on", {})
    return fake


def capacities(values: dict[int, float]):
    return [(main._get_inverter(id), power) for id, power in values.items()]


def test_find_best_combination():
    caps = capacities({1: 800, 2: 800, 3: 600, 4: 1200, 5: 2000})
    cases = {
        0: [], 500: [], 600: [3], 1000: [1], 1500: [1, 3],
        2100: [5], 3300: [4, 5], 10000: [1, 2, 3, 4, 5],
    }
    for consumption, expected in cases.items():
        best = main.find_best_inverter_combination(consumption, caps)
        assert [inv["id"] for inv in best] == expected, consumption


def test_find_best_uses_measured_power():
    caps = capacities({1: 750, 2: 620})
    best = main.find_best_inverter_combination(1400, caps)
    assert {inv["id"] for inv in best} == {1, 2}


def test_update_switches_to_best_combination(shelly):
    # Inverter 1 running at 750 W; consumption 1500 W; inverter 2 would
    # produce 620 W. Best match: 1 + 2 = 1370 W.
    shelly.on[1] = True
    shelly.producing = {1: 750.0, 2: 620.0}
    shelly.grid = 750.0  # consumption = 750 grid + 750 production
    asyncio.run(main.update())
    assert shelly.on == {1: True, 2: True, 3: False, 4: False, 5: False}


def test_probe_result_is_cached(shelly):
    shelly.producing = {2: 620.0}
    states = {id: False for id in SWITCH}
    readings = {1: 0.0, 2: 0.0}

    asyncio.run(main._effective_capacities(states, readings))
    probes = len(shelly.set_calls)
    assert probes == 4  # inverters 1 and 2 probed: each switched on then off

    asyncio.run(main._effective_capacities(states, readings))
    assert len(shelly.set_calls) == probes  # cached, no new probes
    assert main._last_probe[2][1] == 620.0


def test_zero_probe_falls_back_to_rating(shelly):
    # Inverter 2 produces nothing even when switched on (e.g. still syncing)
    states = {id: False for id in SWITCH}
    caps = asyncio.run(main._effective_capacities(states, {1: 0.0, 2: 0.0}))
    assert dict((inv["id"], p) for inv, p in caps)[2] == 800


def test_startup_grace_trusts_expected_power(shelly, monkeypatch):
    # Inverter 1 just switched on, meter still reads 0: not believed
    states = {1: True}
    monkeypatch.setattr(main, "INVERTERS", [main._get_inverter(1)])
    main._last_on[1] = __import__("time").monotonic()
    main._last_probe[1] = (main._last_on[1], 700.0)
    caps = asyncio.run(main._effective_capacities(states, {1: 0.0}))
    assert caps[0][1] == 700.0

    # Past the grace period the 0 reading is believed
    main._last_on[1] -= main.STARTUP_GRACE + 1
    caps = asyncio.run(main._effective_capacities(states, {1: 0.0}))
    assert caps[0][1] == 0


def test_unreadable_monitor_falls_back_to_rating(shelly):
    caps = asyncio.run(main._effective_capacities({1: True}, {1: None}))
    assert caps[0] == (main._get_inverter(1), 800)


def test_unreachable_switch_is_excluded(shelly):
    shelly.unreachable = {SWITCH[3]}
    states = asyncio.run(main._read_switch_states())
    assert 3 not in states
    caps = asyncio.run(main._effective_capacities(states, {1: 100.0, 2: 100.0}))
    assert 3 not in {inv["id"] for inv, _ in caps}


def test_hysteresis_keeps_current_combination(shelly):
    # Inverter 1 covers 750 W of 800 W consumption; switching to 1+2 would
    # only add 40 W, below the hysteresis threshold.
    shelly.on[1] = True
    shelly.producing = {1: 750.0}
    shelly.grid = 50.0
    main._last_probe[2] = (__import__("time").monotonic(), 40.0)
    asyncio.run(main.update())
    assert shelly.set_calls == []


def test_export_forces_reconfiguration(shelly):
    # Exporting 200 W: inverter 1 produces 750 W but consumption is 550 W.
    # Cached probe says inverter 2 yields 500 W: switch 1 off, 2 on.
    shelly.on[1] = True
    shelly.producing = {1: 750.0, 2: 500.0}
    shelly.grid = -200.0
    main._last_probe[2] = (__import__("time").monotonic(), 500.0)
    asyncio.run(main.update())
    assert shelly.on[1] is False
    assert shelly.on[2] is True


def test_consumption_includes_unmonitored_production(shelly):
    # Unmonitored inverter 3 (600 W) is on: production is estimated at its
    # rating, so consumption = 100 grid + 600 = 700 and inverter 3 stays on.
    shelly.on[3] = True
    shelly.grid = 100.0
    # Avoid probing noise from monitored inverters
    main._last_probe[1] = (__import__("time").monotonic(), 800.0)
    main._last_probe[2] = (__import__("time").monotonic(), 800.0)
    asyncio.run(main.update())
    assert shelly.on[3] is True
    assert shelly.set_calls == []
