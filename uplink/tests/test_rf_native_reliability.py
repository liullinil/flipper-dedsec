"""Compile the production Flipper persistence code with a faultable storage API.

These tests exercise C code, including recovery after a reset between synced
write and rename, rather than relying on source-text checks or a Python copy.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _compiler_env():
    """Use an available host C compiler, including a normal Visual Studio setup."""
    for name in ("cc", "gcc", "clang"):
        if shutil.which(name):
            return name, os.environ.copy(), False
    if shutil.which("cl"):
        return "cl", os.environ.copy(), True
    if os.name == "nt":
        roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                 Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
        for root in roots:
            for script in (root / "Microsoft Visual Studio").glob("*/*/VC/Auxiliary/Build/vcvars64.bat"):
                # This only initializes compiler environment variables.  File
                # changes remain in pytest's temporary build directory.
                command = f'call "{script}" >nul && set'
                result = subprocess.run(f'cmd /d /s /c "{command}"',
                                        capture_output=True, text=True, check=True)
                env = os.environ.copy()
                for line in result.stdout.splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env[key] = value
                compiler_path = shutil.which("cl", path=env.get("Path", env.get("PATH")))
                if compiler_path:
                    return compiler_path, env, True
    pytest.skip("host C compiler unavailable")


def test_native_rf_storage_and_settings_recover_after_failed_commit(tmp_path):
    compiler, env, msvc = _compiler_env()
    root = Path(__file__).resolve().parents[2]
    fixtures = Path(__file__).with_name("native_rf")
    source = root / "apps" / "rf_signal_hunter"
    executable = tmp_path / ("reliability.exe" if os.name == "nt" else "reliability")
    files = [str(fixtures / "reliability_harness.c"), str(source / "rf_settings.c"),
             str(source / "rf_store.c")]
    if msvc:
        command = [compiler, "/nologo", "/std:c11", "/W3", "/D_CRT_SECURE_NO_WARNINGS",
                   f"/I{fixtures}", f"/I{source}", *files, f"/Fe:{executable}"]
    else:
        command = [compiler, "-std=c11", "-Wall", "-Wextra", "-I", str(fixtures),
                   "-I", str(source), *files, "-o", str(executable)]
    built = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "RF persistence fault tests passed" in ran.stdout
