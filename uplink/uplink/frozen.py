"""Living as a PyInstaller one-file exe: clean environments for child processes, self-update swap.

The one-file bootloader passes _PYI_APPLICATION_HOME_DIR, _PYI_ARCHIVE_FILE and
_PYI_PARENT_PROCESS_LEVEL to its Python process, so that sys.executable started again (for
multiprocessing) reuses the unpacked _MEI folder instead of unpacking a second copy. Every other
process we start inherits them too. A new copy of the companion started through such a chain then
tries to run from the old _MEI folder, which the old bootloader deleted on exit, and never comes
up: that is how the 1.2.1 -> 1.2.2 self-update swapped the exe but did not restart it. The same
variables (and TCL_LIBRARY, TK_LIBRARY and a PATH entry pointing into _MEI) leaked into the shell
of the CMD tab.
"""
import logging
import os
import subprocess
import sys
import tempfile

from . import config

log = logging.getLogger("uplink.frozen")

PRIVATE_PREFIXES = ("_PYI_", "_MEIPASS")      # _MEIPASS2 is what bootloaders before 6.9 used
BUNDLE_PATH_VARS = ("TCL_LIBRARY", "TK_LIBRARY")
CREATE_NO_WINDOW = 0x08000000


def _inside(path, folder):
    try:
        path = os.path.normcase(os.path.abspath(path))
        folder = os.path.normcase(os.path.abspath(folder))
    except (TypeError, ValueError):
        return False
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def child_env(base=None, restart=False, bundle=None):
    """A copy of the environment for a process we start, without the bootloader's private state.

    restart=True is for starting the companion itself again: PYINSTALLER_RESET_ENVIRONMENT also
    tells the new bootloader to start as a fresh instance whatever it inherits."""
    env = dict(os.environ if base is None else base)
    bundle = bundle if bundle is not None else getattr(sys, "_MEIPASS", None)
    for key in list(env):
        upper = key.upper()
        if upper.startswith(PRIVATE_PREFIXES):
            del env[key]
        elif bundle and upper in BUNDLE_PATH_VARS and _inside(env[key], bundle):
            del env[key]
        elif bundle and upper == "PATH":
            parts = [part for part in env[key].split(os.pathsep) if part and not _inside(part, bundle)]
            env[key] = os.pathsep.join(parts)
    if restart:
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def update_script(temp_path, target, pids, log_path):
    """The PowerShell helper that swaps the exe once we are gone and starts the new version.

    A one-file exe runs as two processes (bootloader parent + Python child) and the parent keeps
    the exe open, so the helper waits for both, retries the swap until the file is free, starts the
    new version, checks that it stays up (else starts it once more through Explorer, which runs it
    with Explorer's own environment), notes each step in update.log and removes itself.  A note
    that cannot be written never stops it, and a failure anywhere still tries to start the target
    through Explorer: the companion must come back no matter what."""
    name = os.path.splitext(os.path.basename(target))[0]
    workdir = os.path.dirname(target) or "."
    return "\r\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$log = %s" % _quote(log_path),
        # a note that cannot be written (the log open elsewhere, a locked folder) must never stop
        # the swap or the restart: on 2026-10-07 a tail -F on update.log did exactly that
        "function Note($text) {",
        "  try {",
        "    Add-Content -LiteralPath $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' update: ' + $text)",
        "  } catch {",
        "    try { Add-Content -LiteralPath ($log + '.notes') -Value $text } catch {}",
        "  }",
        "}",
        "try {",
        "  foreach ($id in @(%s)) {" % ", ".join(str(int(pid)) for pid in pids),
        "    $p = Get-Process -Id $id -ErrorAction SilentlyContinue",
        "    if ($p -and $p.ProcessName -eq %s) { $p.WaitForExit(30000) | Out-Null }" % _quote(name),
        "  }",
        "  $ok = $false",
        "  for ($i = 0; $i -lt 40 -and -not $ok; $i++) {",
        "    try { Move-Item -LiteralPath %s -Destination %s -Force; $ok = $true }" % (
            _quote(temp_path), _quote(target)),
        "    catch { Start-Sleep -Milliseconds 500 }",
        "  }",
        "  if ($ok) { Note 'new version in place' } else { Note 'could not replace the exe' }",
        "  $new = Start-Process -FilePath %s -WorkingDirectory %s -PassThru" % (
            _quote(target), _quote(workdir)),
        "  Start-Sleep -Seconds 20",
        "  if ($new.HasExited) {",
        "    Note ('the new version stopped with code ' + $new.ExitCode + ', starting it from Explorer')",
        "    Start-Process -FilePath 'explorer.exe' -ArgumentList ('\"' + %s + '\"')" % _quote(target),
        "  } else {",
        "    Note ('restarted, pid ' + $new.Id)",
        "  }",
        "} catch {",
        "  Note ('failed: ' + $_)",
        "  try { Start-Process -FilePath 'explorer.exe' -ArgumentList ('\"' + %s + '\"') } catch {}" % _quote(target),
        "}",
        "try { Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force } catch {}",
    ])


def apply_companion_update(temp_path, quit_app, target=None):
    """Swap the running one-file exe for temp_path and restart it (see update_script).

    UTF-8 with BOM: Windows PowerShell 5.1 reads BOM-less scripts as ANSI, which breaks non-ASCII
    paths."""
    target = target or sys.executable
    script = os.path.join(tempfile.gettempdir(), "DedSecUplink-apply-update.ps1")
    body = update_script(temp_path, target, (os.getpid(), os.getppid()),
                         os.path.join(config.APP_DIR, "update.log"))
    with open(script, "w", encoding="utf-8-sig") as fh:
        fh.write(body)
    subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-File", script],
        creationflags=CREATE_NO_WINDOW, env=child_env(restart=True),
    )
    log.info("companion update downloaded, restarting")
    quit_app()
