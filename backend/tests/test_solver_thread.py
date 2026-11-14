import threading

import pytest
from scipy.optimize import linprog

from app.engine.solver_thread import _run_on_solver_thread


def _tiny():
    # min x + y  s.t. -x - y <= -2 (x + y >= 2), x,y >= 0  -> optimum 2
    return _run_on_solver_thread(linprog, [1, 1], A_ub=[[-1, -1]], b_ub=[-2],
                                 bounds=[(0, None), (0, None)], method="highs")


def test_short_lived_threads_then_plain_thread():
    results, errors = [], []

    def worker():
        try:
            results.append(_tiny().fun)
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    for _ in range(10):
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()
        plain = threading.Thread(target=lambda: None)
        plain.start()
        plain.join(timeout=30)
        assert not plain.is_alive()

    assert not errors
    assert len(results) == 10
    assert all(abs(r - 2.0) < 1e-7 for r in results)


def test_exceptions_propagate():
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        _run_on_solver_thread(boom)
    assert abs(_tiny().fun - 2.0) < 1e-7  # solver thread still alive
