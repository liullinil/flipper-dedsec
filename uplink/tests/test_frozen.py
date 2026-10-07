"""Processes the one-file exe starts must not inherit the bootloader's private state."""
import os

import pytest

from uplink import frozen

BUNDLE = r"C:\Temp\_MEI1234"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows paths")


def leaked_env():
    return {
        "_PYI_APPLICATION_HOME_DIR": BUNDLE,
        "_PYI_ARCHIVE_FILE": r"D:\downloads\DedSecUplink.exe",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "_MEIPASS2": BUNDLE,
        "TCL_LIBRARY": BUNDLE + r"\_tcl_data",
        "TK_LIBRARY": BUNDLE + r"\_tk_data",
        "PATH": BUNDLE + r";C:\Windows\system32;C:\Temp\_MEI12345\bin",
        "USERPROFILE": r"C:\Users\someone",
    }


def test_child_env_drops_the_bootloader_state():
    env = frozen.child_env(leaked_env(), bundle=BUNDLE)
    assert not [key for key in env if key.upper().startswith(("_PYI_", "_MEIPASS"))]
    assert "TCL_LIBRARY" not in env and "TK_LIBRARY" not in env
    # only entries inside the bundle go, not a sibling folder sharing its name as a prefix
    assert env["PATH"] == r"C:\Windows\system32;C:\Temp\_MEI12345\bin"
    assert env["USERPROFILE"] == r"C:\Users\someone"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in env


def test_child_env_keeps_the_users_own_tcl():
    env = frozen.child_env({"TCL_LIBRARY": r"C:\Python39\tcl\tcl8.6", "PATH": r"C:\bin"}, bundle=BUNDLE)
    assert env == {"TCL_LIBRARY": r"C:\Python39\tcl\tcl8.6", "PATH": r"C:\bin"}


def test_restart_env_asks_for_a_fresh_instance():
    env = frozen.child_env(leaked_env(), restart=True, bundle=BUNDLE)
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "_PYI_APPLICATION_HOME_DIR" not in env


def test_running_from_source_leaves_paths_alone():
    env = frozen.child_env({"_PYI_ARCHIVE_FILE": "x", "PATH": r"C:\a;C:\b"}, bundle="")
    assert env == {"PATH": r"C:\a;C:\b"}


def test_update_script_waits_swaps_restarts_and_checks():
    script = frozen.update_script(r"C:\Temp\new.exe", r"D:\o'neil\DedSecUplink.exe", (11, 22),
                                  r"C:\Users\x\AppData\Local\DedSecUplink\update.log")
    assert "foreach ($id in @(11, 22))" in script
    assert "$p.ProcessName -eq 'DedSecUplink'" in script
    assert "Move-Item -LiteralPath 'C:\\Temp\\new.exe' -Destination 'D:\\o''neil\\DedSecUplink.exe'" in script
    assert ("Start-Process -FilePath 'D:\\o''neil\\DedSecUplink.exe' -WorkingDirectory 'D:\\o''neil'"
            " -PassThru") in script
    assert "if ($new.HasExited)" in script and "'explorer.exe'" in script
    assert script.splitlines()[-1] == "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force"


def test_apply_update_starts_the_helper_with_a_clean_environment(monkeypatch, tmp_path):
    calls, quits = [], []
    monkeypatch.setattr(frozen.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(frozen.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(frozen.config, "APP_DIR", str(tmp_path))
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", BUNDLE)
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")

    frozen.apply_companion_update(str(tmp_path / "new.exe"), lambda: quits.append(1),
                                  target=r"D:\downloads\DedSecUplink.exe")

    assert quits == [1] and len(calls) == 1
    (argv,), kwargs = calls[0]
    script = argv[argv.index("-File") + 1]
    assert script.startswith(str(tmp_path))
    with open(script, "rb") as fh:
        assert fh.read(3) == b"\xef\xbb\xbf"           # Windows PowerShell 5.1 needs the BOM
    env = kwargs["env"]
    assert "_PYI_APPLICATION_HOME_DIR" not in env and "_PYI_PARENT_PROCESS_LEVEL" not in env
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert kwargs["creationflags"] == frozen.CREATE_NO_WINDOW
