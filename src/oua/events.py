"""Event normalisation and the platform event store.

Time rules (README conventions):
  * received_at is set by our servers and is trusted.
  * occurred_at and sent_at come from the phone clock, which can be off by hours.
  * Both phone timestamps come from the same clock, so (sent_at - occurred_at) is the time the event sat in
    the phone's offline buffer even when the clock itself is wrong. The best estimate of when the event really
    happened is therefore received_at - (sent_at - occurred_at). We never order or window on raw occurred_at.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from .pack import norm_dialect, read_jsonl
from .timeutil import parse


@dataclass
class Event:
    event_id: str
    user_id: str
    event: str
    est_at: object          # best estimate of when it happened (server-anchored)
    received_at: object     # trusted server time
    known_at: object        # when the agent could first have known it (deliver_at in replay)
    buffered_s: float       # time spent in the phone's offline buffer
    clock_skew_s: float     # phone clock minus server clock, as far as we can tell
    session_id: str
    app_dialect: object     # normalised dialect code or None
    app_dialect_raw: object
    app_language: object
    props: dict
    source: str             # history | live | trigger | outcome

    @property
    def ref(self):
        return 'event:' + self.event_id


def normalize(raw, source, known_at=None):
    received = parse(raw['received_at'])
    occurred = parse(raw.get('occurred_at')) or received
    sent = parse(raw.get('sent_at')) or received
    buffered = max(0.0, (sent - occurred).total_seconds())
    est = received - timedelta(seconds=buffered)
    return Event(
        event_id=raw['event_id'], user_id=raw['user_id'], event=raw['event'], est_at=est, received_at=received,
        known_at=parse(raw.get('deliver_at')) or known_at or received, buffered_s=buffered,
        clock_skew_s=(sent - received).total_seconds(), session_id=raw.get('session_id'),
        app_dialect=norm_dialect(raw.get('app_dialect')), app_dialect_raw=raw.get('app_dialect'),
        app_language=raw.get('app_language'), props=raw.get('properties') or {}, source=source)


class EventStore:
    """The platform's event store (a stand-in for the warehouse / event bus). Deduplicates by event_id: the
    first delivery wins and later copies are counted, never re-applied."""

    def __init__(self):
        self.by_user = defaultdict(list)
        self.by_id = {}
        self.duplicates = 0

    def ingest(self, ev):
        if ev.event_id in self.by_id:
            self.duplicates += 1
            return False
        self.by_id[ev.event_id] = ev
        evs = self.by_user[ev.user_id]
        evs.append(ev)
        if len(evs) > 1 and (evs[-2].est_at, evs[-2].received_at) > (ev.est_at, ev.received_at):
            evs.sort(key=lambda e: (e.est_at, e.received_at))
        return True

    def load_history(self, path):
        n = 0
        for raw in read_jsonl(path):
            if self.ingest(normalize(raw, 'history')):
                n += 1
        return n

    def events_for(self, user_id, known_by=None):
        evs = self.by_user.get(user_id, [])
        if known_by is None:
            return list(evs)
        return [e for e in evs if e.known_at <= known_by]

    def get(self, event_id):
        return self.by_id.get(event_id)
