"""
ui/async_utils.py
==================
Tkinter is not thread-safe — every widget read/write has to happen on the Tk
main thread. This is the one place that pattern is implemented: run a
blocking call (an ApiClient request) on a background thread, then hand the
result back on the main thread via a queue drained by a periodic `.after()`
poll — never by touching widgets, or calling `.after()`, from the worker
thread itself.

Used by ui/panel_app.py and ui/config_panel.py so opening the panel (or any
tab in it) never blocks the window from appearing/responding while it waits
on the dispatcher's local API.
"""

import queue
import threading


def run_async(widget, work, on_done, on_error=None, poll_ms: int = 80):
    """
    Runs `work()` (no args) in a background thread.
    - On success: `on_done(result)` is called on the Tk main thread.
    - On exception: `on_error(exception)` is called on the Tk main thread
      (if given — otherwise the exception is swallowed after being queued).
    `widget` only needs a working `.after()` — any Tk widget qualifies.
    """
    q: "queue.Queue" = queue.Queue(maxsize=1)

    def _worker():
        try:
            q.put(("ok", work()))
        except Exception as e:
            q.put(("error", e))

    def _poll():
        try:
            status, payload = q.get_nowait()
        except queue.Empty:
            widget.after(poll_ms, _poll)
            return
        if status == "ok":
            on_done(payload)
        elif on_error:
            on_error(payload)

    threading.Thread(target=_worker, daemon=True).start()
    widget.after(poll_ms, _poll)


def run_async_retrying(widget, work, on_done, on_final_error=None,
                        retries: int = 6, retry_delay_ms: int = 1500, poll_ms: int = 80):
    """
    Like run_async, but retries `work()` on failure before giving up —
    covers the panel-startup race where the window (and its first API calls)
    can exist a beat before the dispatcher's local HTTP server has actually
    bound its port yet ("<urlopen error ...>" / connection refused, purely
    transient). `on_final_error` only fires once every retry is exhausted.
    """
    attempt = {"n": 0}

    def _try():
        def _on_error(e):
            attempt["n"] += 1
            if attempt["n"] >= retries:
                if on_final_error:
                    on_final_error(e)
                return
            widget.after(retry_delay_ms, _try)

        run_async(widget, work=work, on_done=on_done, on_error=_on_error, poll_ms=poll_ms)

    _try()
