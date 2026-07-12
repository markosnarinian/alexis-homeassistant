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
