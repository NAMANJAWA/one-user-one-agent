"""Simulated dependencies with latency and faults, driven by replay/control_events.jsonl.

The harness (not the agent) reads `dependency_unavailable` / `dependency_degraded` control events and makes these
clients slow or failing for the stated window. The agent only sees the symptoms (timeouts, errors) and has to
notice them itself, as it would in production.
"""
import hashlib

from .timeutil import parse


def _h01(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:8], 16) / 16 ** 8


class StoreTimeout(Exception):
    def __init__(self, latency_ms):
        super().__init__('data store read timed out after %d ms' % latency_ms)
        self.latency_ms = latency_ms


class Budget:
    """Per-decision call budget. Configurable; exceeding it is a hard stop, not a warning."""

    def __init__(self, max_model, max_tool):
        self.max_model, self.max_tool = max_model, max_tool
        self.model_calls = 0
        self.tool_calls = 0

    def take_model(self):
        if self.model_calls >= self.max_model:
            return False
        self.model_calls += 1
        return True

    def take_tool(self):
        if self.tool_calls >= self.max_tool:
            return False
        self.tool_calls += 1
        return True

    def to_json(self):
        return {'model_calls': self.model_calls, 'tool_calls': self.tool_calls,
                'limits': {'model_calls': self.max_model, 'tool_calls': self.max_tool}}


class DataStoreClient:
    """Reads a viewer's profile rows and events from the platform store, with simulated latency.

    Normal: 15-60 ms per read. Degraded window: 20% of reads time out, the rest have a long tail (p95 ~8 s).
    The agent passes its own timeout; a read slower than that raises StoreTimeout after `timeout_ms`."""

    def __init__(self, pack, event_store):
        self.pack, self.events = pack, event_store
        self.windows = []   # (start, end, mode)
        self.stats = {'reads': 0, 'timeouts': 0}

    def add_window(self, control):
        mode = 'unavailable' if control['type'] == 'dependency_unavailable' else 'degraded'
        self.windows.append((parse(control['at']), parse(control['until']), mode))

    def mode(self, now):
        for start, end, mode in self.windows:
            if start <= now < end:
                return mode
        return 'normal'

    def _latency(self, key, now):
        mode = self.mode(now)
        u = _h01('store', key)
        if mode == 'unavailable':
            return None
        if mode == 'degraded':
            if u < 0.20:
                return None
            v = (u - 0.20) / 0.80
            return int(300 + 9000 * v ** 3)
        return int(15 + 45 * u)

    def read(self, kind, user_id, now, timeout_ms, budget, key):
        if not budget.take_tool():
            raise StoreTimeout(0)
        self.stats['reads'] += 1
        lat = self._latency('%s:%s:%s' % (kind, user_id, key), now)
        if lat is None or lat > timeout_ms:
            self.stats['timeouts'] += 1
            raise StoreTimeout(timeout_ms)
        if kind == 'profile':
            return self.pack.profile_rows.get(user_id, []), lat
        return self.events.events_for(user_id, known_by=now), lat
