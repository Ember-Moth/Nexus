"""Bounded process-local observations supplemented by authoritative database gauges."""

from collections import Counter
from time import monotonic


class ProcessMetrics:
    def __init__(self):
        self.http = Counter()
        self.queries = Counter()

    def response(self, method, status, started):
        label = method if method in {"GET", "POST", "PATCH", "PUT", "DELETE"} else "OTHER"
        self.http[label, "requests"] += 1
        self.http[label, "seconds"] += monotonic() - started
        if status >= 400:
            self.http[label, "errors"] += 1

    def connection(self, seconds):
        self.queries["acquisitions"] += 1
        self.queries["acquire_seconds"] += seconds

    def snapshot(self):
        return {
            "http": {
                method: {key: self.http[method, key] for key in ("requests", "errors", "seconds")}
                for method, _ in self.http
            },
            "database": dict(self.queries),
        }
