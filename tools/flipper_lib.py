"""Flipper Zero CLI helper over USB serial: list/walk/remove/receive/send/md5."""
import time
import serial

PROMPT = b">: "
EOL = b"\r\n"
ENC = "latin-1"  # byte-preserving; the CLI only accepts ASCII in paths anyway


def ascii_ok(name):
    return all(ord(c) < 127 for c in name)


class Flipper:
    def __init__(self, port, baud=230400):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self._rx = b""
        self.ser.reset_input_buffer()
        self.ser.write(b"\r")
        try:
            self._read_until(PROMPT, 3.0)
        except TimeoutError:
            pass

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _read_until(self, marker, timeout=10.0):
        deadline = time.time() + timeout
        while True:
            idx = self._rx.find(marker)
            if idx != -1:
                end = idx + len(marker)
                out, self._rx = self._rx[:end], self._rx[end:]
                return out
            if time.time() > deadline:
                raise TimeoutError(f"timeout waiting for {marker!r}; tail={self._rx[-200:]!r}")
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                self._rx += chunk
                deadline = time.time() + timeout

    def _read_exact(self, n, timeout=10.0):
        deadline = time.time() + timeout
        while len(self._rx) < n:
            if time.time() > deadline:
                raise TimeoutError(f"short read: {len(self._rx)}/{n}")
            chunk = self.ser.read(max(1, n - len(self._rx)))
            if chunk:
                self._rx += chunk
                deadline = time.time() + timeout
        out, self._rx = self._rx[:n], self._rx[n:]
        return out

    def _write(self, text):
        self.ser.write(text.encode(ENC))

    def cmd(self, command, timeout=10.0):
        self._rx = b""
        self.ser.reset_input_buffer()
        self._write(command + "\r")
        out = self._read_until(PROMPT, timeout)
        text = out.decode(ENC)[: -len(PROMPT)]
        lines = text.split("\n")
        if lines and command in lines[0]:
            lines = lines[1:]
        return "\n".join(lines).strip("\r\n")

    @staticmethod
    def q(path):
        return f'"{path}"'

    def list(self, path):
        out = self.cmd(f"storage list {self.q(path)}")
        entries = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line == "Empty":
                continue
            if line.startswith("[D] "):
                entries.append(("D", line[4:], 0))
            elif line.startswith("[F] "):
                name, _, size = line[4:].rpartition(" ")
                entries.append(("F", name, int(size.rstrip("b"))))
            elif line.lower().startswith("storage error"):
                raise RuntimeError(f"{path}: {line}")
        return entries

    def stat(self, path):
        out = self.cmd(f"storage stat {self.q(path)}")
        if out.lower().startswith("storage error"):
            return None
        if out.startswith("Directory") or out.startswith("Storage"):
            return "D"
        if out.startswith("File"):
            return "F"
        return None

    def walk(self, path):
        entries = self.list(path)
        dirs = [e for e in entries if e[0] == "D"]
        files = [e for e in entries if e[0] == "F"]
        yield path, dirs, files
        for d in dirs:
            if not ascii_ok(d[1]):
                print("skip (non-ascii name):", path + "/" + repr(d[1]))
                continue
            yield from self.walk(f"{path}/{d[1]}")

    def remove(self, path):
        out = self.cmd(f"storage remove {self.q(path)}")
        if "error" in out.lower():
            raise RuntimeError(f"remove {path}: {out}")
        return out

    def rmtree(self, path, log=None, errors=None):
        for t, name, _ in self.list(path):
            p = f"{path}/{name}"
            try:
                if not ascii_ok(name):
                    raise RuntimeError(f"{p!r}: non-ascii name, not addressable over CLI")
                if t == "D":
                    self.rmtree(p, log, errors)
                else:
                    self.remove(p)
                    if log:
                        log("rm", p)
            except RuntimeError as e:
                if errors is None:
                    raise
                errors.append(str(e))
        try:
            self.remove(path)
            if log:
                log("rmdir", path)
        except RuntimeError as e:
            if errors is None:
                raise
            errors.append(str(e))

    def mkdir(self, path):
        out = self.cmd(f"storage mkdir {self.q(path)}")
        if "error" in out.lower() and "exist" not in out.lower():
            raise RuntimeError(f"mkdir {path}: {out}")

    def md5(self, path):
        out = self.cmd(f"storage md5 {self.q(path)}", timeout=60)
        if "error" in out.lower():
            raise RuntimeError(f"md5 {path}: {out}")
        return out.strip().lower()

    def _fail(self, path, line):
        try:
            self._read_until(PROMPT, 3)
        except TimeoutError:
            pass
        raise RuntimeError(f"{path}: {line.decode(ENC).strip()}")

    def receive_file(self, path, local_path, chunk=8192):
        self._rx = b""
        self.ser.reset_input_buffer()
        self._write(f"storage read_chunks {self.q(path)} {chunk}\r")
        self._read_until(EOL)  # echo
        line = self._read_until(EOL)
        if b"Size:" not in line:
            self._fail(path, line)
        size = int(line.split(b":")[1])
        data = bytearray()
        while len(data) < size:
            self._read_until(b"Ready?" + EOL)
            self.ser.write(b"y")
            data += self._read_exact(min(chunk, size - len(data)))
        self._read_until(PROMPT)
        with open(local_path, "wb") as fh:
            fh.write(data)
        return size

    def send_file(self, local_path, path, chunk=8192):
        try:
            self.remove(path)
        except RuntimeError:
            pass
        with open(local_path, "rb") as fh:
            data = fh.read()
        sent = 0
        while sent < len(data):
            part = data[sent : sent + chunk]
            self._rx = b""
            self._write(f"storage write_chunk {self.q(path)} {len(part)}\r")
            self._read_until(EOL)  # echo
            line = self._read_until(EOL)
            if b"Ready" not in line:
                self._fail(path, line)
            self.ser.write(part)
            self._read_until(PROMPT, 30)
            sent += len(part)
        return sent
