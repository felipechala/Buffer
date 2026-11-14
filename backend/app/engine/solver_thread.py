"""Run SciPy/HiGHS solves on one persistent daemon thread.

SciPy 1.17.1's HiGHS (linprog method="highs") corrupts process state when it runs in a thread
that later exits; the next threading.Thread.start() then hits a Windows access violation and can
hang forever. FastAPI/TestClient run sync endpoints in short-lived worker threads, so every solve
is funnelled through a single thread that never exits (this also serializes solves). Do not use a
ThreadPoolExecutor: its worker is joined at interpreter exit, which reproduces the crash.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future

_SOLVER_Q: "queue.Queue" = queue.Queue()
_solver_thread: threading.Thread | None = None
_solver_start_lock = threading.Lock()


def _solver_loop() -> None:
    while True:
        fut, fn, args, kwargs = _SOLVER_Q.get()
        if not fut.set_running_or_notify_cancel():
            continue
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:  # propagate to the caller
            fut.set_exception(e)


def _run_on_solver_thread(fn, *args, **kwargs):
    global _solver_thread
    with _solver_start_lock:
        if _solver_thread is None:
            _solver_thread = threading.Thread(target=_solver_loop, daemon=True, name="highs-solver")
            _solver_thread.start()
    fut: Future = Future()
    _SOLVER_Q.put((fut, fn, args, kwargs))
    return fut.result()
