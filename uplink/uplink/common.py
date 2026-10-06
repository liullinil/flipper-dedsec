"""Shared bits: session model, JSONL tailing, timestamps, ASCII for the Flipper fonts."""
import json
import os
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime

# --------------------------------------------------------------------------- model
WORKING, APPROVAL, YOUR_TURN, IDLE, ERROR = "W", "A", "I", "S", "E"
ATTENTION = (APPROVAL, YOUR_TURN)
ORDER = {APPROVAL: 0, YOUR_TURN: 1, WORKING: 2, ERROR: 3, IDLE: 4}


@dataclass
class Session:
    key: str              # short stable id
    name: str
    state: str = IDLE
    detail: str = ""
    done: int = 0
    total: int = 0
    last_ts: float = 0.0  # last activity, epoch seconds
    attn: int = 0         # grows each time the session starts waiting for the user

    def age(self, now=None):
        return max(0, int((now or time.time()) - self.last_ts))


def short_key(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:6]


class AttentionCounter:
    """Turns state transitions into a monotonically growing 'needs you' counter per key.
    The first observation of a key only sets the baseline (no buzz on companion start)."""

    def __init__(self):
        self.prev = {}
        self.count = {}

    def update(self, key, state):
        prev = self.prev.get(key)
        if prev is not None and state in ATTENTION and prev != state:
            self.count[key] = self.count.get(key, 0) + 1
        self.prev[key] = state
        return self.count.get(key, 0)


# --------------------------------------------------------------------------- time
def parse_ts(value):
    """ISO-8601 'Z' timestamps from Codex/Claude logs -> epoch seconds."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- files
class JsonlTail:
    """Incrementally reads a growing JSONL file.

    The first read returns the first line (session header) plus only the last
    `initial_tail` bytes, so multi-megabyte transcripts load fast; later reads
    return just the newly appended complete lines."""

    def __init__(self, path, initial_tail=768 * 1024):
        self.path = path
        self.initial_tail = initial_tail
        self.pos = None
        self.buf = b""
        self.mtime = 0.0

    def changed(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return False
        return m != self.mtime

    def read(self):
        try:
            size = os.path.getsize(self.path)
            self.mtime = os.path.getmtime(self.path)
        except OSError:
            return []
        raw = []
        with open(self.path, "rb") as fh:
            if self.pos is None or size < self.pos:
                first = fh.readline()
                raw.append(first)
                start = max(fh.tell(), size - self.initial_tail)
                if start > fh.tell():
                    fh.seek(start)
                    fh.readline()           # drop the partial line we landed in
                self.pos = fh.tell()
                self.buf = b""
            fh.seek(self.pos)
            data = fh.read()
            self.pos += len(data)
        parts = (self.buf + data).split(b"\n")
        self.buf = parts.pop()
        raw.extend(parts)
        out = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


def recent_files(paths, window_s, now=None):
    now = now or time.time()
    out = []
    for p in paths:
        try:
            if now - os.path.getmtime(p) <= window_s:
                out.append(p)
        except OSError:
            pass
    return out


# --------------------------------------------------------------------------- text
_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "—": "-", "–": "-", "«": '"', "»": '"', "…": "...", "’": "'", "“": '"', "”": '"', "→": "->",
}


def ascii_text(text, limit):
    """Flipper fonts are ASCII only: transliterate Cyrillic, drop the rest, squeeze spaces."""
    out = []
    for ch in text or "":
        low = ch.lower()
        if low in _RU:
            t = _RU[low]
            out.append(t.capitalize() if ch != low and t else t)
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        elif ch in "\n\r\t":
            out.append(" ")
    s = " ".join("".join(out).replace("|", "/").split())
    return s[:limit]


_PUNCT = {
    "—": "-", "–": "-", "«": '"', "»": '"', "…": "...", "’": "'", "‘": "'",
    "“": '"', "”": '"', "→": "->", "←": "<-", "•": "*", " ": " ",
}


def utf8_text(text, limit):
    """Text for the Flipper's UTF-8 font (Latin + Cyrillic): keep ASCII and Cyrillic, map common
    typography to ASCII, drop anything the font can't draw, squeeze spaces, cap by characters."""
    out = []
    for ch in text or "":
        if ch in _PUNCT:
            out.append(_PUNCT[ch])
        elif ch in "\n\r\t":
            out.append(" ")
        elif ch == "|":
            out.append("/")          # protocol separator
        elif 32 <= ord(ch) < 127 or "Ѐ" <= ch <= "ӿ":
            out.append(ch)
    return " ".join("".join(out).split())[:limit]


def strip_md(text):
    return (text or "").replace("**", "").replace("`", "").strip()
