"""Assembles what is known about one viewer, with source, time and freshness for every fact, and resolves
conflicts with an explicit rule per fact. No source wins just because it is the largest, newest or nearest.

Precedence rules (also in ARCHITECTURE.md):
  subscription   billing events after the snapshot > snapshot row consistent with billing history >
                 single snapshot row > unresolved (two rows disagree and no billing event decides)
  consent        opt-out event received after the snapshot row was written > snapshot row; no row -> no consent;
                 two rows disagree -> the more restrictive value
  display dialect  the app setting on the trigger, because the app only requests the page for its own dialect.
                 Profile preference and campaign link are recorded as conflicts, never used to pick the page.
  signup         signup_completed event > profile signup_at
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .timeutil import iso, ist_day, parse

BILLING = ('purchase_completed', 'subscription_renewed', 'subscription_lapsed')
DEEPLINK = re.compile(r'^ott://c/([^/]+)/([^/]+)/title/(\w+)$')


@dataclass
class Fact:
    name: str
    value: object
    source: str
    refs: list
    observed_at: object = None
    known_at: object = None
    conflicts: list = field(default_factory=list)
    resolution: str = None

    def to_json(self, now=None):
        d = {'value': self.value, 'source': self.source, 'refs': self.refs,
             'observed_at': iso(self.observed_at), 'known_at': iso(self.known_at)}
        if now is not None and self.known_at is not None:
            d['age_s'] = int((now - self.known_at).total_seconds())
        if self.conflicts:
            d['conflicts'] = self.conflicts
        if self.resolution:
            d['resolution'] = self.resolution
        return d


def profile_ref(row):
    return 'profile:%s@%s' % (row['user_id'], row['updated_at'])


def status_from_event(e):
    if e.event == 'subscription_lapsed':
        return 'lapsed'
    if e.event == 'subscription_renewed':
        return 'active'
    if e.event == 'purchase_completed':
        return 'trial_active' if e.props.get('is_trial') else 'active'
    return None


def derive_status(billing, now, trial_days):
    """Status implied by billing events (sorted by time). A trial only lasts its plan's days: a trial bought weeks
    ago with nothing after it is over, and without a paid purchase the viewer has still never paid. Paid plans end
    with an explicit subscription_lapsed event in this data, so they are not expired by date."""
    last = billing[-1]
    status = status_from_event(last)
    if status == 'trial_active' and last.est_at + timedelta(days=trial_days) <= now:
        paid = any(e.event == 'subscription_renewed' or
                   (e.event == 'purchase_completed' and not e.props.get('is_trial')) for e in billing)
        status = 'lapsed' if paid else 'never'
    return status


# Opt-out channel values are free text in the event. Anything that is not clearly one channel opts out of all.
_CHANNEL_ALIASES = {'push': 'push', 'notification': 'push', 'notifications': 'push', 'push_notification': 'push',
                    'whatsapp': 'whatsapp', 'wa': 'whatsapp'}


def opt_out_channels(props):
    raw = str((props or {}).get('channel') or '').strip().lower().replace(' ', '_').replace('-', '_')
    ch = _CHANNEL_ALIASES.get(raw)
    return {ch} if ch else {'push', 'whatsapp'}


class ViewerView:
    """Read-only picture of one viewer at decision time `now`, built only from data known by `now`."""

    def __init__(self, pack, cfg, user_id, events, profile_rows, now, trigger=None, from_cache=False):
        self.pack, self.cfg, self.user_id, self.now = pack, cfg, user_id, now
        self.events = [e for e in events if e.known_at <= now]
        self.rows = profile_rows
        self.trigger = trigger
        self.from_cache = from_cache
        self.coverage_start = parse(cfg['history']['coverage_start'])
        self.facts = {}
        self.assumptions = []
        self._build()

    # ------------------------------------------------------------------ helpers
    def of(self, *names):
        return [e for e in self.events if e.event in names]

    def _add(self, fact):
        self.facts[fact.name] = fact
        return fact

    def value(self, name):
        f = self.facts.get(name)
        return f.value if f else None

    def trial_days(self):
        return next((p['days'] for p in self.pack.pricing['plans'] if p['plan_id'] == 'P_TRIAL'), 3)

    # ------------------------------------------------------------------ build
    def _build(self):
        self._signup()
        self._subscription()
        self._consent()
        self._dialect()
        self._activity()

    def _signup(self):
        ev = self.of('signup_completed')
        prof = [r for r in self.rows if r.get('signup_at')]
        if ev:
            e = ev[0]
            f = Fact('signup_at', iso(e.est_at), 'event', [e.ref], e.est_at, e.known_at)
            if prof and abs((parse(prof[-1]['signup_at']) - e.est_at).total_seconds()) > 3600:
                f.conflicts.append('profile:%s' % prof[-1]['signup_at'])
                f.resolution = 'signup event is a direct observation; profile copy is a derived export'
            self._add(f)
        elif prof:
            r = prof[-1]
            self._add(Fact('signup_at', r['signup_at'], 'profile', [profile_ref(r)], parse(r['signup_at']),
                           parse(r['snapshot_at'])))
        else:
            self._add(Fact('signup_at', None, 'none', [], resolution='no profile row and no signup event'))
        s = self.value('signup_at')
        full = s is not None and parse(s) >= self.coverage_start
        self._add(Fact('full_history', full, 'derived', ['derived:history_window>=%s' % iso(self.coverage_start)],
                       resolution=None if full else 'history starts %s; "never did X" cannot be proven for this viewer'
                       % iso(self.coverage_start)))

    def _subscription(self):
        billing = self.of(*BILLING)
        rows = self.rows
        snapshot_at = parse(rows[0]['snapshot_at']) if rows else None
        after = [e for e in billing if snapshot_at is None or e.received_at > snapshot_at]
        ever_paid_ev = [e for e in billing if e.event == 'subscription_renewed'
                        or (e.event == 'purchase_completed' and not e.props.get('is_trial'))]
        row_status = sorted({r['subscription_status'] for r in rows})
        conflicts = ['%s:%s' % (profile_ref(r), r['subscription_status']) for r in rows] if len(row_status) > 1 else []

        trial_days = self.trial_days()
        if after:
            e = after[-1]
            f = Fact('subscription_status', derive_status(billing, self.now, trial_days), 'event',
                     [x.ref for x in after], e.est_at, e.known_at)
            if rows and rows[-1]['subscription_status'] != f.value:
                f.conflicts.append('%s:%s' % (profile_ref(rows[-1]), rows[-1]['subscription_status']))
                f.resolution = 'billing event received after the snapshot overrides the day-old snapshot'
        elif billing and rows:
            derived = derive_status(billing, self.now, trial_days)
            match = [r for r in rows if r['subscription_status'] == derived]
            if match:
                r = match[-1]
                f = Fact('subscription_status', derived, 'profile+event', [profile_ref(r), billing[-1].ref],
                         billing[-1].est_at, parse(r['snapshot_at']), conflicts)
                if conflicts:
                    f.resolution = 'kept the snapshot row consistent with the latest billing event'
            else:
                f = Fact('subscription_status', derived, 'event', [billing[-1].ref], billing[-1].est_at,
                         billing[-1].known_at, ['%s:%s' % (profile_ref(r), r['subscription_status']) for r in rows],
                         'snapshot contradicts billing history; billing events are direct observations')
        elif len(row_status) == 1:
            r = rows[-1]
            f = Fact('subscription_status', r['subscription_status'], 'profile', [profile_ref(r)],
                     parse(r['updated_at']), parse(r['snapshot_at']))
        elif len(row_status) > 1:
            f = Fact('subscription_status', None, 'unresolved', [profile_ref(r) for r in rows], None, None, conflicts,
                     'two snapshot rows disagree and no billing event in the visible history decides; '
                     'status-dependent goals are not allowed')
        else:
            # No profile row: a brand-new viewer. "Never paid" is provable only if we can see their whole history.
            if self.value('full_history'):
                f = Fact('subscription_status', 'never', 'derived', ['derived:no_billing_events_since_signup'] +
                         self.facts['signup_at'].refs, None, self.now,
                         resolution='no profile row yet; no purchase since a signup inside the visible history')
            else:
                f = Fact('subscription_status', None, 'none', [], resolution='no profile row and no billing events')
        self._add(f)
        self._add(Fact('ever_paid', bool(ever_paid_ev) or f.value in ('active', 'lapsed'),
                       'event' if ever_paid_ev else f.source, [e.ref for e in ever_paid_ev] or f.refs))
        lapses = self.of('subscription_lapsed')
        if lapses:
            e = lapses[-1]
            self._add(Fact('last_lapse_at', iso(e.est_at), 'event', [e.ref], e.est_at, e.known_at))
        if rows:
            r = rows[-1]
            age_h = (self.now - parse(r['snapshot_at'])).total_seconds() / 3600
            self._add(Fact('profile_age_h', round(age_h, 1), 'profile', [profile_ref(r)], parse(r['snapshot_at']),
                           parse(r['snapshot_at'])))

    def _consent(self):
        opt_outs = self.of('notification_opt_out')
        for channel, field_ in (('push', 'notifications_opt_in'), ('whatsapp', 'whatsapp_opt_in')):
            name = 'consent_' + channel
            if not self.rows:
                self._add(Fact(name, False, 'none', [], resolution='no profile row: consent unknown, treated as no'))
                continue
            vals = {r[field_] for r in self.rows}
            r = self.rows[-1]
            value = vals == {'true'}
            f = Fact(name, value, 'profile', [profile_ref(x) for x in self.rows], parse(r['updated_at']),
                     parse(r['snapshot_at']))
            if len(vals) > 1:
                f.conflicts = ['%s:%s' % (profile_ref(x), x[field_]) for x in self.rows]
                f.resolution = 'snapshot rows disagree; using the more restrictive value'
            later = [e for e in opt_outs if channel in opt_out_channels(e.props)
                     and e.received_at > parse(r['updated_at'])]
            if later and f.value:
                f = Fact(name, False, 'event', [e.ref for e in later], later[-1].est_at, later[-1].known_at,
                         ['%s:true' % profile_ref(r)], 'opt-out event after the snapshot overrides it')
            self._add(f)

    def _dialect(self):
        t = self.trigger
        prof = [r['preferred_dialect'] for r in self.rows if r.get('preferred_dialect')]
        campaign = None
        link = (t.props.get('deeplink') if t else None) or None
        if link:
            m = DEEPLINK.match(link)
            if m:
                campaign = {'campaign': m.group(1), 'dialect': m.group(2), 'title_id': m.group(3), 'ref': t.ref}
        if campaign is None:
            for e in reversed(self.of('app_open')):
                m = DEEPLINK.match(e.props.get('deeplink') or '')
                if m and (self.now - e.est_at) <= timedelta(days=7):
                    campaign = {'campaign': m.group(1), 'dialect': m.group(2), 'title_id': m.group(3), 'ref': e.ref}
                    break
        self.campaign = campaign
        conflicts = []
        if t is not None:
            used = t.app_dialect
            f = Fact('display_dialect', used, 'app_setting', [t.ref], t.est_at, t.known_at)
            if used is None:
                f.resolution = ('app setting reported %r: the dialect the app will request is unknown'
                                % t.app_dialect_raw)
        else:
            last = [e for e in self.events if e.app_dialect]
            used = last[-1].app_dialect if last else None
            f = Fact('display_dialect', used, 'app_setting', [last[-1].ref] if last else [],
                     last[-1].est_at if last else None, last[-1].known_at if last else None)
        for p in sorted(set(prof)):
            if p != used:
                conflicts.append('profile:%s' % p)
        if campaign and campaign['dialect'] != used:
            conflicts.append('campaign:%s' % campaign['dialect'])
        if conflicts:
            f.conflicts = conflicts
            f.resolution = ('the app only requests (and shows) the page for its current dialect setting, so the '
                            'app setting decides what the viewer will see; profile and campaign are kept as '
                            'context and do not pick the page')
        self._add(f)
        self._add(Fact('display_language', t.app_language if t else (self.rows[-1]['preferred_language']
                                                                       if self.rows else None),
                       'app_setting' if t else 'profile', [t.ref] if t else []))
        # Which dialects the viewer actually watches: supporting evidence, shown next to the conflict.
        watched = defaultdict(int)
        for e in self.of('play_started'):
            title = self.pack.titles.get(e.props.get('title_id'))
            if title:
                watched[title.dialect] += 1
        self.watched_dialects = dict(watched)

    def _activity(self):
        now = self.now
        plays = self.of('play_started')
        self.plays = plays
        self.completions = self.of('episode_completed')
        session = self.trigger.session_id if self.trigger else None
        opens = [e for e in self.of('app_open', 'login_success') if e.session_id != session and e.est_at < now]
        self.prior_opens = opens
        self._add(Fact('plays_total', len(plays), 'event', [e.ref for e in plays[-3:]]))
        self._add(Fact('completions_total', len(self.completions), 'event', [e.ref for e in self.completions[-3:]]))
        lb = timedelta(days=self.cfg['goals']['habit_lookback_days'])
        days = sorted({str(ist_day(e.est_at)) for e in plays if now - lb <= e.est_at <= now})
        self._add(Fact('play_days_14d', len(days), 'event', [e.ref for e in plays if now - lb <= e.est_at][-5:]))
        act_days = sorted({str(ist_day(e.est_at)) for e in opens if now - lb <= e.est_at <= now})
        self._add(Fact('active_days_14d', len(act_days), 'event', [e.ref for e in opens if now - lb <= e.est_at][-5:]))
        last_open = max((e.est_at for e in opens), default=None)
        self._add(Fact('last_open_at', iso(last_open), 'event',
                       [max(opens, key=lambda e: e.est_at).ref] if opens else []))
        week = timedelta(days=7)
        paywall = [e for e in self.of('paywall_viewed') if now - week <= e.est_at <= now]
        self._add(Fact('paywall_views_7d', len(paywall), 'event', [e.ref for e in paywall[-3:]]))
        first = min((e.est_at for e in self.events), default=None)
        self._add(Fact('first_seen_at', iso(first), 'event', []))

        # Series progress per content_slug, from the episode ids the viewer started or finished.
        progress = {}
        for e in plays + self.completions:
            ep = self.pack.episode_index.get(e.props.get('episode_id'))
            title = self.pack.titles.get(e.props.get('title_id'))
            if not ep or not title:
                continue
            slug, idx, total = ep
            p = progress.setdefault(slug, {'slug': slug, 'title_id': title.title_id, 'max_ep': 0, 'total': total,
                                           'last_play': None, 'refs': [], 'type': title.content_type,
                                           'dialect': title.dialect})
            p['max_ep'] = max(p['max_ep'], idx)
            if e.event == 'play_started':
                p['last_play'] = max(p['last_play'] or e.est_at, e.est_at)
            p['refs'] = (p['refs'] + [e.ref])[-3:]
        self.progress = progress
        genres = defaultdict(int)
        for e in plays:
            title = self.pack.titles.get(e.props.get('title_id'))
            if title:
                for g in title.genres:
                    genres[g] += 1
        self.genre_affinity = dict(sorted(genres.items(), key=lambda kv: (-kv[1], kv[0])))
        self.message_days = defaultdict(int)
        for e in self.of('message_delivered'):
            self.message_days[str(ist_day(e.est_at))] += 1

    # ------------------------------------------------------------------ export
    def facts_json(self):
        return {k: f.to_json(self.now) for k, f in sorted(self.facts.items())}

    def conflicts(self):
        return {k: {'value': f.value, 'conflicts': f.conflicts, 'resolution': f.resolution}
                for k, f in self.facts.items() if f.conflicts}
