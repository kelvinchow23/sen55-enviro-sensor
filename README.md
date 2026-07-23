# sen55-enviro-sensor

SEN5x (SEN50/54/55) environmental sensor on a Raspberry Pi Zero 2 W, dockerized.
Samples the sensor on a fixed cadence, stores every reading in SQLite, and
exposes a polling REST API so other machines on the network can pull the data.

## Architecture

```
 ┌──────────────┐   writes   ┌──────────────┐   reads   ┌──────────────┐
 │  Sampler      │──────────►│  SQLite       │◄─────────│  FastAPI      │◄── HTTP :8000
 │  (bg thread)  │  1 row /  │  readings.db  │           │  REST API     │    network clients
 │  sen55.SEN5x  │  interval │  (volume)     │           │  (uvicorn)    │
 └──────────────┘           └──────────────┘           └──────────────┘
```

Sampling and serving are decoupled: a slow/dead HTTP client can't stall
sampling, and a sensor hiccup can't break the API. Both run in one container.

| File | Role |
|------|------|
| `sen55.py`   | pure SEN5x I2C driver (also runnable standalone for debugging) |
| `config.py`  | env-var settings |
| `storage.py` | SQLite schema + queries |
| `sampler.py` | background thread: read sensor → store row |
| `app.py`     | FastAPI app + endpoints |

## Pi setup (one-time)

1. Enable I2C and confirm the sensor is detected:
   ```
   sudo raspi-config nonint do_i2c 0
   sudo reboot
   sudo apt install -y i2c-tools
   i2cdetect -y 1        # expect 0x69 to appear
   ```
2. Install Docker if needed:
   ```
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```
   Log out/in for the group change to take effect.

## Run

```
git clone https://github.com/kelvinchow23/sen55-enviro-sensor.git
cd sen55-enviro-sensor
docker compose up --build -d
docker compose logs -f          # watch startup
```

The API is then reachable from any machine on the tailnet at
`http://sdl2-pi0-enviornment-sensor:8000`.

## Configuration

Set in `docker-compose.yml` under `environment:` (no rebuild needed, just
`docker compose up -d` again):

| Variable | Default | Meaning |
|----------|---------|---------|
| `SAMPLE_INTERVAL_S` | `5`  | seconds between samples |
| `RETENTION_DAYS`    | `30` | prune rows older than this (`0` = keep all) |
| `DB_PATH`           | `/data/readings.db` | SQLite file (on the volume) |
| `I2C_BUS`           | `1`  | I2C bus number |
| `MAX_LIMIT`         | `5000` | max rows returned per request |

## API

All endpoints are `GET` and return JSON. No auth — access is gated by Tailscale.

### `GET /health`
Sampler liveness and staleness. `state` is one of `starting` (thread up, no
sample yet — normal for the first few seconds), `ok`, `stale` (thread alive but
no recent sample), or `dead` (thread gone). `healthy` is true only when
`state == "ok"`.
```json
{ "sampler_alive": true, "state": "ok", "healthy": true,
  "seconds_since_last_sample": 3.1, "last_status_ok": true,
  "last_status_raw": 0, "consecutive_errors": 0, "last_error": null,
  "uptime_seconds": 812.4, "max_id": 1620 }
```

### `GET /latest`
Single most-recent reading (404 until the first sample lands).

### `GET /config`
Device identity (product / serial / firmware) + current sampler settings.

### `GET /data`
Historical readings. Pick **one** query mode (combining them is a 400):

| Query | Returns |
|-------|---------|
| `?after_id=<cursor>` | rows with `id` greater than the cursor — **since last request** |
| `?since=2026-07-23T18:00:00Z` | rows at/after an ISO-8601 UTC timestamp |
| `?hours=24` / `?days=7` | rows within a trailing window |
| *(none)* | the last `?limit` rows |

All modes accept `?limit=N` (capped at `MAX_LIMIT`). Response:
```json
{ "count": 12, "next_cursor": 1632, "readings": [ { ...reading... }, ... ] }
```

Each reading:
```json
{ "id": 1632, "ts": "2026-07-23T18:42:07Z",
  "pm1_0": 2.9, "pm2_5": 3.3, "pm4_0": 3.3, "pm10": 3.3,
  "rh": 46.8, "temp_c": 23.9, "voc": 19.0, "nox": 1.0,
  "status_ok": true, "status_raw": 0 }
```

### Continuous "since last request" streaming (polling pattern)

The server is stateless; the cursor lives on the client. Read `next_cursor`
from each response and pass it back as `after_id` on the next poll — you get
exactly the rows added since you last asked, with no duplicates and no gaps,
even across client or server restarts.

```bash
# first call: grab recent history and a starting cursor
resp=$(curl -s "http://sdl2-pi0-enviornment-sensor:8000/data?limit=100")
cursor=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin)['next_cursor'])")

# subsequent polls: only what's new since last time
while true; do
  resp=$(curl -s "http://sdl2-pi0-enviornment-sensor:8000/data?after_id=$cursor")
  echo "$resp"
  cursor=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin)['next_cursor'])")
  sleep 10
done
```

## Standalone debugging

To bypass Docker and print readings directly on the Pi:
```
python sen55.py
```

## Notes / limitations

* The SEN55 VOC/NOx outputs are Sensirion's **relative index (1–500)**, not
  absolute concentrations — a sharp *rise* is the meaningful signal, not any
  fixed value. PM values are absolute µg/m³. This is a screening sensor, not a
  certified safety/exposure monitor.
* Temperature reads high in an enclosure (self-heating). `sen55.py` supports a
  calibration offset (`set_temp_offset`) if you later need corrected temp.
