"""The companion keeps the Claude Code hooks on and prefers the fast native hook."""
import json
import os

from uplink import hooks


def setup(tmp_path, monkeypatch, bundled=True):
    app_dir = tmp_path / "app"
    native = app_dir / "uplink_hook.exe"
    settings = tmp_path / ".claude" / "settings.json"
    source = tmp_path / "bundle" / "uplink_hook.exe"
    if bundled:
        source.parent.mkdir()
        source.write_bytes(b"MZ fake hook")
    monkeypatch.setattr(hooks, "APP_DIR", str(app_dir))
    monkeypatch.setattr(hooks, "NATIVE_HOOK", str(native))
    monkeypatch.setattr(hooks, "SETTINGS", str(settings))
    monkeypatch.setattr(hooks, "_bundled_hook", lambda: str(source))
    return native, settings


def commands(settings):
    data = json.loads(settings.read_text(encoding="utf-8"))
    return {event: [h["command"] for g in groups for h in g["hooks"]]
            for event, groups in data["hooks"].items()}


def test_installs_the_native_hook_for_every_event(tmp_path, monkeypatch):
    native, settings = setup(tmp_path, monkeypatch)
    assert hooks.ensure_installed() is True
    assert native.read_bytes() == b"MZ fake hook"
    expected = '"%s"' % native
    assert commands(settings) == {event: [expected] for event in hooks.HOOK_EVENTS}
    assert hooks.ensure_installed() is False          # nothing to do the second time


def test_keeps_the_users_own_hooks_and_replaces_a_stale_command(tmp_path, monkeypatch):
    native, settings = setup(tmp_path, monkeypatch)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "my-own-hook"}]},
        {"hooks": [{"type": "command", "command": '"D:\\old\\DedSecUplink.exe" --hook'}]},
    ]}}), encoding="utf-8")
    assert hooks.ensure_installed() is True
    stop = commands(settings)["Stop"]
    assert "my-own-hook" in stop and '"%s"' % native in stop
    assert not any("old" in c for c in stop)


def test_falls_back_without_the_native_hook(tmp_path, monkeypatch):
    native, settings = setup(tmp_path, monkeypatch, bundled=False)
    hooks.ensure_installed()
    assert not native.exists()
    assert all("uplink_hook" in c[0] or "DedSecUplink" in c[0] for c in commands(settings).values())
