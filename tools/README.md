# iot_monitor.py

Log, plot and document the Ambient IoT node's readings.

```
pip install bleak pyserial matplotlib pandas
```

## Record data

| Firmware on the Xiao | Command |
|---|---|
| `unoptimized_BLE_full_connection` (name `IoT_Node`) | `python iot_monitor.py log --source ble --live` |
| `optimized_BLE_burst_beaconing` (name `EcoNode`) | `python iot_monitor.py log --source beacon --live` |
| No BLE, just the Nano's USB debug output | `python iot_monitor.py log --source serial --port COM8 --live` |
| No hardware (tool test) | `python iot_monitor.py log --source simulate --live` |

- Readings go to `data/run_<timestamp>.csv`. Use `--out` to choose the file; an existing file is appended to.
- Stop with Ctrl+C or by closing the plot window. `--duration 600` stops after 10 minutes.
- `--report` builds the HTML report as soon as logging stops.
- In `serial` mode the Nano's other debug lines are shown too, which helps when the SHT40 isn't answering.
- Only one program can hold a serial port at a time: close the Arduino IDE Serial Monitor first.
- The phone app and this tool can't both be connected in `ble` mode. `beacon` mode has no connection, so it doesn't have this limit.

## Build a report

```
python iot_monitor.py report data/run_20261113_140000.csv --title "Window sill, day 1"
python iot_monitor.py report data/run.csv --ppk ppk_capture.csv --voltage 3.3
python iot_monitor.py report data/run.csv --soil-dry 3050 --soil-wet 1350
```

You get one self-contained HTML file that you can open in a browser or print to PDF for the lab report. It contains:
- Summary tiles: number of readings, duration, median interval, estimated delivery rate.
- Statistics for each measure, and temperature / humidity / soil moisture charts on a shared time axis.
- Optional **power profile** from a PPK2 export (*Power Profiler, Export, CSV*): average and peak current, sleep floor, active duty cycle, charge, energy and average power. This lets you show the before/after effect of the optimizations in manual section XII.
- The full raw data table.

Soil calibration: note the raw value with the probe in dry air (`--soil-dry`) and in a glass of water (`--soil-wet`). The report then shows soil moisture as a percentage of that range.
