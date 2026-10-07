"""One Tk interpreter in one thread for every companion window.

pystray owns the main thread, so Tk runs in a thread of its own. All windows (the tray panel, the
RF analyzer) are Toplevels of a hidden root created here; other threads never touch Tk objects, they post
callables with :meth:`UiThread.call`. Tk variables and widgets are created and released on this
thread too: releasing them from another thread at exit made Tcl abort the process.
"""
import gc
import logging
import queue
import threading

log = logging.getLogger("uplink.ui")

POLL_MS = 50


class UiThread:
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._stopping = False
        self._closers = []          # callables run on the Tk thread before it ends
        self.root = None

    # ------------------------------------------------------------------ public
    def call(self, fn, *args):
        """Run fn(*args) on the Tk thread (starting it on first use). Never blocks."""
        with self._lock:
            if self._stopping:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="ui", daemon=True)
                self._thread.start()
        self._queue.put((fn, args))

    def on_close(self, fn):
        """Register a callable that releases a window's Tk objects when the UI stops."""
        self._closers.append(fn)

    def stop(self, timeout=3.0):
        with self._lock:
            self._stopping = True
            thread = self._thread
        if thread is None:
            return
        self._queue.put((None, ()))
        thread.join(timeout)
        if thread.is_alive():
            log.warning("UI thread did not stop in time")

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ Tk thread
    def _run(self):
        try:
            import tkinter as tk
        except Exception as exc:  # pragma: no cover - minimal Python installs
            log.warning("windows are unavailable: %s", exc)
            return
        try:
            root = tk.Tk()
        except Exception as exc:  # pragma: no cover - needs a desktop session
            log.warning("cannot start the window system: %s", exc)
            return
        root.withdraw()
        try:
            from tkinter import ttk
            ttk.Style(root).theme_use("clam")   # one look for every window (dark styles need clam)
        except Exception:
            pass
        self.root = root
        root.after(POLL_MS, self._poll)
        try:
            root.mainloop()
        finally:
            for closer in self._closers:
                try:
                    closer()
                except Exception:
                    log.debug("window close failed", exc_info=True)
            self._closers = []
            try:
                root.destroy()
            except Exception:
                pass
            self.root = None
            del root
            gc.collect()  # Tk objects must die on this thread

    def _poll(self):
        root = self.root
        while True:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                break
            if fn is None:
                root.quit()
                return
            try:
                fn(*args)
            except Exception:
                log.exception("UI action failed")
        root.after(POLL_MS, self._poll)
