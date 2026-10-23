"""Builds tiny synthetic packs so each test controls exactly one situation. Config files (goals, interventions,
pricing) are copies of the pack's config; all viewers, titles and events here are invented for the tests."""
import csv
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from oua.runtime import Replay, load_config  # noqa: E402

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 1, tzinfo=IST)          # replay day starts 2026-10-01 00:00 IST
SNAPSHOT = '2026-09-30T00:00:00.000Z'
FIX = os.path.join(ROOT, 'tests', 'fixtures')
REAL_PACK = os.environ.get('OUA_PACK', os.path.join(ROOT, '..', 'one_user_one_agent_pack'))


def iso(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def ist(h, m=0, day=0):
    return DAY + timedelta(days=day, hours=h, minutes=m)


def ev(user, event, at, props=None, dialect='d1', lang='hin', session=None, buffered_s=0, skew_s=0, eid=None,
       deliver=None):
    """`at` is when it really happened. buffered_s = time in the phone's offline buffer; skew_s = phone clock error."""
    occurred = at + timedelta(seconds=skew_s)
    sent = occurred + timedelta(seconds=buffered_s)
    received = at + timedelta(seconds=buffered_s + 1)
    d = {'event_id': eid or str(uuid.uuid4()), 'user_id': user, 'event': event, 'occurred_at': iso(occurred),
         'sent_at': iso(sent), 'received_at': iso(received), 'session_id': session or str(uuid.uuid4()),
         'app_dialect': dialect, 'app_language': lang, 'platform': 'android', 'app_version': '6.1.4',
         'properties': props or {}}
    if deliver is not None:
        d['deliver_at'] = iso(deliver)
    return d


def trig(user, at, **kw):
    e = ev(user, kw.pop('event', 'app_open'), at, **kw)
    e.setdefault('deliver_at', e['received_at'])
    return e


def live(user, event, at, **kw):
    e = ev(user, event, at, **kw)
    e.setdefault('deliver_at', e['received_at'])
    return e


def profile(user, status='never', dialect='d1', lang='hin', push='true', wa='false', signup='2026-01-10T10:00:00Z',
            updated='2026-09-29T10:00:00.000Z'):
    return {'user_id': user, 'signup_at': signup, 'subscription_status': status, 'plan_started_at': '',
            'plan_ends_at': '', 'preferred_dialect': dialect, 'preferred_language': lang,
            'notifications_opt_in': push, 'whatsapp_opt_in': wa, 'platform': 'android', 'updated_at': updated,
            'snapshot_at': SNAPSHOT}


def title(tid, dialect='d1', ctype='micro_drama', genres='Drama|Comedy', premium='false', lang='hin', slug=None,
          release='2025-01-01', until='', desc='A drama micro-drama set in a small town.', name=None):
    return {'title_id': tid, 'content_slug': slug or ('slug-%s' % tid.lower()), 'title': name or ('Title %s' % tid),
            'dialect': dialect, 'language': lang, 'content_type': ctype, 'genres': genres, 'release_date': release,
            'available_from': '2025-01-01T00:00:00.000Z', 'available_until': until, 'is_premium': premium,
            'description': desc}


def default_titles(n=40, dialect='d1'):
    return [title('T%04d' % i, dialect=dialect, genres=['Drama|Comedy', 'Action|Thriller', 'Romance|Family'][i % 3],
                  premium='true' if i % 4 == 0 else 'false') for i in range(1, n + 1)]


class MiniPack:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='oua_test_')
        self.profiles, self.titles, self.history = [], default_titles(), []
        self.triggers, self.live, self.control, self.faults, self.arms = [], [], [], [], {}
        self._replays = []

    def write(self):
        d = self.dir
        for sub in ('data', 'config', 'replay', 'pilot', 'simulator'):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        for f in ('goals.json', 'interventions.json', 'pricing.json'):
            shutil.copy(os.path.join(FIX, 'config', f), os.path.join(d, 'config', f))
        self._csv('data/profiles_snapshot.csv', self.profiles or [profile('U-99999')])
        self._csv('data/catalogue.csv', self.titles)
        eps = []
        for t in self.titles:
            n = 1 if t['content_type'] == 'film' else 6
            for k in range(1, n + 1):
                row = {'episode_id': '%s-s1e%d' % (t['content_slug'], k), 'content_slug': t['content_slug'],
                       'season': 1, 'episode': k, 'minutes': 3}
                if row not in eps:
                    eps.append(row)
        self._csv('data/episodes.csv', eps)
        # a background viewer whose plays make every event type the goals need observable in history
        bg = [ev('U-99999', e, ist(10, day=-5)) for e in ('app_open', 'play_started', 'episode_completed',
                                                          'purchase_completed', 'subscription_lapsed')]
        for e in bg:
            if e['event'] in ('play_started', 'episode_completed'):
                e['properties'] = {'title_id': 'T0001', 'episode_id': 'slug-t0001-s1e1'}
            if e['event'] == 'purchase_completed':
                e['properties'] = {'plan_id': 'P_MONTH', 'price_inr': 149, 'is_trial': False}
        self._jl('data/events.jsonl', bg + self.history)
        self._jl('replay/triggers.jsonl', sorted(self.triggers, key=lambda e: e['deliver_at']))
        self._jl('replay/live_events.jsonl', sorted(self.live, key=lambda e: e['deliver_at']))
        self._jl('replay/control_events.jsonl', self.control)
        self._jl('replay/model_faults.jsonl', self.faults)
        users = {p['user_id'] for p in self.profiles} | {t['user_id'] for t in self.triggers} | {'U-99999'}
        self._csv('replay/arms.csv', [{'user_id': u, 'arm': self.arms.get(u, 'agent')} for u in sorted(users)])
        return d

    def _csv(self, rel, rows):
        with open(os.path.join(self.dir, rel), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def _jl(self, rel, rows):
        with open(os.path.join(self.dir, rel), 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')

    def run(self, cfg_overrides=None):
        self.write()
        cfg = load_config()
        # Tests use the organic baseline fitted on the real pack (aggregate rates only, no viewer data): a tiny
        # synthetic pack has too little history to fit one. Messaging tests send every follow-up unless a test
        # sets the randomised share itself.
        overrides = {'baseline.table': os.path.join(FIX, 'baseline_table.json'), 'messaging.followup_share': 1.0}
        overrides.update(cfg_overrides or {})
        for path, v in overrides.items():
            node = cfg
            keys = path.split('.')
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = v
        out = os.path.join(self.dir, 'run')
        r = Replay(self.dir, out, cfg, log=lambda *a: None).run()
        r.write()
        self._replays.append(r)
        return Result(r, out)

    def cleanup(self):
        for r in self._replays:
            r.ctx.memory.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Result:
    def __init__(self, replay, out):
        self.replay, self.out = replay, out
        with open(os.path.join(out, 'decisions.jsonl'), encoding='utf-8') as f:
            self.decisions = [json.loads(l) for l in f]
        with open(os.path.join(out, 'actions.jsonl'), encoding='utf-8') as f:
            self.actions = [json.loads(l) for l in f]

    def for_user(self, uid, kind='trigger'):
        return [d for d in self.decisions if d['viewer_id'] == uid and (kind is None or d['kind'] == kind)]

    def actions_for(self, uid, types=None):
        return [a for a in self.actions if a['user_id'] == uid and (types is None or a['type'] in types)]

    @property
    def memory(self):
        return self.replay.ctx.memory


def series_viewer(pk, uid, dialect='d1', days_ago=5, status='never', push='true', wa='false', lang='hin',
                  title_id='T0002', signup='2026-01-10T10:00:00Z'):
    """An established viewer who started a series `days_ago` days ago and stopped after episode 1 (G07-eligible)."""
    pk.profiles.append(profile(uid, status=status, dialect=dialect, push=push, wa=wa, lang=lang, signup=signup))
    s = str(uuid.uuid4())
    pk.history += [ev(uid, 'app_open', ist(19, day=-days_ago), dialect=dialect, session=s),
                   ev(uid, 'play_started', ist(19, 1, day=-days_ago),
                      {'title_id': title_id, 'episode_id': 'slug-%s-s1e1' % title_id.lower()}, dialect=dialect,
                      session=s)]


def new_viewer(pk, uid, signed_up_days_ago=1.0, dialect='d1', with_profile=True, push='true', wa='false',
               played=False, completed=False):
    """A brand-new viewer: signup inside the visible history, optionally already played / completed."""
    at = ist(10) - timedelta(days=signed_up_days_ago)
    if with_profile:
        pk.profiles.append(profile(uid, signup=iso(at), dialect=dialect, push=push, wa=wa))
    s = str(uuid.uuid4())
    pk.history.append(ev(uid, 'signup_completed', at, {'method': 'phone'}, dialect=dialect, session=s))
    if played:
        pk.history.append(ev(uid, 'play_started', at + timedelta(minutes=5),
                             {'title_id': 'T0003', 'episode_id': 'slug-t0003-s1e1'}, dialect=dialect, session=s))
    if completed:
        pk.history.append(ev(uid, 'episode_completed', at + timedelta(minutes=9),
                             {'title_id': 'T0003', 'episode_id': 'slug-t0003-s1e1'}, dialect=dialect, session=s))
