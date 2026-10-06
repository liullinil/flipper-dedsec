"""Wait for the Flipper to disappear (reboot) and come back, then print firmware info."""
import sys, time
import serial.tools.list_ports
from flipper_lib import Flipper

def flipper_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == 0x0483 and p.pid == 0x5740:
            return p.device
    return None

limit = float(sys.argv[1]) if len(sys.argv) > 1 else 480
t0 = time.time()
while flipper_port() and time.time() - t0 < 90:
    time.sleep(1)
print("port gone" if not flipper_port() else "port never disappeared (update may not have started)", f"after {time.time()-t0:.0f}s")
gone_at = time.time()
while time.time() - gone_at < limit:
    port = flipper_port()
    if port:
        time.sleep(4)
        try:
            with Flipper(port) as f:
                info = f.cmd("device_info", timeout=15)
            if "firmware_version" in info:
                print(f"back on {port} after {time.time()-gone_at:.0f}s")
                for line in info.splitlines():
                    if any(k in line for k in ("firmware_version", "firmware_api", "firmware_build_date", "firmware_origin_fork", "radio_stack_m")):
                        print(line.strip())
                sys.exit(0)
        except Exception as e:
            print("not ready yet:", e)
    time.sleep(3)
sys.exit("device did not come back in time")
