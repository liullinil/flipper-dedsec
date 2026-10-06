"""CPU / RAM / disk / network load via psutil (rates in KB/s)."""
import time

import psutil


class SysMon:
    def __init__(self):
        psutil.cpu_percent(None)
        self.t = time.time()
        self.disk = psutil.disk_io_counters()
        self.net = psutil.net_io_counters()

    def sample(self):
        now = time.time()
        dt = max(0.2, now - self.t)
        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()
        vm = psutil.virtual_memory()
        rd = (disk.read_bytes - self.disk.read_bytes) / dt / 1024
        wr = (disk.write_bytes - self.disk.write_bytes) / dt / 1024
        # time the disks spent on I/O in this interval; can exceed 100 % with parallel queues
        io_ms = (disk.read_time - self.disk.read_time) + (disk.write_time - self.disk.write_time)
        busy = min(100, int(io_ms / (dt * 1000) * 100))
        up = (net.bytes_sent - self.net.bytes_sent) / dt / 1024
        dn = (net.bytes_recv - self.net.bytes_recv) / dt / 1024
        self.t, self.disk, self.net = now, disk, net
        return {
            "cpu": int(round(psutil.cpu_percent(None))),
            "ram": int(round(vm.percent)),
            "dsk": max(0, busy),
            "rd": max(0, int(rd)),
            "wr": max(0, int(wr)),
            "up": max(0, int(up)),
            "dn": max(0, int(dn)),
            "used_mb": int((vm.total - vm.available) / 2**20),
            "total_mb": int(vm.total / 2**20),
        }
