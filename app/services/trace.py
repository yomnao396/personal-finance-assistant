# app/services/trace.py
# Which processing stages one chat answer went through, and how long each took:
# interpretation, context, retrieval (database queries and records read),
# calculation, validation and the model. It shows whether a fast answer did the
# work (the records were read, the result was checked) or skipped it.
#
# Only stage names, timings and counts are kept and logged: never amounts,
# names or message text.

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger("pfa.processing")
_current = ContextVar("pfa_trace", default=None)


class Trace:
    def __init__(self):
        self.started = time.perf_counter()
        self.stages = []            # [(name, milliseconds)]
        self.queries = 0            # database queries
        self.rows = 0               # records read
        self.model_calls = 0

    def summary(self, path, intent):
        return {"path": path, "intent": intent,
                "stages": [{"stage": n, "ms": ms} for n, ms in self.stages],
                "database_queries": self.queries, "records_read": self.rows, "model_calls": self.model_calls,
                "total_ms": round((time.perf_counter() - self.started) * 1000, 2)}


def start():
    trace = Trace()
    _current.set(trace)
    return trace


def current():
    return _current.get()


@contextmanager
def stage(name):
    """Time one stage of the answer in progress (a no-op outside one)."""
    trace = _current.get()
    began = time.perf_counter()
    try:
        yield
    finally:
        if trace is not None:
            trace.stages.append((name, round((time.perf_counter() - began) * 1000, 2)))


def mark(name, since):
    """Record a stage that started at `since` (time.perf_counter()); returns now."""
    now = time.perf_counter()
    trace = _current.get()
    if trace is not None:
        trace.stages.append((name, round((now - since) * 1000, 2)))
    return now


def count_query(rows):
    trace = _current.get()
    if trace is not None:
        trace.queries += 1
        trace.rows += rows


def count_model_call():
    trace = _current.get()
    if trace is not None:
        trace.model_calls += 1


def log(summary):
    stages = ", ".join(f"{s['stage']}={s['ms']}ms" for s in summary["stages"])
    logger.info("chat answered: path=%s intent=%s stages=[%s] queries=%d records=%d model_calls=%d total=%sms",
                summary["path"], summary["intent"], stages, summary["database_queries"], summary["records_read"],
                summary["model_calls"], summary["total_ms"])
