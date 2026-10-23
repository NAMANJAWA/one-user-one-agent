"""Organic baseline: how likely a viewer is to reach a goal WITHOUT the agent.

Built only from data that exists before the replay day:
  * the pilot's randomised holdout arm (pilot_outcomes.csv): true no-agent outcomes for G02, G04, G05, G06, G09;
  * the 30-day event history: "anchor" visits (fresh app opens between day 8 and day 23) and what each viewer did
    in the following window, for G01, G03, G07, G08, G12.

Where the data supports it the rate depends on the viewer (G07: episodes watched x days idle; G08: play days in the
last 14), smoothed toward the goal's overall rate. On the replay day the holdout arm receives nothing, so its
outcomes test this model directly (calibration table in impact_report.md).

The agent uses it to decide how much an intervention can move a goal: movability = 4p(1-p) is 0 for sure things
(p=1) and lost causes (p=0) and 1 at p=0.5. Score = goal value x movability."""
import csv
import os
from collections import defaultdict
from datetime import timedelta

from .timeutil import ist_day, parse

SMOOTHING = 20      # pseudo-observations pulling a bucket toward its goal's overall rate


def movability(p):
    return 4.0 * p * (1.0 - p)


class Rate:
    def __init__(self):
        self.k, self.n = 0, 0

    def add(self, ok):
        self.k += bool(ok)
        self.n += 1

    @property
    def p(self):
        return self.k / self.n if self.n else None


class OrganicBaseline:
    def __init__(self, pack, cfg, store, replay_start, table=None):
        self.pack, self.cfg = pack, cfg
        self.goal = defaultdict(Rate)                      # goal -> overall rate
        self.bucket = defaultdict(lambda: defaultdict(Rate))   # goal -> bucket -> rate
        self.source = {}
        self.updates = []
        if table is not None:
            self._load(table)
        else:
            self._pilot(pack)
            self._history(pack, store, replay_start)

    # ------------------------------------------------------------------ persistence and learning
    def _load(self, table):
        """Loads a saved table (e.g. one fitted on an earlier period and updated with observed outcomes)."""
        for gid, t in table.items():
            if not t.get('n'):
                continue
            r = self.goal[gid]
            r.n, r.k = t['n'], int(round(t['p'] * t['n']))
            self.source[gid] = t.get('source')
            for b, bt in (t.get('buckets') or {}).items():
                rb = self.bucket[gid][b]
                rb.n, rb.k = bt['n'], int(round(bt['p'] * bt['n']))

    def update(self, observations, label):
        """Adds observed no-agent outcomes [(goal_id, bucket or None, succeeded)], e.g. the holdout arm's goals
        after a replay day. Holdout viewers got nothing, so their outcomes are exactly what the baseline predicts;
        adding them moves the rates toward what happens in the current environment."""
        added = defaultdict(int)
        for gid, bucket, ok in observations:
            self.goal[gid].add(ok)
            if bucket:
                self.bucket[gid][bucket].add(ok)
            added[gid] += 1
        for gid, n in added.items():
            self.source[gid] = '%s + %d %s' % (self.source.get(gid) or 'no prior', n, label)
        self.updates.append({'label': label, 'added': dict(added)})

    # ------------------------------------------------------------------ fitting
    def _pilot(self, pack):
        path = os.path.join(pack.root, 'pilot', 'pilot_outcomes.csv')
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r['arm'] == 'holdout':
                    self.goal[r['goal_id']].add(r['succeeded'] == 'true')
                    self.source[r['goal_id']] = 'pilot holdout arm (no agent), n=%d'
        for gid in list(self.source):
            self.source[gid] = self.source[gid] % self.goal[gid].n

    def _history(self, pack, store, replay_start):
        lo = parse(self.cfg['history']['coverage_start']) + timedelta(days=7)
        hi = replay_start - timedelta(days=7)
        for uid, evs in store.by_user.items():
            prof = pack.profile_rows.get(uid, [])
            signups = [e.est_at for e in evs if e.event == 'signup_completed']
            signup = min(signups) if signups else (parse(prof[-1]['signup_at']) if prof and prof[-1].get('signup_at')
                                                   else None)
            active = bool(prof) and prof[-1]['subscription_status'] == 'active'
            seen = set()
            for a in evs:
                if a.event != 'app_open' or a.buffered_s > 600 or a.session_id in seen or not lo <= a.est_at <= hi:
                    continue
                seen.add(a.session_id)
                self._anchor(pack, evs, a, signup, active)
        for gid in ('G01', 'G03', 'G07', 'G08', 'G12'):
            if self.goal[gid].n:
                self.source[gid] = 'history anchors before the replay day, n=%d' % self.goal[gid].n

    def _anchor(self, pack, evs, a, signup, active):
        T = a.est_at
        before = [e for e in evs if e.est_at < T]
        after = [e for e in evs if e.est_at >= T]
        plays_b = [e for e in before if e.event == 'play_started']
        within = lambda e, h: e.est_at <= T + timedelta(hours=h)
        if signup and timedelta(0) <= T - signup <= timedelta(days=7) and not plays_b:
            self.goal['G01'].add(any(e.event == 'play_started' and within(e, 24) for e in after))
        if signup and timedelta(0) <= T - signup <= timedelta(days=2):
            for ev in ('play_started', 'app_open'):
                self.bucket['G03'][ev].add(any(e.event == ev and ist_day(e.est_at) > ist_day(T) and within(e, 48)
                                               for e in after))
            self.goal['G03'].add(any(e.event == 'play_started' and ist_day(e.est_at) > ist_day(T) and within(e, 48)
                                     for e in after))
        opens = [e for e in before if e.event in ('app_open', 'login_success') and e.session_id != a.session_id
                 and T - timedelta(days=14) <= e.est_at]
        first = min((e.est_at for e in evs), default=T)
        if not opens and signup and T - signup >= timedelta(days=14) and T - first >= timedelta(days=14):
            self.goal['G12'].add(any(e.event == 'play_started' and within(e, 168) for e in after))
        progress = {}
        for e in before:
            if e.event in ('play_started', 'episode_completed'):
                ep = pack.episode_index.get(e.props.get('episode_id'))
                t = pack.titles.get(e.props.get('title_id'))
                if ep and t and t.content_type != 'film':
                    p = progress.setdefault(ep[0], {'max': 0, 'total': ep[2], 'last': None})
                    p['max'] = max(p['max'], ep[1])
                    if e.event == 'play_started':
                        p['last'] = max(p['last'] or e.est_at, e.est_at)
        for slug, p in progress.items():
            if p['last'] and p['max'] < p['total'] and T - p['last'] >= timedelta(days=3):
                ok = any(e.event == 'play_started' and within(e, 72) and pack.titles.get(e.props.get('title_id'))
                         and pack.titles[e.props['title_id']].slug == slug for e in after)
                self.goal['G07'].add(ok)
                self.bucket['G07'][self.g07_bucket(p['max'], (T - p['last']).days)].add(ok)
        if active:
            days = len({ist_day(e.est_at) for e in plays_b if T - timedelta(days=14) <= e.est_at})
            lo_d = T.date() - timedelta(days=30)
            ok = any(e.event == 'play_started' and within(e, 168) and pack.titles.get(e.props.get('title_id'))
                     and lo_d <= parse(pack.titles[e.props['title_id']].release_date + 'T00:00:00Z').date() <= T.date()
                     for e in after)
            self.goal['G08'].add(ok)
            self.bucket['G08'][self.g08_bucket(days)].add(ok)

    @staticmethod
    def g07_bucket(watched, idle_days):
        return '%s/%s' % ('ep1' if watched <= 1 else 'ep2+',
                          '3-6d' if idle_days < 7 else '7-13d' if idle_days < 14 else '14d+')

    @staticmethod
    def g08_bucket(play_days):
        return 'play_days_0-1' if play_days <= 1 else 'play_days_2-4' if play_days <= 4 else 'play_days_5+'

    # ------------------------------------------------------------------ prediction
    def _smoothed(self, gid, bucket):
        prior = self.goal[gid].p
        b = self.bucket[gid].get(bucket) if bucket else None
        if b is None or not b.n or prior is None:
            return prior, (self.goal[gid].n if prior is not None else 0)
        return (b.k + SMOOTHING * prior) / (b.n + SMOOTHING), b.n

    def predict(self, gid, view=None, detail=None, success_event=None):
        """Returns (p_without_agent, basis) or (None, reason) when the pack has no data for the goal."""
        bucket = None
        if gid == 'G07' and detail and detail.get('series') and view is not None:
            s = detail['series']
            prog = view.progress.get(s['slug']) or {}
            last = prog.get('last_play')
            idle = (view.now - last).days if last else 30
            bucket = self.g07_bucket(s.get('next_episode', 2) - 1, idle)
        elif gid == 'G08' and view is not None:
            bucket = self.g08_bucket(view.value('play_days_14d') or 0)
        elif gid == 'G03' and success_event:
            b = self.bucket['G03'].get(success_event)
            if b and b.n:
                return b.p, {'source': self.source.get('G03'), 'bucket': 'success=' + success_event, 'n': b.n}
        p, n = self._smoothed(gid, bucket)
        if p is None:
            return None, {'source': 'no pre-replay data for %s' % gid}
        return round(p, 4), {'source': self.source.get(gid), 'bucket': bucket, 'n': n}

    def table(self):
        out = {}
        for gid in sorted(self.goal):
            out[gid] = {'p': round(self.goal[gid].p, 4) if self.goal[gid].p is not None else None,
                        'n': self.goal[gid].n, 'source': self.source.get(gid),
                        'buckets': {b: {'p': round(r.p, 4), 'n': r.n} for b, r in sorted(self.bucket[gid].items())
                                    if r.n}}
        return out
