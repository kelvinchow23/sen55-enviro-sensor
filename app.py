"""FastAPI service exposing SEN5x readings over a polling REST API.

Endpoints (all GET, JSON):
  /health          sampler liveness, staleness, last device status, row count
  /latest          single most-recent reading
  /data            historical readings; query modes are mutually exclusive:
                     ?after_id=<cursor>   -> rows newer than a cursor (since-last-request)
                     ?since=<ISO8601>     -> rows at/after a timestamp
                     ?hours=H / ?days=D   -> rows within a trailing window
                     (none)               -> last ?limit rows (default)
                   all modes accept ?limit=N (capped at config.MAX_LIMIT)
  /config          device identity + current sampler settings

The server is fully stateless. The "since last request" cursor lives on the
client: read `next_cursor` from a response and pass it back as ?after_id.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query

import config
import storage
from sampler import Sampler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

sampler = Sampler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting sampler (interval=%ss, retention=%sd, db=%s)",
             config.SAMPLE_INTERVAL_S, config.RETENTION_DAYS, config.DB_PATH)
    sampler.start()
    try:
        yield
    finally:
        log.info("stopping sampler")
        sampler.stop()


app = FastAPI(title="SEN55 Environmental Sensor API", lifespan=lifespan)


def _envelope(readings: list[dict], next_cursor: int) -> dict:
    """Wrap a result set with the cursor a client uses for its next poll.

    next_cursor must be computed by the caller from the SAME read snapshot as
    the query (see storage.query_after_id) so an empty batch can never advance
    the cursor past a row the query didn't return.
    """
    return {
        "count": len(readings),
        "next_cursor": next_cursor,
        "readings": readings,
    }


@app.get("/health")
def health():
    h = sampler.health()
    # max_id() is O(1) (PK); avoid a per-poll COUNT(*) scan that grows with the
    # table. This is the highest id ever assigned, i.e. lifetime sample count.
    h["max_id"] = storage.max_id()
    return h


@app.get("/config")
def get_config():
    return {
        "device": sampler.device,
        "sample_interval_s": config.SAMPLE_INTERVAL_S,
        "retention_days": config.RETENTION_DAYS,
        "max_limit": config.MAX_LIMIT,
        "i2c_bus": config.I2C_BUS,
    }


@app.get("/latest")
def latest():
    row = storage.latest()
    if row is None:
        raise HTTPException(status_code=404, detail="no readings yet")
    return row


@app.get("/data")
def data(
    after_id: int | None = Query(default=None, ge=0,
                                 description="return rows with id greater than this cursor"),
    since: str | None = Query(default=None,
                              description="ISO-8601 UTC timestamp, e.g. 2026-07-23T18:00:00Z"),
    hours: float | None = Query(default=None, gt=0,
                                description="trailing window in hours"),
    days: float | None = Query(default=None, gt=0,
                               description="trailing window in days"),
    limit: int = Query(default=config.MAX_LIMIT, ge=1),
):
    # Enforce mutually-exclusive query modes so results are unambiguous.
    modes = [("after_id", after_id is not None), ("since", since is not None),
             ("hours", hours is not None), ("days", days is not None)]
    active = [name for name, on in modes if on]
    if len(active) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"use only one of after_id, since, hours, days (got {active})",
        )

    if after_id is not None:
        # Cursor mode: the high-water id is read in the same snapshot as the
        # query, so an empty batch echoes the client's own cursor and can
        # never skip an unseen row.
        rows, next_cursor = storage.query_after_id(after_id, limit)
    elif since is not None:
        cutoff = _normalize_iso(since)
        rows = storage.query_since_ts(cutoff, limit)
        next_cursor = _cursor_from(rows)
    elif hours is not None or days is not None:
        window = timedelta(hours=hours or 0, days=days or 0)
        cutoff = (datetime.now(timezone.utc) - window
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = storage.query_since_ts(cutoff, limit)
        next_cursor = _cursor_from(rows)
    else:
        rows = storage.query_latest_n(limit)
        next_cursor = _cursor_from(rows)

    return _envelope(rows, next_cursor)


def _cursor_from(rows: list[dict]) -> int:
    """Next cursor for the non-after_id modes: last id in the batch, else 0.

    These modes are not the incremental-polling path, so a 0 fallback on an
    empty result is harmless (the client re-issues the same window query).
    """
    return rows[-1]["id"] if rows else 0


def _normalize_iso(value: str) -> str:
    """Parse a client ISO-8601 timestamp and re-emit it in the exact stored
    UTC '...Z' format, so the lexical string comparison in query_since_ts is
    always valid. Accepts offset timezones and space separators; a naive
    (tz-less) value is assumed to be UTC. Rejects unparseable input with 400.
    """
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="since must be ISO-8601, e.g. 2026-07-23T18:00:00Z",
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
