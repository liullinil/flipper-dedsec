"""Full session reports and acknowledgement-aware views.

The BLE list keeps short summaries for the small display.  This module keeps the
full, local-only report in memory and exposes wrapped pages for a richer client.
Only opaque acknowledgement keys are persisted; report text and names never
leave the process through the acknowledgement file.
"""
import json
import os
import tempfile

from .common import APPROVAL, ERROR, Session, WORKING, YOUR_TURN


_ACTIVE = frozenset((WORKING, APPROVAL, ERROR))


def _wrap(text, width):
    width = max(1, int(width))
    lines = []
    paragraphs = str(text or "").splitlines()
    if not paragraphs:
        paragraphs = [""]
    for paragraph in paragraphs:
        if not paragraph:
            lines.append("")
            continue
        rest = paragraph
        while len(rest) > width:
            cut = rest.rfind(" ", 0, width + 1)
            if cut <= 0:
                cut = width
            lines.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        lines.append(rest)
    return lines


class SessionViews:
    """Track current sessions, completed-report acknowledgements and pages."""

    def __init__(self, persistence_path=None):
        self.persistence_path = os.fspath(persistence_path) if persistence_path else None
        self._latest = {}       # kind -> key -> Session
        self._acks = set()      # (kind, key, opaque revision token)
        self._load()

    @staticmethod
    def _kind(kind):
        return str(kind)

    @staticmethod
    def _token(kind, key, revision):
        # Persist only an opaque acknowledgement token.  The actual report,
        # name and revision string remain in memory and are never serialized.
        import hashlib
        value = "\0".join((str(kind), str(key), str(revision)))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _load(self):
        if not self.persistence_path:
            return
        try:
            with open(self.persistence_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, TypeError):
            return
        entries = data.get("acks", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return
        for item in entries:
            if not isinstance(item, dict):
                continue
            kind, key = item.get("kind"), item.get("key")
            token = item.get("revision", item.get("token"))
            if isinstance(kind, str) and isinstance(key, str) and isinstance(token, str):
                self._acks.add((kind, key, token))

    def _save(self):
        if not self.persistence_path:
            return
        parent = os.path.dirname(os.path.abspath(self.persistence_path)) or "."
        try:
            os.makedirs(parent, exist_ok=True)
            payload = {
                "acks": [
                    {"kind": kind, "key": key, "revision": token}
                    for kind, key, token in sorted(self._acks)
                ]
            }
            fd, tmp = tempfile.mkstemp(prefix=".session-acks-", suffix=".tmp", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                    fh.write("\n")
                os.replace(tmp, self.persistence_path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError:
            # Acknowledgement persistence is best effort; the live view remains usable.
            return

    @staticmethod
    def _is_visible(session):
        if session.state in _ACTIVE:
            return True
        return session.state == YOUR_TURN

    def _is_acknowledged(self, kind, session):
        if session.state != YOUR_TURN or not session.revision:
            return False
        token = self._token(kind, session.key, session.revision)
        return (kind, session.key, token) in self._acks

    def update(self, kind, rows):
        """Store the latest rows and return active/unacknowledged visible rows."""
        kind = self._kind(kind)
        rows = list(rows or [])
        self._latest[kind] = {row.key: row for row in rows}
        return [
            row for row in rows
            if self._is_visible(row) and not self._is_acknowledged(kind, row)
        ]

    def acknowledge(self, kind, key, revision):
        """Acknowledge exactly the currently displayed completed revision."""
        kind, key, revision = self._kind(kind), str(key), str(revision)
        row = self._latest.get(kind, {}).get(key)
        if row is None or row.state != YOUR_TURN or row.revision != revision:
            return False
        # Replace older acknowledgements for this row, preventing unbounded growth.
        self._acks = {entry for entry in self._acks if entry[:2] != (kind, key)}
        self._acks.add((kind, key, self._token(kind, key, revision)))
        self._save()
        return True

    def page(self, kind, key, offset=0, width=21, count=4):
        """Return a wrapped page of the full name and report for one latest row."""
        kind, key = self._kind(kind), str(key)
        row = self._latest.get(kind, {}).get(key)
        if row is None:
            return None
        name = row.full_name or row.name
        body = row.body or row.detail
        lines = _wrap(name, width)
        if body:
            lines.extend(_wrap(body, width))
        total = len(lines)
        offset = max(0, min(int(offset), total))
        count = max(0, int(count))
        return {
            "key": row.key,
            "name": name,
            "state": row.state,
            "revision": row.revision,
            "offset": offset,
            "total": total,
            "lines": lines[offset:offset + count],
        }
