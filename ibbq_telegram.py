#!/usr/bin/env python3
"""
ibbq_telegram.py - Inkbird IBT-4XS -> Telegram bridge.

Runs forever. Continuously scans for the thermometer; the moment it powers on
and is in range, connects, streams probe temperatures, and reports to a Telegram
bot. Per-probe alert ranges are configured live via chat commands.

iBBQ protocol (Inkbird IBT-4XS):
  service  0xfff0
  fff1  notify  settings results (e.g. battery, alarm-silenced)
  fff2  write   credentials / pairing handshake
  fff4  notify  realtime probe data
  fff5  write   settings / control messages
Reference: https://gist.github.com/uucidl/b9c60b6d36d8080d085a8e3310621d64
"""

import asyncio
import json
import io
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
from bleak import BleakClient, BleakScanner

# --------------------------------------------------------------------------
# Configuration - via environment variables (see ibbq.env.example)
# --------------------------------------------------------------------------
BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]        # required
CHAT_ID     = os.environ["TELEGRAM_CHAT_ID"]          # required (where alerts go)
DEVICE_NAME = os.environ.get("IBBQ_NAME", "iBBQ")     # advertised BLE name
DEVICE_MAC  = os.environ.get("IBBQ_MAC", "").strip()  # optional; overrides name
STATE_FILE  = Path(os.environ.get("IBBQ_STATE", "probe_state.json"))

RESCAN_SECONDS    = 20    # how often to look for the device while it's off
RECONNECT_SECONDS = 10    # pause after a dropped/failed connection
SCAN_TIMEOUT      = 15    # seconds per scan attempt

# iBBQ realtime data is 0.1 C per count -> divide by 10.
# If your readings come out 10x off, change this to 1.0.
TEMP_DIVISOR  = 10.0
UNPLUGGED_RAW = 0xFFF0    # raw >= this means "no probe in this slot"
HYSTERESIS_C  = 2.0       # re-arm an alert only after temp moves back this far

# In-memory history for /graph (this cook only; cleared on each connection)
HISTORY_SAMPLE_SECONDS = 5      # store at most one sample this often
HISTORY_MAX            = 20000  # backstop cap (~28h at 5s) to bound memory

# iBBQ GATT UUIDs (16-bit shorthand expanded to full 128-bit)
UUID_CREDENTIALS = "0000fff2-0000-1000-8000-00805f9b34fb"
UUID_SETTINGS    = "0000fff5-0000-1000-8000-00805f9b34fb"
UUID_REALTIME    = "0000fff4-0000-1000-8000-00805f9b34fb"
UUID_RESULT      = "0000fff1-0000-1000-8000-00805f9b34fb"

# Fixed control messages (from the protocol reference)
MSG_CREDENTIALS     = bytes([0x21,0x07,0x06,0x05,0x04,0x03,0x02,0x01,
                             0xb8,0x22,0x00,0x00,0x00,0x00,0x00])
MSG_REALTIME_ENABLE = bytes([0x0B,0x01,0x00,0x00,0x00,0x00])
MSG_SILENCE_ALARM   = bytes([0x04,0xff,0x00,0x00,0x00,0x00])
MSG_BATTERY         = bytes([0x08,0x24,0x00,0x00,0x00,0x00])

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ibbq")


# --------------------------------------------------------------------------
# Temperature helpers - everything is stored internally in Celsius
# --------------------------------------------------------------------------
def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def fmt(c: Optional[float]) -> str:
    if c is None:
        return "-"
    return f"{c:.1f}C / {c_to_f(c):.1f}F"


def parse_temp(token: str) -> float:
    """'160c', '98C', '225f', '45' -> Celsius float. Defaults to Celsius."""
    token = token.strip().lower()
    unit = "c"
    if token and token[-1] in ("c", "f"):
        unit, token = token[-1], token[:-1]
    val = float(token)
    return val if unit == "c" else (val - 32) * 5 / 9


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
@dataclass
class ProbeConfig:
    target: Optional[float] = None   # ping once when reached
    low: Optional[float] = None      # ping when temp drops below this
    high: Optional[float] = None     # ping when temp rises above this
    # runtime "armed" flags (not persisted) - debounce so we don't spam
    target_armed: bool = True
    low_armed: bool = True
    high_armed: bool = True


class State:
    def __init__(self):
        self.probes: dict[int, ProbeConfig] = {}
        self.latest: dict[int, Optional[float]] = {}
        self.load()

    def cfg(self, idx: int) -> ProbeConfig:
        return self.probes.setdefault(idx, ProbeConfig())

    def load(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            for k, v in data.items():
                self.probes[int(k)] = ProbeConfig(
                    target=v.get("target"), low=v.get("low"), high=v.get("high"))
        except Exception as e:
            log.warning("could not load state: %s", e)

    def save(self):
        data = {str(i): {"target": c.target, "low": c.low, "high": c.high}
                for i, c in self.probes.items()}
        try:
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("could not save state: %s", e)


# --------------------------------------------------------------------------
# Telegram (raw Bot API over aiohttp - no heavy framework)
# --------------------------------------------------------------------------
class Telegram:
    def __init__(self, session: aiohttp.ClientSession, token: str, chat_id: str):
        self.s = session
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat = chat_id
        self.offset: Optional[int] = None

    async def send(self, text: str):
        try:
            await self.s.post(f"{self.base}/sendMessage",
                              json={"chat_id": self.chat, "text": text})
        except Exception as e:
            log.warning("telegram send failed: %s", e)

    async def send_photo(self, png: bytes, caption: str = ""):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.chat))
        if caption:
            form.add_field("caption", caption)
        form.add_field("photo", png, filename="graph.png",
                       content_type="image/png")
        try:
            await self.s.post(f"{self.base}/sendPhoto", data=form)
        except Exception as e:
            log.warning("telegram photo failed: %s", e)

    async def poll(self, handler):
        """Long-poll getUpdates and dispatch text messages to `handler`."""
        while True:
            try:
                params = {"timeout": 30}
                if self.offset is not None:
                    params["offset"] = self.offset
                async with self.s.get(f"{self.base}/getUpdates", params=params,
                                      timeout=aiohttp.ClientTimeout(total=40)) as r:
                    data = await r.json()
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg:
                        continue
                    text = (msg.get("text") or "").strip()
                    if text:
                        await handler(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll error: %s", e)
                await asyncio.sleep(3)


HELP = (
    "IBT-4XS bot commands:\n"
    "/status - current probe temps + configured alerts\n"
    "/graph - a chart of this cook so far\n"
    "/probeN target=160c low=45c high=102c - set alerts for probe N (1-4)\n"
    "    target: ping once when the probe reaches this temp\n"
    "    low/high: ping when the probe leaves this band\n"
    "    units: append c or f (e.g. 225f); default is Celsius\n"
    "/probeN clear - remove probe N's alerts\n"
    "/mute - silence the device's own buzzer\n"
    "/battery - ask the device for its battery level\n"
    "/help - show this message"
)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App:
    def __init__(self, session: aiohttp.ClientSession):
        self.state = State()
        self.tg = Telegram(session, BOT_TOKEN, CHAT_ID)
        self.client: Optional[BleakClient] = None
        # (timestamp, {probe_idx: temp_c}) for the current session
        self.history: deque = deque(maxlen=HISTORY_MAX)
        self._last_sample = 0.0

    # ---- graphing ----
    def _render_png(self) -> Optional[bytes]:
        """Render the session history to a PNG. Runs in a worker thread."""
        import matplotlib
        matplotlib.use("Agg")           # headless backend, no display needed
        import matplotlib.pyplot as plt

        hist = list(self.history)
        if not hist:
            return None
        t0 = hist[0][0]
        probe_ids = sorted({i for _, temps in hist
                            for i, v in temps.items() if v is not None})
        if not probe_ids:
            return None

        fig, ax = plt.subplots(figsize=(8, 4.5))
        for pid in probe_ids:
            xs, ys = [], []
            for ts, temps in hist:
                v = temps.get(pid)
                if v is not None:
                    xs.append((ts - t0) / 60.0)
                    ys.append(v)
            if xs:
                ax.plot(xs, ys, linewidth=1.8, label=f"Probe {pid + 1}")

        ax.set_xlabel("Minutes")
        ax.set_ylabel("Temperature (C)")
        ax.set_title("Current session")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")

        # right-hand axis in Fahrenheit, mapped from the same range
        ax2 = ax.twinx()
        lo, hi = ax.get_ylim()
        ax2.set_ylim(c_to_f(lo), c_to_f(hi))
        ax2.set_ylabel("Temperature (F)")

        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        return buf.getvalue()

    async def send_graph(self):
        if not self.history:
            await self.tg.send("No data yet this session "
                               "(is the thermometer connected?).")
            return
        try:
            png = await asyncio.to_thread(self._render_png)
        except ImportError:
            await self.tg.send("Graphing needs matplotlib. Install it with:\n"
                               "  ./venv/bin/pip install matplotlib")
            return
        except Exception as e:
            log.warning("graph render failed: %s", e)
            await self.tg.send("Couldn't render the graph.")
            return
        if not png:
            await self.tg.send("No probe data to plot yet.")
            return
        span = (self.history[-1][0] - self.history[0][0]) / 60.0
        await self.tg.send_photo(
            png, caption=f"Session so far: {span:.0f} min, "
                         f"{len(self.history)} samples")

    # ---- Telegram command handling ----
    async def handle_command(self, text: str):
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]  # strip @botname

        if cmd in ("help", "start"):
            await self.tg.send(HELP)
        elif cmd == "status":
            await self.tg.send(self.status_text())
        elif cmd == "graph":
            await self.send_graph()
        elif cmd in ("mute", "silence"):
            ok = await self.write_settings(MSG_SILENCE_ALARM)
            await self.tg.send("Silenced the device alarm." if ok
                               else "Not connected - can't silence right now.")
        elif cmd == "battery":
            ok = await self.write_settings(MSG_BATTERY)
            await self.tg.send("Requested battery level..." if ok
                               else "Not connected right now.")
        elif cmd.startswith("probe"):
            await self.handle_probe(cmd, parts[1:])
        else:
            await self.tg.send("Unknown command. Send /help for options.")

    async def handle_probe(self, cmd: str, args: list[str]):
        try:
            n = int(cmd[len("probe"):])
        except ValueError:
            await self.tg.send("Use /probe1 ... /probe4.")
            return
        if not 1 <= n <= 8:
            await self.tg.send("Probe number out of range.")
            return
        idx = n - 1
        cfg = self.state.cfg(idx)

        if args and args[0].lower() in ("clear", "off", "reset"):
            self.state.probes[idx] = ProbeConfig()
            self.state.save()
            await self.tg.send(f"Cleared alerts for probe {n}.")
            return

        changed = []
        for a in args:
            if "=" not in a:
                continue
            key, val = a.split("=", 1)
            key = key.lower()
            try:
                t = parse_temp(val)
            except ValueError:
                await self.tg.send(f"Couldn't read '{a}'.")
                return
            if key == "target":
                cfg.target, cfg.target_armed = t, True
                changed.append(f"target {fmt(t)}")
            elif key == "low":
                cfg.low, cfg.low_armed = t, True
                changed.append(f"low {fmt(t)}")
            elif key == "high":
                cfg.high, cfg.high_armed = t, True
                changed.append(f"high {fmt(t)}")

        if not changed:
            await self.tg.send(f"Probe {n}: {self.cfg_text(cfg)}")
            return
        self.state.save()
        await self.tg.send(f"Probe {n} set: " + ", ".join(changed))

    def cfg_text(self, cfg: ProbeConfig) -> str:
        bits = []
        if cfg.target is not None:
            bits.append(f"target {fmt(cfg.target)}")
        if cfg.low is not None:
            bits.append(f"low {fmt(cfg.low)}")
        if cfg.high is not None:
            bits.append(f"high {fmt(cfg.high)}")
        return ", ".join(bits) if bits else "no alerts set"

    def status_text(self) -> str:
        if not self.state.latest:
            out = "No live data yet (is the thermometer on and in range?)."
        else:
            out = "\n".join(f"Probe {i+1}: {fmt(self.state.latest[i])}"
                            for i in sorted(self.state.latest))
        cfgs = [f"Probe {i+1}: {self.cfg_text(c)}"
                for i, c in sorted(self.state.probes.items())
                if self.cfg_text(c) != "no alerts set"]
        if cfgs:
            out += "\n\nAlerts:\n" + "\n".join(cfgs)
        return out

    # ---- BLE plumbing ----
    async def write_settings(self, payload: bytes) -> bool:
        c = self.client
        if c is None or not c.is_connected:
            return False
        try:
            await c.write_gatt_char(UUID_SETTINGS, payload, response=True)
            return True
        except Exception as e:
            log.warning("settings write failed: %s", e)
            return False

    def on_realtime(self, _sender, data: bytearray):
        temps: dict[int, Optional[float]] = {}
        for i in range(len(data) // 2):
            raw = data[2 * i] | (data[2 * i + 1] << 8)
            temps[i] = None if raw >= UNPLUGGED_RAW else raw / TEMP_DIVISOR
        self.state.latest = temps
        now = time.time()
        if now - self._last_sample >= HISTORY_SAMPLE_SECONDS:
            self._last_sample = now
            self.history.append((now, dict(temps)))
        asyncio.create_task(self.check_alerts(temps))

    def on_settings(self, _sender, data: bytearray):
        # Battery reply: header 0x24, then current/max voltage (uint16 LE each)
        if data and data[0] == 0x24 and len(data) >= 5:
            cur = data[1] | (data[2] << 8)
            mx = (data[3] | (data[4] << 8)) or 6550
            pct = max(0, min(100, round(cur / mx * 100)))
            asyncio.create_task(self.tg.send(f"Battery ~{pct}% ({cur}/{mx} mV)"))

    async def check_alerts(self, temps: dict[int, Optional[float]]):
        for idx, temp in temps.items():
            if temp is None:
                continue
            cfg = self.state.probes.get(idx)
            if not cfg:
                continue
            n = idx + 1
            if cfg.target is not None:
                if cfg.target_armed and temp >= cfg.target:
                    await self.tg.send(
                        f"Probe {n} hit target {fmt(cfg.target)} (now {fmt(temp)})")
                    cfg.target_armed = False
                elif not cfg.target_armed and temp < cfg.target - HYSTERESIS_C:
                    cfg.target_armed = True
            if cfg.high is not None:
                if cfg.high_armed and temp > cfg.high:
                    await self.tg.send(
                        f"Probe {n} ABOVE high {fmt(cfg.high)} (now {fmt(temp)})")
                    cfg.high_armed = False
                elif not cfg.high_armed and temp <= cfg.high - HYSTERESIS_C:
                    cfg.high_armed = True
            if cfg.low is not None:
                if cfg.low_armed and temp < cfg.low:
                    await self.tg.send(
                        f"Probe {n} BELOW low {fmt(cfg.low)} (now {fmt(temp)})")
                    cfg.low_armed = False
                elif not cfg.low_armed and temp >= cfg.low + HYSTERESIS_C:
                    cfg.low_armed = True

    async def find_device(self):
        try:
            if DEVICE_MAC:
                return await BleakScanner.find_device_by_address(
                    DEVICE_MAC, timeout=SCAN_TIMEOUT)
            return await BleakScanner.find_device_by_name(
                DEVICE_NAME, timeout=SCAN_TIMEOUT)
        except Exception as e:
            log.warning("scan error: %s", e)
            return None

    async def ble_loop(self, stop: asyncio.Event):
        while not stop.is_set():
            device = await self.find_device()
            if device is None:
                await asyncio.sleep(RESCAN_SECONDS)
                continue
            log.info("found %s (%s)", device.name, device.address)
            try:
                async with BleakClient(device) as client:
                    self.client = client
                    # Handshake: credentials -> subscribe -> enable realtime
                    await client.write_gatt_char(
                        UUID_CREDENTIALS, MSG_CREDENTIALS, response=True)
                    await client.start_notify(UUID_RESULT, self.on_settings)
                    await client.start_notify(UUID_REALTIME, self.on_realtime)
                    await client.write_gatt_char(
                        UUID_SETTINGS, MSG_REALTIME_ENABLE, response=True)

                    # fresh cook -> clear history + re-arm every alert
                    self.history.clear()
                    self._last_sample = 0.0
                    for cfg in self.state.probes.values():
                        cfg.target_armed = cfg.low_armed = cfg.high_armed = True

                    await self.tg.send("Thermometer connected - monitoring started.")
                    while client.is_connected and not stop.is_set():
                        await asyncio.sleep(1)
                await self.tg.send("Thermometer disconnected.")
            except Exception as e:
                log.warning("connection ended: %s", e)
            finally:
                self.client = None
            if not stop.is_set():
                await asyncio.sleep(RECONNECT_SECONDS)


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    async with aiohttp.ClientSession() as session:
        app = App(session)
        await app.tg.send("IBT-4XS bridge started. Send /help for commands.")
        tasks = [
            asyncio.create_task(app.ble_loop(stop)),
            asyncio.create_task(app.tg.poll(app.handle_command)),
        ]
        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())