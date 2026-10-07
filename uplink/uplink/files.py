"""Durable file replacement, also on PCs where renames in AppData are refused.

On some PCs (an encrypted AppData folder) every rename inside the folder fails with
ERROR_NOT_SAME_DEVICE (17), even from one name to another in the same directory. MoveFileEx then
has to copy (MOVEFILE_COPY_ALLOWED): not atomic, but the copy is written from the start and
flushed, so an interrupted one leaves a prefix of the new file, and the source stays until the
copy is complete.
"""
import os
import time

ERROR_NOT_SAME_DEVICE = 17
RETRY_ERRORS = (5, 32, 33)   # access denied / sharing / lock violation: a reader holds the target

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MoveFileExW = _kernel32.MoveFileExW
    _MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    _MoveFileExW.restype = wintypes.BOOL
    MOVEFILE_REPLACE_EXISTING = 0x1
    MOVEFILE_COPY_ALLOWED = 0x2
    MOVEFILE_WRITE_THROUGH = 0x8

    def replace(src, dst) -> None:
        """Rename src over dst and return only after the change reached the disk.

        A reader (for example the analyzer reloading the folder) can hold the target open for a
        moment; Windows reports that as a sharing violation, so retry briefly before giving up."""
        flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        for attempt in range(10):
            if _MoveFileExW(os.fspath(src), os.fspath(dst), flags):
                return
            error = ctypes.get_last_error()
            if error == ERROR_NOT_SAME_DEVICE and not flags & MOVEFILE_COPY_ALLOWED:
                flags |= MOVEFILE_COPY_ALLOWED   # renames refused here: copy, then delete src
                continue
            if error not in RETRY_ERRORS or attempt == 9:
                raise ctypes.WinError(error)
            time.sleep(0.05)
else:
    def replace(src, dst) -> None:
        os.replace(src, dst)
        try:
            fd = os.open(os.path.dirname(os.path.abspath(dst)), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)   # the rename itself must survive a power cut
        except OSError:
            pass
        finally:
            os.close(fd)
