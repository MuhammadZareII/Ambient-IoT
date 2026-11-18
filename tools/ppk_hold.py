"""Hold PPK2 VOUT on (source meter) and print the average current once per second. Ctrl+C to stop."""
import sys, time, numpy as np
from ppk2_api.ppk2_api import PPK2_API
port = sys.argv[1] if len(sys.argv) > 1 else "COM11"
mv = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
p = PPK2_API(port, timeout=1, write_timeout=1, exclusive=True)
p.get_modifiers()
p.use_source_meter()
p.set_source_voltage(mv)
p.toggle_DUT_power("ON")
p.start_measuring()
print(f"VOUT ON at {mv} mV", flush=True)
buf, t = [], time.time()
try:
    while True:
        d = p.get_data()
        if d:
            r = p.get_samples(d); buf.extend(r[0] if isinstance(r, tuple) else r)
        if time.time() - t >= 1 and buf:
            a = np.asarray(buf); print(f"avg {a.mean()/1000:8.3f} mA   peak {a.max()/1000:8.3f} mA", flush=True)
            buf, t = [], time.time()
        time.sleep(0.01)
finally:
    p.stop_measuring(); p.toggle_DUT_power("OFF"); p.ser.close(); print("VOUT OFF")
