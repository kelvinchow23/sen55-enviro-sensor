"""Runtime configuration, read from environment variables (Docker-native).

Every setting has a sane default so the service runs with zero config. Override
any value from docker-compose.yml without rebuilding the image.
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# How often the sampler reads the sensor and writes one row (seconds).
SAMPLE_INTERVAL_S = _float("SAMPLE_INTERVAL_S", 5.0)

# Rows older than this are pruned. 0 disables pruning (keep everything).
RETENTION_DAYS = _int("RETENTION_DAYS", 30)

# Where the SQLite file lives inside the container. Mapped to a Docker volume
# so data survives container rebuilds and Pi reboots.
DB_PATH = os.environ.get("DB_PATH", "/data/readings.db")

# I2C bus the SEN5x is on (Pi Zero 2 W -> bus 1).
I2C_BUS = _int("I2C_BUS", 1)

# TCP port the HTTP API listens on inside the container.
API_PORT = _int("API_PORT", 8000)

# Max rows any single /data response may return, to protect the Pi's memory
# from an unbounded query. Clients paginate with after_id to read more.
MAX_LIMIT = _int("MAX_LIMIT", 5000)

# How often (in sampler loop iterations) to run the retention prune. Pruning
# every tick is wasteful; once every ~this many samples is plenty.
PRUNE_EVERY_N_SAMPLES = _int("PRUNE_EVERY_N_SAMPLES", 720)
