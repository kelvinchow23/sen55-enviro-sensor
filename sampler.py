"""Background sampler: reads the SEN5x on a fixed cadence and persists rows.

Runs in its own daemon thread, launched from app.py on startup. Kept entirely
separate from request handling so a slow or dead HTTP client can never stall
sampling, and a sensor hiccup can never break the API.

Error policy (per the design decision):
* A successful read stores every signal plus device-status health flags.
* A hard failure (CRC mismatch, I2C exception) is logged and the sample is
  SKIPPED -- no fake/NULL row is written. Staleness is detectable via /health,
  which reports seconds-since-last-successful-sample.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import storage
from sen55 import SEN5x

log = logging.getLogger("sampler")

# Number of consecutive read failures before we tear down and re-init the
# sensor. A single failure (e.g. the first poll landing before warm-up, or a
# one-off bus glitch) is tolerated without a disruptive reset/start cycle.
REINIT_AFTER_READ_ERRORS = 5

# Seconds to wait after start() before the first measurement read. The SEN5x
# needs the fan/laser to spin up and a first measurement to complete (~1 s per
# the datasheet); reading before then returns an I2C I/O error.
WARMUP_AFTER_START_S = 2.0


def _utc_now_iso() -> str:
    # Second resolution is plenty for a multi-second sample interval, and keeps
    # timestamps tidy. Always UTC, always suffixed 'Z'.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Sampler:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Shared health state, read by the API thread. Guarded by _lock.
        self._lock = threading.Lock()
        self._last_sample_monotonic: float | None = None
        self._last_status_ok: bool | None = None
        self._last_status_raw: int | None = None
        self._last_error: str | None = None
        self._consecutive_errors = 0
        self._started_monotonic = time.monotonic()
        # Device identity, filled in once at startup. Read by /config.
        self.device = {
            "product_name": None,
            "serial_number": None,
            "firmware_version": None,
        }

    # --- lifecycle ------------------------------------------------------------
    def start(self):
        storage.init_db()
        self._thread = threading.Thread(target=self._run, name="sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                # Thread is wedged (e.g. blocked in a long I2C transaction that
                # doesn't observe the stop Event). It's a daemon thread so the
                # process can still exit, but surface it rather than pretending
                # shutdown was clean.
                log.warning("sampler thread did not stop within 10s; "
                            "it may be blocked mid-I2C-transaction")

    # --- health snapshot (thread-safe) ---------------------------------------
    def health(self) -> dict:
        with self._lock:
            last = self._last_sample_monotonic
            age = None if last is None else round(time.monotonic() - last, 1)
            thread_alive = bool(self._thread and self._thread.is_alive())
            fresh = age is not None and age < max(30.0,
                                                  config.SAMPLE_INTERVAL_S * 4)
            # Three distinct states so a consumer can tell "warming up" from
            # "dead". Before the first successful sample (age is None) we're
            # "starting", not "unhealthy" -- the sensor needs a few seconds
            # after reset/start before data_ready() goes true.
            if not thread_alive:
                state = "dead"
            elif last is None:
                state = "starting"
            elif fresh:
                state = "ok"
            else:
                state = "stale"
            return {
                "sampler_alive": thread_alive,
                "state": state,
                "healthy": state == "ok",
                "seconds_since_last_sample": age,
                "last_status_ok": self._last_status_ok,
                "last_status_raw": self._last_status_raw,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "uptime_seconds": round(time.monotonic()
                                        - self._started_monotonic, 1),
            }

    # --- main loop ------------------------------------------------------------
    def _run(self):
        try:
            self._sample_loop()
        except Exception:  # pragma: no cover - last-resort guard
            log.exception("sampler thread crashed")

    def _sample_loop(self):
        interval = max(1.0, config.SAMPLE_INTERVAL_S)
        sen = None
        sample_count = 0
        read_errors = 0  # consecutive read failures on the current handle

        while not self._stop.is_set():
            cycle_start = time.monotonic()

            # (Re)initialize the sensor if we don't have a working handle.
            if sen is None:
                sen = self._init_sensor()
                if sen is None:
                    # Couldn't open the sensor; back off and retry.
                    self._record_error("sensor init failed")
                    self._interruptible_sleep(self._backoff(interval))
                    continue
                read_errors = 0

            try:
                if sen.data_ready():
                    values = sen.read()
                    status_ok, status_raw = self._read_status(sen)
                    storage.insert_reading(
                        _utc_now_iso(), values, status_ok, status_raw
                    )
                    self._record_success(status_ok, status_raw)
                    read_errors = 0
                    sample_count += 1
                    if (config.RETENTION_DAYS > 0 and sample_count
                            % config.PRUNE_EVERY_N_SAMPLES == 0):
                        self._prune()
                else:
                    # Sensor answered but has no new measurement yet (normal
                    # right after start() and between the ~1 Hz update cadence).
                    # A clean not-ready is not an error -- clear the counter.
                    read_errors = 0
            except Exception as e:
                # data_ready()/read() I2C glitch or CRC mismatch. Do NOT tear
                # the sensor down on a single failure -- that caused a
                # reset/start churn loop when the very first read fired before
                # the sensor was warmed up. Only re-init after several
                # consecutive failures, which indicates a real bus/device fault
                # rather than a transient hiccup.
                read_errors += 1
                self._record_error(f"{type(e).__name__}: {e}")
                log.warning("read failed (%d in a row), skipping sample: %s",
                            read_errors, e)
                if read_errors >= REINIT_AFTER_READ_ERRORS:
                    log.warning("re-initializing sensor after %d read errors",
                                read_errors)
                    try:
                        sen.close()
                    except Exception:
                        pass
                    sen = None
                    read_errors = 0
                    self._interruptible_sleep(self._backoff(interval))
                    continue

            # Sleep the remainder of the interval (never negative).
            elapsed = time.monotonic() - cycle_start
            self._interruptible_sleep(max(0.0, interval - elapsed))

        # Clean shutdown.
        if sen is not None:
            try:
                sen.stop()
                sen.close()
            except Exception:
                pass

    # --- helpers --------------------------------------------------------------
    def _backoff(self, base: float) -> float:
        """Sleep duration after a failure. Grows with consecutive errors so a
        persistent fault doesn't hammer reset()/start() on the bus every
        interval, capped so recovery latency stays bounded."""
        with self._lock:
            n = self._consecutive_errors
        return min(base * (2 ** min(n, 5)), 60.0)

    def _init_sensor(self) -> SEN5x | None:
        sen = None
        try:
            sen = SEN5x(bus_num=config.I2C_BUS)
            sen.reset()
            # Capture identity once (best-effort; failures here aren't fatal).
            try:
                self.device = {
                    "product_name": sen.product_name(),
                    "serial_number": sen.serial_number(),
                    "firmware_version": sen.firmware_version(),
                }
            except Exception as e:
                log.warning("could not read device identity: %s", e)
            sen.start()
            # Let the fan/laser spin up and the first measurement complete
            # before the loop attempts a read (avoids an immediate I/O error).
            self._interruptible_sleep(WARMUP_AFTER_START_S)
            log.info("sensor started: %s", self.device)
            return sen
        except Exception as e:
            log.error("sensor init error: %s", e)
            # Close the fd opened by the SEN5x constructor; otherwise a flaky
            # bus that fails reset()/start() every retry leaks one i2c fd per
            # attempt until the process runs out of descriptors.
            if sen is not None:
                try:
                    sen.close()
                except Exception:
                    pass
            return None

    @staticmethod
    def _read_status(sen: SEN5x) -> tuple[bool, int | None]:
        """Read device status; ok == no error/fault bits set.

        If the status read itself fails, report ok=False with raw=None rather
        than masking it as healthy -- a failed status transaction is exactly
        the case a consumer needs to see. status_raw=None distinguishes
        "couldn't read status" from a real fault register value.
        """
        try:
            st = sen.device_status()
            raw = st["raw"]
            faults = (
                st["gas_sensor_error"] or st["rht_comm_error"]
                or st["laser_failure"] or st["fan_failure"]
                or st["fan_speed_out_of_range"]
            )
            return (not faults), raw
        except Exception:
            return False, None

    def _prune(self):
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=config.RETENTION_DAYS)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            deleted = storage.prune_older_than(cutoff)
            if deleted:
                log.info("pruned %d rows older than %s", deleted, cutoff)
        except Exception as e:
            log.warning("prune failed: %s", e)

    def _record_success(self, status_ok: bool, status_raw: int | None):
        with self._lock:
            self._last_sample_monotonic = time.monotonic()
            self._last_status_ok = status_ok
            self._last_status_raw = status_raw
            self._last_error = None
            self._consecutive_errors = 0

    def _record_error(self, msg: str):
        with self._lock:
            self._last_error = msg
            self._consecutive_errors += 1

    def _interruptible_sleep(self, seconds: float):
        # Wake early if a stop is requested, so shutdown is prompt.
        self._stop.wait(timeout=seconds)
