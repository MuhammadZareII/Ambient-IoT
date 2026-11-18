#!/usr/bin/env python3
"""
Ambient IoT monitor - log, visualize and document the sensor node's results.

Data sources (all produce the same CSV: time, temperature_c, humidity_pct, soil_raw, source):
  ble      Connect to the Xiao running unoptimized_BLE_full_connection ("IoT_Node", Nordic UART).
  beacon   Passively scan for the Xiao running optimized_BLE_burst_beaconing ("EcoNode",
           manufacturer data 0xFFFF: int16 temp*100, uint16 hum*100, uint16 soil).
  serial   Read the Nano R4 USB debug output ("[TX] 23.4,45.1,1873" lines).
  simulate Synthetic data, for trying the tool without hardware (marked as simulated in the CSV).

Commands:
  python iot_monitor.py log    --source ble    [--out data/run.csv] [--live] [--duration 600]
  python iot_monitor.py log    --source serial --port COM8 --live
  python iot_monitor.py report data/run.csv [--ppk ppk_export.csv] [--title "Lab run 1"] [--out report.html]

The report is a single self-contained HTML file (charts embedded) with summary statistics,
link-quality figures (packet interval, gaps), an optional PPK2 power-profile analysis and
the raw data table. Open it in any browser, print it to PDF for your lab report.

Requires: pip install bleak pyserial matplotlib pandas
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import html
import io
import math
import random
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Nordic UART: notifications from the Xiao
FULL_PAYLOAD = re.compile(r"-?\d+\.\d,\d+\.\d,\d+")  # node sends one decimal for T and RH
CSV_FIELDS = ["time", "temperature_c", "humidity_pct", "soil_raw", "source"]

# Reference categorical slots 1-3 (validated palette) + recessive ink for axes/grid.
C_TEMP, C_HUM, C_SOIL = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"


@dataclass
class Reading:
    time: datetime
    temperature_c: float | None
    humidity_pct: float | None
    soil_raw: int | None
    source: str


def parse_payload(text: str, source: str) -> Reading | None:
    """Parse 't,h,s' (the framed payload the node sends)."""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(\d+)", text)
    if not m:
        return None
    t, h, s = float(m.group(1)), float(m.group(2)), int(m.group(3))
    return Reading(datetime.now(), None if t <= -999 else t, h, s, source)


# ----------------------------------------------------------------------------- sources
def run_ble(q: Queue, stop: threading.Event, name: str, address: str | None):
    from bleak import BleakClient, BleakScanner

    async def main():
        while not stop.is_set():
            dev = address or None
            if dev is None:
                print(f"[ble] scanning for '{name}' ...")
                found = await BleakScanner.find_device_by_name(name, timeout=15)
                if not found:
                    print("[ble] not found, retrying")
                    continue
                dev = found.address
            try:
                buf = bytearray()
                async with BleakClient(dev, timeout=20) as client:
                    print(f"[ble] connected to {dev}")

                    def on_data(_, data: bytearray):
                        # The Xiao writes the payload and the '\n' as separate notifications and the
                        # '\n' is sometimes dropped, so a notification holding a complete payload
                        # counts as a reading on its own; '\n' is only a separator.
                        buf.extend(data)
                        while b"\n" in buf:
                            line, _, rest = bytes(buf).partition(b"\n")
                            buf[:] = rest
                            r = parse_payload(line.decode(errors="replace"), "ble")
                            if r:
                                q.put(r)
                        pending = buf.decode(errors="replace").strip()
                        if FULL_PAYLOAD.fullmatch(pending):
                            buf.clear()
                            q.put(parse_payload(pending, "ble"))

                    await client.start_notify(NUS_TX_CHAR, on_data)
                    while client.is_connected and not stop.is_set():
                        await asyncio.sleep(0.5)
                print("[ble] disconnected")
            except Exception as e:  # noqa: BLE001 - keep logging through radio hiccups
                print(f"[ble] error: {e}; reconnecting")
                await asyncio.sleep(2)

    asyncio.run(main())


def run_beacon(q: Queue, stop: threading.Event, name: str, address: str | None):
    from bleak import BleakScanner

    async def main():
        last_payload = None

        def cb(dev, adv):
            nonlocal last_payload
            md = adv.manufacturer_data.get(0xFFFF)
            if not md or len(md) < 6:
                return
            if address and dev.address.upper() != address.upper():
                return
            if not address and (adv.local_name or dev.name or name) != name and adv.local_name:
                return
            if md[:6] == last_payload:  # same burst repeats every 100 ms - keep one sample
                return
            last_payload = bytes(md[:6])
            t, h, s = struct.unpack("<hHH", md[:6])
            q.put(Reading(datetime.now(), t / 100, h / 100, s, "beacon"))

        async with BleakScanner(cb):
            print(f"[beacon] scanning for manufacturer data 0xFFFF ('{name}')")
            while not stop.is_set():
                await asyncio.sleep(0.5)

    asyncio.run(main())


def run_serial(q: Queue, stop: threading.Event, port: str, baud: int):
    import serial

    with serial.Serial(port, baud, timeout=1) as ser:
        print(f"[serial] reading {port} @ {baud}")
        while not stop.is_set():
            line = ser.readline().decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("[TX]"):
                r = parse_payload(line, "serial")
                if r:
                    q.put(r)
            else:
                print(f"[node] {line}")


def run_simulate(q: Queue, stop: threading.Event, interval: float):
    t0 = time.time()
    while not stop.is_set():
        k = (time.time() - t0) / 60
        q.put(Reading(datetime.now(),
                      round(22.5 + 1.5 * math.sin(k / 3) + random.gauss(0, 0.1), 1),
                      round(41 + 6 * math.cos(k / 4) + random.gauss(0, 0.4), 1),
                      int(2100 - 40 * k + random.gauss(0, 15)), "simulated"))
        stop.wait(interval)


# ----------------------------------------------------------------------------- log
def cmd_log(a):
    out = Path(a.out or f"data/run_{datetime.now():%Y%m%d_%H%M%S}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    q: Queue = Queue()
    stop = threading.Event()
    target = {
        "ble": lambda: run_ble(q, stop, a.name or "IoT_Node", a.address),
        "beacon": lambda: run_beacon(q, stop, a.name or "EcoNode", a.address),
        "serial": lambda: run_serial(q, stop, a.port, a.baud),
        "simulate": lambda: run_simulate(q, stop, a.interval),
    }[a.source]
    threading.Thread(target=target, daemon=True).start()

    new = not out.exists()
    f = out.open("a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(CSV_FIELDS)
    print(f"[log] writing {out}  (Ctrl+C to stop)")

    rows: list[Reading] = []
    plot = LivePlot() if a.live else None
    t_end = time.time() + a.duration if a.duration else None
    try:
        while not (t_end and time.time() > t_end):
            try:
                r = q.get(timeout=0.2)
                rows.append(r)
                w.writerow([r.time.isoformat(timespec="milliseconds"), r.temperature_c,
                            r.humidity_pct, r.soil_raw, r.source])
                f.flush()
                print(f"{r.time:%H:%M:%S}  T={r.temperature_c} °C  RH={r.humidity_pct} %  soil={r.soil_raw}")
            except Empty:
                pass
            if plot and not plot.update(rows):
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        f.close()
        print(f"[log] {len(rows)} readings saved to {out}")
        if a.report and rows:
            cmd_report(argparse.Namespace(csv=str(out), ppk=None, out=None, title=None,
                                          soil_dry=None, soil_wet=None, voltage=3.3))


class LivePlot:
    def __init__(self):
        import matplotlib.pyplot as plt
        self.plt = plt
        plt.ion()
        self.fig, self.axes = make_figure(live=True)
        self.closed = False
        self.fig.canvas.mpl_connect("close_event", lambda _: setattr(self, "closed", True))
        self.n = -1

    def update(self, rows):
        if self.closed:
            return False
        if len(rows) != self.n and rows:
            self.n = len(rows)
            draw_series(self.axes, [r.time for r in rows],
                        [r.temperature_c for r in rows], [r.humidity_pct for r in rows],
                        [r.soil_raw for r in rows], soil_label="Soil moisture (raw ADC)")
            self.fig.canvas.draw_idle()
        self.plt.pause(0.05)
        return True


# ----------------------------------------------------------------------------- charts
def style_axis(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def make_figure(live=False, n=3):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(n, 1, figsize=(10, 2.4 * n), sharex=True, facecolor=SURFACE)
    fig.subplots_adjust(hspace=0.45, left=0.08, right=0.97, top=0.93 if live else 0.95, bottom=0.08)
    if live:
        fig.suptitle("Ambient IoT node - live", color=INK, fontsize=12, x=0.08, ha="left")
    return fig, list(axes)


def draw_series(axes, ts, temp, hum, soil, soil_label):
    import matplotlib.dates as mdates
    specs = [(temp, C_TEMP, "Temperature (°C)"), (hum, C_HUM, "Relative humidity (%)"),
             (soil, C_SOIL, soil_label)]
    for ax, (vals, color, title) in zip(axes, specs):
        ax.clear()
        style_axis(ax)
        pts = [(t, v) for t, v in zip(ts, vals) if v is not None and not _isnan(v)]
        ax.set_title(title, loc="left", color=INK, fontsize=10, fontweight="bold", pad=6)
        if not pts:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", color=INK_2)
            continue
        x, y = zip(*pts)
        ax.plot(x, y, color=color, linewidth=2, solid_capstyle="round")
        if len(pts) <= 120:
            ax.scatter(x, y, s=18, color=color, edgecolors=SURFACE, linewidths=1.2, zorder=3)
        # direct-label the latest value only (selective labelling)
        ax.annotate(f"{y[-1]:g}", (x[-1], y[-1]), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))


def _isnan(v):
    try:
        return math.isnan(v)
    except TypeError:
        return False


def fig_to_b64(fig) -> str:
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# ----------------------------------------------------------------------------- PPK2
def analyze_ppk(path: str, voltage: float):
    """PPK2 'Export -> CSV': columns Timestamp(ms), Current(uA) [, D0-D7]."""
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt

    df = pd.read_csv(path)
    tcol = next(c for c in df.columns if c.lower().startswith("timestamp"))
    icol = next(c for c in df.columns if c.lower().startswith("current"))
    t = df[tcol].to_numpy(dtype=float) / 1000.0  # s
    i_ua = df[icol].to_numpy(dtype=float)
    if "na" in icol.lower():
        i_ua = i_ua / 1000.0
    if "ma" in icol.lower():
        i_ua = i_ua * 1000.0
    t = t - t[0]
    dur = t[-1] - t[0]
    charge_c = np.trapezoid(i_ua, t) * 1e-6
    floor_ua = float(np.percentile(i_ua, 10))
    active = i_ua > max(floor_ua * 3, floor_ua + 500)  # > 3x floor and > +0.5 mA
    duty = float(active.mean() * 100)
    stats = {
        "Duration": f"{dur:.2f} s",
        "Average current": fmt_current(i_ua.mean()),
        "Peak current": fmt_current(i_ua.max()),
        "Sleep floor (P10)": fmt_current(floor_ua),
        "Active duty cycle": f"{duty:.1f} %",
        "Charge": f"{charge_c * 1000:.3f} mC",
        f"Energy @ {voltage:g} V": f"{charge_c * voltage * 1000:.3f} mJ",
        "Average power": f"{i_ua.mean() * 1e-6 * voltage * 1000:.3f} mW",
    }

    # Downsample for plotting (min/max envelope keeps spikes visible)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5.2), facecolor=SURFACE,
                                   gridspec_kw={"height_ratios": [2, 1]})
    fig.subplots_adjust(hspace=0.55, left=0.08, right=0.97, top=0.92, bottom=0.1)
    style_axis(ax1)
    n = len(t)
    step = max(1, n // 4000)
    if step > 1:
        m = n // step * step
        tt = t[:m].reshape(-1, step)[:, 0]
        hi = i_ua[:m].reshape(-1, step).max(1)
        lo = i_ua[:m].reshape(-1, step).min(1)
        ax1.fill_between(tt, lo / 1000, hi / 1000, color=C_TEMP, linewidth=0, alpha=0.35)
        ax1.plot(tt, hi / 1000, color=C_TEMP, linewidth=0.8)  # keeps flat stretches visible
        ax1.plot(tt, lo / 1000, color=C_TEMP, linewidth=0.8)
    else:
        ax1.plot(t, i_ua / 1000, color=C_TEMP, linewidth=1)
    ax1.axhline(i_ua.mean() / 1000, color=INK_2, linewidth=1, linestyle=(0, (4, 3)))
    ax1.annotate(f"avg {fmt_current(i_ua.mean())}", (t[-1], i_ua.mean() / 1000),
                 xytext=(0, 4), textcoords="offset points", ha="right", fontsize=9, color=INK)
    ax1.set_title("Supply current (mA)", loc="left", color=INK, fontsize=10, fontweight="bold")
    ax1.set_xlabel("time (s)", color=INK_2, fontsize=9)

    style_axis(ax2)
    pos = i_ua[i_ua > 0]
    bins = np.logspace(math.log10(max(pos.min(), 0.1)), math.log10(pos.max()), 60)
    ax2.hist(pos, bins=bins, color=C_TEMP, edgecolor=SURFACE, linewidth=0.5)
    ax2.set_xscale("log")
    ax2.set_title("Current distribution (µA, log scale)", loc="left", color=INK,
                  fontsize=10, fontweight="bold")
    ax2.set_yticks([])
    return stats, fig_to_b64(fig)


def cmd_ppk(a):
    """Drive a PPK2 directly (no nRF Connect needed) and save a Power-Profiler-style CSV."""
    import numpy as np
    from ppk2_api.ppk2_api import PPK2_API

    if not 800 <= a.mv <= 5000:
        sys.exit("--mv must be between 800 and 5000")
    out = Path(a.out or f"data/ppk_{datetime.now():%Y%m%d_%H%M%S}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    ppk = PPK2_API(a.port, timeout=1, write_timeout=1, exclusive=True)
    ppk.get_modifiers()
    if a.mode == "source":
        ppk.use_source_meter()
        ppk.set_source_voltage(a.mv)
        print(f"[ppk] source meter {a.mv} mV on {a.port}")
    else:
        ppk.use_ampere_meter()
        print(f"[ppk] ampere meter on {a.port} (external supply through VIN/VOUT)")

    native = 100_000                           # PPK2 sample rate
    step = max(1, native // a.rate)            # average blocks down to --rate
    pending = np.empty(0)
    n_out = 0
    t_end = time.time() + a.duration
    with out.open("w", newline="") as f:
        f.write("Timestamp(ms),Current(uA)\n")
        try:
            if a.mode == "source":
                ppk.toggle_DUT_power("ON")
            ppk.start_measuring()
            while time.time() < t_end:
                data = ppk.get_data()
                if data:
                    res = ppk.get_samples(data)
                    samples = res[0] if isinstance(res, tuple) else res
                    pending = np.concatenate([pending, np.asarray(samples, dtype=float)])
                    m = len(pending) // step * step
                    if m:
                        avg = pending[:m].reshape(-1, step).mean(1)
                        pending = pending[m:]
                        ts = (np.arange(n_out, n_out + len(avg)) * step / native) * 1000
                        np.savetxt(f, np.column_stack([ts, avg]), fmt="%.3f", delimiter=",")
                        n_out += len(avg)
                time.sleep(0.01)
                left = int(t_end - time.time())
                print(f"\r[ppk] recording... {left:4d} s left", end="", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            ppk.stop_measuring()
            if a.mode == "source" and not a.keep_power:
                ppk.toggle_DUT_power("OFF")
            ppk.ser.close()
    print(f"\n[ppk] {n_out} samples ({a.rate} Hz) saved to {out}")
    if n_out:
        stats, _ = analyze_ppk(str(out), a.mv / 1000 if a.mode == "source" else 3.3)
        for k, v in stats.items():
            print(f"  {k:<20} {v}")


def fmt_current(ua: float) -> str:
    return f"{ua / 1000:.2f} mA" if ua >= 1000 else f"{ua:.1f} µA"


# ----------------------------------------------------------------------------- report
def cmd_report(a):
    import pandas as pd

    df = pd.read_csv(a.csv, parse_dates=["time"])
    if df.empty:
        sys.exit("CSV has no rows")
    df = df.sort_values("time")
    soil_label = "Soil moisture (raw ADC)"
    soil_col = "soil_raw"
    if a.soil_dry is not None and a.soil_wet is not None:
        df["soil_pct"] = ((df.soil_raw - a.soil_dry) / (a.soil_wet - a.soil_dry) * 100).clip(0, 100).round(1)
        soil_col, soil_label = "soil_pct", "Soil moisture (% of calibrated range)"

    fig, axes = make_figure()
    draw_series(axes, list(df.time), list(df.temperature_c), list(df.humidity_pct),
                list(df[soil_col]), soil_label)
    series_png = fig_to_b64(fig)

    dt = df.time.diff().dt.total_seconds().dropna()
    med = float(dt.median()) if len(dt) else float("nan")
    gaps = int((dt > 2.5 * med).sum()) if len(dt) else 0
    span = (df.time.iloc[-1] - df.time.iloc[0]).total_seconds()
    expected = span / med + 1 if med and med > 0 else len(df)
    delivery = min(100.0, len(df) / expected * 100) if expected else 100.0

    def stat_row(label, s, unit):
        s = s.dropna()
        if s.empty:
            return f"<tr><td>{label}</td><td colspan=5>no data</td></tr>"
        return (f"<tr><td>{label}</td><td>{s.iloc[-1]:g} {unit}</td><td>{s.min():g}</td>"
                f"<td>{s.mean():.2f}</td><td>{s.max():g}</td><td>{s.std():.2f}</td></tr>")

    measures = "".join([
        stat_row("Temperature", df.temperature_c, "°C"),
        stat_row("Humidity", df.humidity_pct, "%"),
        stat_row("Soil moisture", df[soil_col], "%" if soil_col == "soil_pct" else ""),
    ])
    sources = ", ".join(sorted(df.source.astype(str).unique()))
    tiles = [
        ("Readings", f"{len(df)}"),
        ("Duration", fmt_duration(span)),
        ("Median interval", f"{med:.1f} s" if not math.isnan(med) else "-"),
        ("Delivery (est.)", f"{delivery:.0f} %"),
    ]

    ppk_html = ""
    if a.ppk:
        stats, ppk_png = analyze_ppk(a.ppk, a.voltage)
        rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in stats.items())
        ppk_html = f"""
<section><h2>Power profile</h2>
<p class=muted>PPK2 capture: {html.escape(Path(a.ppk).name)} - supply {a.voltage:g} V.
Active samples are those above 3&times; the sleep floor and above floor + 0.5 mA.</p>
<div class=split><table class=kv>{rows}</table>
<img alt="Supply current over time and its distribution" src="data:image/png;base64,{ppk_png}"></div>
</section>"""

    table_rows = "".join(
        f"<tr><td>{r.time:%Y-%m-%d %H:%M:%S}</td><td>{_cell(r.temperature_c)}</td>"
        f"<td>{_cell(r.humidity_pct)}</td><td>{_cell(r.soil_raw)}</td>"
        + (f"<td>{_cell(r.soil_pct)}</td>" if soil_col == "soil_pct" else "")
        + f"<td>{html.escape(str(r.source))}</td></tr>"
        for r in df.itertuples())
    title = a.title or f"Ambient IoT node - {df.time.iloc[0]:%Y-%m-%d %H:%M}"
    simulated = "simulated" in sources

    out = Path(a.out or Path(a.csv).with_suffix(".html"))
    out.write_text(HTML_TEMPLATE.format(
        title=html.escape(title),
        generated=f"{datetime.now():%Y-%m-%d %H:%M}",
        csv=html.escape(Path(a.csv).name),
        sources=html.escape(sources),
        banner='<p class=banner>Contains simulated data - not a hardware measurement.</p>' if simulated else "",
        tiles="".join(f"<div class=tile><div class=v>{v}</div><div class=k>{k}</div></div>" for k, v in tiles),
        measures=measures,
        series_png=series_png,
        gaps=gaps,
        first=f"{df.time.iloc[0]:%Y-%m-%d %H:%M:%S}",
        last=f"{df.time.iloc[-1]:%Y-%m-%d %H:%M:%S}",
        ppk=ppk_html,
        soil_head="<th>Soil %</th>" if soil_col == "soil_pct" else "",
        table_rows=table_rows,
    ), encoding="utf-8")
    print(f"[report] {out.resolve()}")


def _cell(v):
    return "" if v is None or _isnan(v) else f"{v:g}"


def fmt_duration(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


HTML_TEMPLATE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:#e6e5e0;--card:#ffffff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1040px;margin:0 auto;padding:32px 16px 64px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:18px;margin:36px 0 12px}}
.muted{{color:var(--ink2);font-size:13px}}
.banner{{background:#fff4d6;border:1px solid #eda100;padding:8px 12px;border-radius:6px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}}
.tile .v{{font-size:24px;font-weight:600}} .tile .k{{color:var(--ink2);font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--ink2);font-weight:500}}
img{{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:#fcfcfb}}
.split{{display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:16px;align-items:start}}
@media (max-width:720px){{.split{{grid-template-columns:1fr}}}}
details{{margin-top:12px}} .scroll{{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px}}
@media print{{.scroll{{max-height:none;overflow:visible}} details{{display:block}}}}
</style></head><body><main>
<h1>{title}</h1>
<p class=muted>Generated {generated} from <code>{csv}</code> &middot; source: {sources} &middot; {first} &rarr; {last}</p>
{banner}
<div class=tiles>{tiles}</div>

<section><h2>Measurements</h2>
<table><tr><th>Measure</th><th>Latest</th><th>Min</th><th>Mean</th><th>Max</th><th>Std dev</th></tr>{measures}</table>
<p class=muted>Gaps longer than 2.5&times; the median interval: {gaps}. Delivery is estimated from the median interval over the run.</p>
<img alt="Temperature, humidity and soil moisture over time" src="data:image/png;base64,{series_png}">
</section>
{ppk}
<section><h2>Raw data</h2>
<details><summary>Show all readings</summary><div class=scroll><table>
<tr><th>Time</th><th>Temp &deg;C</th><th>RH %</th><th>Soil raw</th>{soil_head}<th>Source</th></tr>{table_rows}
</table></div></details></section>
</main></body></html>"""


# ----------------------------------------------------------------------------- CLI
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    except AttributeError:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="record readings to CSV (optionally with a live plot)")
    lg.add_argument("--source", choices=["ble", "beacon", "serial", "simulate"], required=True)
    lg.add_argument("--out", help="CSV path (appends if it exists). Default: data/run_<timestamp>.csv")
    lg.add_argument("--live", action="store_true", help="show a live updating plot")
    lg.add_argument("--duration", type=float, help="stop after N seconds")
    lg.add_argument("--report", action="store_true", help="build the HTML report when logging stops")
    lg.add_argument("--name", help="BLE name (default IoT_Node for ble, EcoNode for beacon)")
    lg.add_argument("--address", help="BLE address, skips the name lookup")
    lg.add_argument("--port", default="COM8", help="serial port (serial source)")
    lg.add_argument("--baud", type=int, default=115200)
    lg.add_argument("--interval", type=float, default=5.0, help="simulate: seconds between readings")
    lg.set_defaults(func=cmd_log)

    pp = sub.add_parser("ppk", help="record a power profile with a PPK2 (no nRF Connect needed)")
    pp.add_argument("--port", default="COM11")
    pp.add_argument("--mode", choices=["source", "ampere"], default="source",
                    help="source: PPK2 powers the node; ampere: in series with an external supply")
    pp.add_argument("--mv", type=int, default=5000, help="source voltage in mV (800-5000)")
    pp.add_argument("--duration", type=float, default=60)
    pp.add_argument("--rate", type=int, default=10000, help="saved samples per second (averaged)")
    pp.add_argument("--keep-power", action="store_true", help="leave the node powered afterwards")
    pp.add_argument("--out")
    pp.set_defaults(func=cmd_ppk)

    rp = sub.add_parser("report", help="build a self-contained HTML report from a CSV")
    rp.add_argument("csv")
    rp.add_argument("--ppk", help="PPK2 CSV export to include as a power-profile section")
    rp.add_argument("--voltage", type=float, default=3.3, help="supply voltage for energy figures")
    rp.add_argument("--soil-dry", type=float, help="raw ADC value in dry air (enables % scale)")
    rp.add_argument("--soil-wet", type=float, help="raw ADC value in water (enables % scale)")
    rp.add_argument("--title")
    rp.add_argument("--out", help="HTML path (default: next to the CSV)")
    rp.set_defaults(func=cmd_report)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
