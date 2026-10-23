"""Durable agent memory (SQLite).

  viewer_state    one JSON document per viewer: facts with provenance, the single active goal and plan, every
                  decision/action/outcome id, learnings and channel state. One writer per viewer (the agent).
  journal         append-only log of everything the agent learned, decided, did and observed, with the virtual
                  time it happened. `oua explain VIEWER --at T` replays it to show what the agent knew at T.
  processed       trigger event ids already handled (idempotency across duplicates, restarts and re-runs).
  goal_instances  indexed by (goal_id, status) so a rule correction touches only the open goals it affects.
  actions / action_titles   indexed by title so a withdrawal finds the pages and queued messages it affects.
"""
import json
import os
import sqlite3
import zlib

from .timeutil import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewer_state(user_id TEXT PRIMARY KEY, doc TEXT NOT NULL, version INTEGER NOT NULL,
                                        updated_at TEXT);
CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user_id TEXT, kind TEXT,
                                   ref TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS journal_user ON journal(user_id, at);
CREATE INDEX IF NOT EXISTS journal_kind ON journal(kind);
CREATE TABLE IF NOT EXISTS processed(event_id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, decision_id TEXT,
                                     at TEXT);
CREATE TABLE IF NOT EXISTS goal_instances(instance_id TEXT PRIMARY KEY, user_id TEXT, goal_id TEXT,
                                          rule_version TEXT, status TEXT, started_at TEXT, deadline TEXT,
                                          decision_id TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS gi_goal_status ON goal_instances(goal_id, status);
CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY, decision_id TEXT, user_id TEXT, type TEXT,
                                   status TEXT, published_at TEXT, send_at TEXT, expires_at TEXT, session_id TEXT,
                                   goal_instance_id TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS actions_user ON actions(user_id);
CREATE INDEX IF NOT EXISTS actions_outbox ON actions(status, send_at);
CREATE TABLE IF NOT EXISTS action_titles(action_id TEXT, title_id TEXT);
CREATE INDEX IF NOT EXISTS at_title ON action_titles(title_id);
CREATE INDEX IF NOT EXISTS at_action ON action_titles(action_id);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
"""


def journal_doc(blob):
    """Journal docs are zlib-compressed JSON (the journal repeats every decision in full)."""
    if isinstance(blob, bytes):
        return json.loads(zlib.decompress(blob).decode('utf-8'))
    return json.loads(blob)


def new_state(user_id, arm):
    return {'viewer_id': user_id, 'arm': arm, 'facts': {}, 'active_goal': None, 'plan': None,
            'goal_history': [], 'decisions': [], 'actions': [], 'outcomes': [], 'learnings': [],
            'channel_state': {}, 'sessions_acted': {}, 'last_trigger_at': None}


class Memory:
    def __init__(self, path, fresh=False):
        if fresh and os.path.exists(path):
            os.remove(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._cache = {}

    # ------------------------------------------------------------------ ids
    def next_id(self, prefix):
        cur = self.db.execute('SELECT value FROM counters WHERE name=?', (prefix,)).fetchone()
        n = (cur[0] if cur else 0) + 1
        self.db.execute('INSERT OR REPLACE INTO counters(name, value) VALUES(?, ?)', (prefix, n))
        return '%s-%06d' % (prefix, n)

    # ------------------------------------------------------------------ state
    def load(self, user_id, arm=None):
        if user_id in self._cache:
            return self._cache[user_id]
        row = self.db.execute('SELECT doc FROM viewer_state WHERE user_id=?', (user_id,)).fetchone()
        state = json.loads(row[0]) if row else new_state(user_id, arm)
        self._cache[user_id] = state
        return state

    def save(self, state, at):
        uid = state['viewer_id']
        self._cache[uid] = state
        self.db.execute(
            'INSERT INTO viewer_state(user_id, doc, version, updated_at) VALUES(?, ?, 1, ?) '
            'ON CONFLICT(user_id) DO UPDATE SET doc=excluded.doc, version=viewer_state.version+1, '
            'updated_at=excluded.updated_at', (uid, json.dumps(state, sort_keys=True), iso(at)))

    def journal(self, at, user_id, kind, ref, doc):
        blob = zlib.compress(json.dumps(doc, sort_keys=True, default=str).encode('utf-8'), 6)
        self.db.execute('INSERT INTO journal(at, user_id, kind, ref, doc) VALUES(?, ?, ?, ?, ?)',
                        (iso(at), user_id, kind, ref, blob))

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()

    # ------------------------------------------------------------------ idempotency
    def processed(self, event_id):
        row = self.db.execute('SELECT decision_id FROM processed WHERE event_id=?', (event_id,)).fetchone()
        return row[0] if row else None

    def mark_processed(self, event_id, user_id, session_id, decision_id, at):
        self.db.execute('INSERT OR IGNORE INTO processed VALUES(?, ?, ?, ?, ?)',
                        (event_id, user_id, session_id, decision_id, iso(at)))

    # ------------------------------------------------------------------ goals
    def put_goal(self, gi):
        self.db.execute('INSERT OR REPLACE INTO goal_instances VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (gi['instance_id'], gi['viewer_id'], gi['goal_id'], gi['rule_version'], gi['status'],
                         gi['started_at'], gi['deadline'], gi['decision_id'], json.dumps(gi, sort_keys=True)))

    def goals_for(self, goal_id, statuses):
        q = 'SELECT doc FROM goal_instances WHERE goal_id=? AND status IN (%s)' % ','.join('?' * len(statuses))
        return [json.loads(r[0]) for r in self.db.execute(q, (goal_id, *statuses))]

    def goal(self, instance_id):
        row = self.db.execute('SELECT doc FROM goal_instances WHERE instance_id=?', (instance_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def count_messages_between(self, start_iso, end_iso):
        """Push/WhatsApp messages queued or sent with send_at in (start, end], across all viewers."""
        return self.db.execute("SELECT COUNT(*) FROM actions WHERE type IN ('push', 'whatsapp') AND status IN "
                               "('queued', 'sent') AND send_at>? AND send_at<=?", (start_iso, end_iso)).fetchone()[0]

    def all_goals(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT doc FROM goal_instances ORDER BY instance_id')]

    # ------------------------------------------------------------------ actions
    def put_action(self, a, status):
        self.db.execute('INSERT OR REPLACE INTO actions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (a['action_id'], a['decision_id'], a['user_id'], a['type'], status, a.get('published_at'),
                         a.get('send_at'), a.get('_expires_at'), a.get('_session_id'), a.get('_goal_instance_id'),
                         json.dumps(a, sort_keys=True)))
        self.db.execute('DELETE FROM action_titles WHERE action_id=?', (a['action_id'],))
        tids = set()
        if a.get('hero_title_id'):
            tids.add(a['hero_title_id'])
        for r in a.get('rows') or []:
            tids.update(r['title_ids'])
        if a.get('title_id'):
            tids.add(a['title_id'])
        self.db.executemany('INSERT INTO action_titles VALUES(?, ?)', [(a['action_id'], t) for t in sorted(tids)])

    def set_action_status(self, action_id, status):
        self.db.execute('UPDATE actions SET status=? WHERE action_id=?', (status, action_id))

    def actions_with_title(self, title_id, statuses):
        q = ('SELECT a.doc, a.status FROM actions a JOIN action_titles t ON a.action_id=t.action_id '
             'WHERE t.title_id=? AND a.status IN (%s)' % ','.join('?' * len(statuses)))
        return [(json.loads(r[0]), r[1]) for r in self.db.execute(q, (title_id, *statuses))]

    def actions_for(self, user_id):
        rows = self.db.execute('SELECT doc, status FROM actions WHERE user_id=? ORDER BY action_id', (user_id,))
        return [(json.loads(r[0]), r[1]) for r in rows]

    def all_states(self):
        for uid, doc in self.db.execute('SELECT user_id, doc FROM viewer_state ORDER BY user_id'):
            yield uid, json.loads(doc)

    def journal_of_kind(self, kind):
        return [journal_doc(r[0]) for r in self.db.execute('SELECT doc FROM journal WHERE kind=? ORDER BY seq',
                                                           (kind,))]

    def journal_for(self, user_id, until_iso=None):
        q = 'SELECT seq, at, kind, ref, doc FROM journal WHERE user_id=?'
        args = [user_id]
        if until_iso:
            q += ' AND at<=?'
            args.append(until_iso)
        return [{'seq': r[0], 'at': r[1], 'kind': r[2], 'ref': r[3], 'doc': journal_doc(r[4])}
                for r in self.db.execute(q + ' ORDER BY seq', args)]
