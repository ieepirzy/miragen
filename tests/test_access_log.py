import logging

from miragen.access_log import RoutinePollingFilter, install


def record(path: str, status: int = 200) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", path, "1.1", status), None)


def test_routine_polling_is_sampled_and_the_rest_passes():
    f = RoutinePollingFilter(every=100)
    health = [f.filter(record("/health")) for _ in range(250)]
    assert sum(health) == 3          # the 1st, 101st and 201st
    approvals = [f.filter(record("/approvals?since=0&wait=30.0")) for _ in range(100)]
    assert sum(approvals) == 1       # counted separately from /health
    assert f.filter(record("/instances/mira/turns"))
    assert f.filter(record("/runs/abc", status=404))   # failures always show
    assert f.filter(record("/health", status=503))


def test_every_one_logs_everything_and_install_is_idempotent():
    f = RoutinePollingFilter(every=1)
    assert all(f.filter(record("/health")) for _ in range(5))
    install()
    install()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(x, RoutinePollingFilter) for x in filters) == 1
