#!/usr/bin/env python3
"""
SEN5x (SEN50/54/55) I2C driver for Raspberry Pi via raw smbus2 transactions.

The SEN5x does NOT use the standard SMBus register model. Commands are bare
16-bit codes written big-endian. The command code carries its own 3-bit CRC,
so no checksum is appended to commands. Read-back data arrives as 2-byte words,
each followed by a CRC-8 byte (poly 0x31, init 0xFF) which we verify.

    pip install smbus2
    i2cdetect -y 1        # confirm 0x69 shows up first

Datasheet: SEN5x v2 D1, March 2022. Address 0x69, 100 kbit/s, no clock stretch.
"""

import time
from smbus2 import SMBus, i2c_msg

SEN5X_ADDR = 0x69
I2C_BUS = 1  # Pi Zero 2 W -> /dev/i2c-1

# --- Command codes (16-bit). See datasheet Table 12. --------------------------
CMD_START_MEASURE      = 0x0021
CMD_START_RHT_GAS_ONLY = 0x0037   # SEN54/55: no PM, fan+laser off, low power
CMD_STOP_MEASURE       = 0x0104
CMD_READ_DATA_READY    = 0x0202
CMD_READ_MEASURED      = 0x03C4
CMD_START_FAN_CLEAN    = 0x5607   # Measurement-Mode only; 10 s at max fan speed
CMD_AUTO_CLEAN_IVL     = 0x8004
CMD_TEMP_OFFSET        = 0x60B2   # RH/T self-heating compensation (read/write)
CMD_READ_PRODUCT_NAME  = 0xD014
CMD_READ_SERIAL        = 0xD033
CMD_READ_FIRMWARE      = 0xD100
CMD_READ_STATUS        = 0xD206
CMD_CLEAR_STATUS       = 0xD210
CMD_RESET              = 0xD304

# Per-command execution time (ms) you must wait before reading back.
EXEC_MS = {
    CMD_STOP_MEASURE: 200,
    CMD_RESET: 100,
    CMD_READ_MEASURED: 20,
    # everything else: 20 ms is safe
}


# --- CRC-8 (verified against datasheet vector CRC(0xBEEF) = 0x92) -------------
def crc8(data: bytes) -> int:
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _to_signed16(u: int) -> int:
    return u - 0x10000 if u >= 0x8000 else u


class SEN5x:
    def __init__(self, bus_num: int = I2C_BUS, addr: int = SEN5X_ADDR):
        self.addr = addr
        self.bus = SMBus(bus_num)

    def close(self):
        self.bus.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Best-effort: leave the sensor idle when we're done.
        try:
            self.stop()
        except Exception:
            pass
        self.close()

    # --- low-level ------------------------------------------------------------
    def _send(self, cmd: int):
        """Write a bare 16-bit command code, no CRC."""
        msg = i2c_msg.write(self.addr, [(cmd >> 8) & 0xFF, cmd & 0xFF])
        self.bus.i2c_rdwr(msg)

    def _read_words(self, cmd: int, num_words: int) -> list[int]:
        """Send a command, wait, then read num_words 16-bit words (CRC-checked)."""
        self._send(cmd)
        time.sleep(EXEC_MS.get(cmd, 20) / 1000.0)
        n = num_words * 3
        rd = i2c_msg.read(self.addr, n)
        self.bus.i2c_rdwr(rd)
        raw = list(rd)
        words = []
        for i in range(0, n, 3):
            payload, crc = raw[i:i + 2], raw[i + 2]
            if crc8(bytes(payload)) != crc:
                raise IOError(f"CRC mismatch at word {i // 3} "
                              f"(got {crc:#04x}, want {crc8(bytes(payload)):#04x})")
            words.append((payload[0] << 8) | payload[1])
        return words

    def _read_string(self, cmd: int) -> str:
        words = self._read_words(cmd, 16)  # 32 ASCII chars, null-terminated
        chars = bytes(b for w in words for b in ((w >> 8) & 0xFF, w & 0xFF))
        return chars.split(b"\x00")[0].decode("ascii", errors="replace")

    def _write_words(self, cmd: int, words: list[int]):
        """Write a command code followed by data words, each with its CRC byte."""
        payload = [(cmd >> 8) & 0xFF, cmd & 0xFF]
        for w in words:
            hi, lo = (w >> 8) & 0xFF, w & 0xFF
            payload += [hi, lo, crc8(bytes([hi, lo]))]
        self.bus.i2c_rdwr(i2c_msg.write(self.addr, payload))
        time.sleep(EXEC_MS.get(cmd, 20) / 1000.0)

    # --- control --------------------------------------------------------------
    def reset(self):
        self._send(CMD_RESET)
        time.sleep(EXEC_MS[CMD_RESET] / 1000.0)

    def start(self):
        self._send(CMD_START_MEASURE)

    def start_rht_gas_only(self):
        self._send(CMD_START_RHT_GAS_ONLY)

    def stop(self):
        self._send(CMD_STOP_MEASURE)
        time.sleep(EXEC_MS[CMD_STOP_MEASURE] / 1000.0)

    def start_fan_clean(self):
        """10 s max-speed clean. Only valid while measuring; no data during clean."""
        self._send(CMD_START_FAN_CLEAN)

    def clear_status(self):
        self._send(CMD_CLEAR_STATUS)

    # --- info -----------------------------------------------------------------
    def product_name(self) -> str:
        return self._read_string(CMD_READ_PRODUCT_NAME)

    def serial_number(self) -> str:
        return self._read_string(CMD_READ_SERIAL)

    def firmware_version(self) -> int:
        return self._read_words(CMD_READ_FIRMWARE, 1)[0] >> 8  # byte 0

    def device_status(self) -> dict:
        w = self._read_words(CMD_READ_STATUS, 2)
        reg = (w[0] << 16) | w[1]  # MSB word first
        return {
            "raw": reg,
            "fan_speed_out_of_range": bool(reg & (1 << 21)),  # SPEED
            "fan_cleaning_active":    bool(reg & (1 << 19)),  # FAN (info)
            "gas_sensor_error":       bool(reg & (1 << 7)),
            "rht_comm_error":         bool(reg & (1 << 6)),
            "laser_failure":          bool(reg & (1 << 5)),
            "fan_failure":            bool(reg & (1 << 4)),   # blocked/broken
        }

    # --- temperature compensation ---------------------------------------------
    def get_temp_offset(self) -> dict:
        w = self._read_words(CMD_TEMP_OFFSET, 3)
        return {
            "offset_c": _to_signed16(w[0]) / 200,
            "slope": _to_signed16(w[1]) / 10000,
            "time_constant_s": w[2],
        }

    def set_temp_offset(self, offset_c: float = 0.0, slope: float = 0.0,
                        time_constant_s: int = 0):
        """
        Correct RH/T self-heating once the sensor is mounted in its enclosure.

            T_compensated = T_ambient + slope * T_ambient + offset_c

        offset_c        deg C. Almost always NEGATIVE -- the module reads high.
                        Measure it: run at thermal steady state beside a trusted
                        reference, then offset_c = T_reference - T_reported.
        slope           normalized slope (default 0). Leave 0 for a first pass;
                        only add it if the error scales with ambient temp.
        time_constant_s how fast new values ramp in (63% after this many seconds).
                        0 = apply immediately.

        Not idle-only -- can be written while measuring -- but the value is
        volatile, so re-apply on every boot.
        """
        off = round(offset_c * 200) & 0xFFFF     # int16, scale 200
        slp = round(slope * 10000) & 0xFFFF      # int16, scale 10000
        tc = int(time_constant_s) & 0xFFFF       # uint16, scale 1
        self._write_words(CMD_TEMP_OFFSET, [off, slp, tc])

    # --- measurement ----------------------------------------------------------
    def data_ready(self) -> bool:
        return bool(self._read_words(CMD_READ_DATA_READY, 1)[0] & 0x0001)

    def read(self) -> dict:
        """Read + scale all 8 signals. 0xFFFF (unknown / not in this mode) -> None."""
        w = self._read_words(CMD_READ_MEASURED, 8)

        def u(x, scale):  # unsigned
            return None if x == 0xFFFF else x / scale

        def s(x, scale):  # signed
            return None if x == 0xFFFF else _to_signed16(x) / scale

        return {
            "pm1_0":  u(w[0], 10),   # ug/m3
            "pm2_5":  u(w[1], 10),
            "pm4_0":  u(w[2], 10),
            "pm10":   u(w[3], 10),
            "rh":     s(w[4], 100),  # %RH   (scale 100 -- NOT the same as T!)
            "temp_c": s(w[5], 200),  # deg C (scale 200)
            "voc":    s(w[6], 10),   # index 1..500
            "nox":    s(w[7], 10),   # index 1..500 (SEN55 only)
        }


def main():
    with SEN5x() as sen:
        sen.reset()
        print(f"Product : {sen.product_name()}")
        print(f"Serial  : {sen.serial_number()}")
        print(f"Firmware: v{sen.firmware_version()}")

        sen.start()
        print("Measuring. PM stabilises in ~8-30 s; VOC hits spec <1 h, NOx <6 h.")
        print("Ctrl-C to stop.\n")

        try:
            while True:
                time.sleep(1)
                if not sen.data_ready():
                    continue
                m = sen.read()
                nox = "n/a" if m["nox"] is None else f"{m['nox']:.0f}"
                print(
                    f"PM2.5 {m['pm2_5']:6.1f}  PM10 {m['pm10']:6.1f} ug/m3 | "
                    f"{m['temp_c']:5.1f} C  {m['rh']:5.1f} %RH | "
                    f"VOC {m['voc']:5.1f}  NOx {nox}"
                )
        except KeyboardInterrupt:
            print("\nStopping.")


if __name__ == "__main__":
    main()