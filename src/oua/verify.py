"""Independent pre-publish checks.

This module does NOT use the planner's ViewerView or eligibility code. It re-reads raw profile rows, raw events and
raw catalogue rows and applies its own, deliberately simpler and more conservative predicates. When it disagrees
with the decision step the action is blocked and the disagreement is recorded. Generated text is never accepted as
evidence: evidence references must resolve to real rows in the pack.

A check is listed under `passed` only if it actually ran for that decision.
"""
import re
import unicodedata
from collections import deque
from datetime import timedelta

from .pack import looks_like_instruction
from .timeutil import iso, ist_day, in_quiet_hours, parse

_ZERO_WIDTH = dict.fromkeys(map(ord, '​‌‍⁠﻿­'))
# Amounts with a currency marker before or after them: Rs 149, INR 99, ₹149.50, 149 rupees, 99/-
PRICE = re.compile(r'(?:rs\.?|inr|₹)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:rs\b|rupees?\b|inr\b|'
                   r'₹|/-)')
OFFER = re.compile(r'\d+\s*%\s*off\b|\bdiscount|\bcashback\b|\bcoupon\b|\bpromo code\b')
EPISODE = re.compile(r'(?:season\s*(\d+)\D{0,12})?episode\s*(\d+)')


def normalise(text):
    """Unicode-normalised, zero-width stripped, lower-case, with hyphens/underscores/odd spaces as single spaces."""
    t = unicodedata.normalize('NFKC', text or '').translate(_ZERO_WIDTH).lower()
    t = re.sub(r'[\-_‐-―]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _optout_channels(props):
    raw = normalise(str((props or {}).get('channel') or '')).replace(' ', '')
    if raw in ('push', 'notification', 'notifications', 'pushnotification'):
        return {'push'}
    if raw in ('whatsapp', 'wa'):
        return {'whatsapp'}
    return {'push', 'whatsapp'}      # missing, 'all' or unknown: treat as opting out of every channel


class CheckResult:
    def __init__(self):
        self.passed, self.failed = [], []

    def ok(self, name):
        if name not in self.passed and not any(f['check'] == name for f in self.failed):
            self.passed.append(name)

    def fail(self, name, reason, scope):
        self.failed.append({'check': name, 'reason': reason, 'blocks': scope})
        if name in self.passed:
            self.passed.remove(name)

    def blocks(self, scope):
        return any(f['blocks'] in (scope, 'all') for f in self.failed)

    def to_json(self):
        return {'passed': sorted(self.passed), 'failed': self.failed}


class Verifier:
    def __init__(self, pack, cfg, event_store, content, memory):
        self.pack, self.cfg, self.events, self.content, self.memory = pack, cfg, event_store, content, memory
        iv = pack.interventions
        self.row_types = set(iv['home_page']['rows']['row_types'])
        self.max_rows = iv['home_page']['rows']['max_rows']
        self.min_row = iv['home_page']['rows']['min_titles_per_row']
        self.max_row = iv['home_page']['rows']['max_titles_per_row']
        self.forbidden = [normalise(p) for p in iv['push']['copy']['forbidden_phrases']]
        self.title_max = iv['push']['copy']['title_max_chars']
        self.body_max = iv['push']['copy']['body_max_chars']
        self.templates = {t['template_id']: t for t in iv['whatsapp']['approved_templates']}
        self.prices = {p['price_inr'] for p in pack.pricing['plans']}
        self.trial_days = next((p['days'] for p in pack.pricing['plans'] if p['plan_id'] == 'P_TRIAL'), 3)
        self.coverage_start = parse(cfg['history']['coverage_start'])
        self.observed_types = {e.event for evs in event_store.by_user.values() for e in evs}
        self.reach_log = deque()   # (time, viewer) of published pages, to scale the blast-radius cap

    # ================================================================== raw helpers (independent of facts.py)
    def _raw_events(self, uid, now):
        return [e for e in self.events.events_for(uid) if e.known_at <= now]

    def _signup(self, uid, evs):
        times = [e.est_at for e in evs if e.event == 'signup_completed']
        times += [parse(r['signup_at']) for r in self.pack.profile_rows.get(uid, []) if r.get('signup_at')]
        return min(times) if times else None

    def _status(self, uid, evs, now):
        """Billing events are direct observations and decide when present (a trial counts only for its plan's
        days); otherwise every snapshot row must agree. Anything else is 'unknown', which fails every
        status-dependent goal."""
        bill = sorted((e for e in evs if e.event in ('purchase_completed', 'subscription_renewed',
                                                      'subscription_lapsed')), key=lambda e: e.est_at)
        if bill:
            last = bill[-1]
            if last.event == 'subscription_lapsed':
                return 'lapsed'
            if last.event == 'purchase_completed' and last.props.get('is_trial'):
                if now - last.est_at < timedelta(days=self.trial_days):
                    return 'trial_active'
                paid = [e for e in bill if e.event == 'subscription_renewed' or
                        (e.event == 'purchase_completed' and not e.props.get('is_trial'))]
                return 'lapsed' if paid else 'never'
            return 'active'
        rows = self.pack.profile_rows.get(uid, [])
        vals = {r['subscription_status'] for r in rows}
        if len(vals) == 1:
            return vals.pop()
        if not rows:
            s = self._signup(uid, evs)
            if s is not None and s >= self.coverage_start:
                return 'never'
        return 'unknown'

    def _latest_app_dialect(self, uid, now):
        """The dialect the viewer's app reported most recently (any event known by now)."""
        evs = [e for e in self._raw_events(uid, now) if e.app_dialect_raw is not None or e.event ==
               'app_dialect_changed']
        if not evs:
            return None
        e = max(evs, key=lambda x: x.received_at)
        if e.event == 'app_dialect_changed':
            return e.props.get('to')
        return e.app_dialect

    # ================================================================== goal checks
    def check_goal(self, res, uid, goal, now, trigger):
        gid = goal['goal_id']
        evs = self._raw_events(uid, now)
        status = self._status(uid, evs, now)
        signup = self._signup(uid, evs)
        days = (now - signup).total_seconds() / 86400 if signup else None
        has = lambda name: any(e.event == name for e in evs)
        reason = None
        if gid == 'G01':
            if days is None or days > 7 or has('play_started'):
                reason = 'needs signup <= 7 days and no play_started (signup %.1f d ago, played=%s)' % (
                    days if days is not None else -1, has('play_started'))
        elif gid == 'G02':
            if has('episode_completed') or signup is None or signup < self.coverage_start:
                reason = 'needs no episode_completed and full visible history'
        elif gid == 'G03':
            if days is None or days > 2:
                reason = 'needs signup <= 2 days'
        elif gid == 'G04':
            if status != 'trial_active':
                reason = 'raw status is %s, not trial_active' % status
        elif gid == 'G05':
            if status != 'never':
                reason = 'raw status is %s, not never' % status
        elif gid == 'G06':
            lapses = [e for e in evs if e.event == 'subscription_lapsed']
            win = timedelta(days=self.cfg['goals']['recent_lapse_days'])
            if status != 'lapsed' or not lapses or now - max(e.est_at for e in lapses) > win:
                reason = 'needs a subscription_lapsed event within %d days and no later purchase' % win.days
        elif gid == 'G07':
            series = goal.get('detail', {}).get('series') or {}
            slug = series.get('slug')
            plays = [e for e in evs if e.event == 'play_started'
                     and self.pack.titles.get(e.props.get('title_id')) is not None
                     and self.pack.titles[e.props['title_id']].slug == slug]
            eps = [self.pack.episode_index.get(e.props.get('episode_id')) for e in evs
                   if e.event in ('play_started', 'episode_completed')]
            watched = max([ep[1] for ep in eps if ep and ep[0] == slug] or [0])
            total = len(self.pack.episodes.get(slug, []))
            idle = timedelta(days=self.cfg['goals']['resume_min_idle_days'])
            if not plays or watched >= total or now - max(e.est_at for e in plays) < idle:
                reason = 'series %s: needs a play, unwatched episodes and >= %d idle days' % (slug, idle.days)
        elif gid in ('G08', 'G09'):
            if status != 'active':
                reason = 'raw status is %s, not active' % status
            elif gid == 'G09':
                lb = timedelta(days=self.cfg['goals']['habit_lookback_days'])
                d = {ist_day(e.est_at) for e in evs if e.event == 'play_started' and now - lb <= e.est_at <= now}
                if len(d) > 1:
                    reason = 'played on %d days in the last 14' % len(d)
        elif gid == 'G12':
            win = timedelta(days=self.cfg['goals']['absence_days'])
            sess = trigger.session_id if trigger else None
            recent = [e for e in evs if e.event in ('app_open', 'login_success') and e.session_id != sess
                      and now - win <= e.est_at < now]
            if recent or signup is None or now - signup < win:
                reason = 'needs no app open in %d days and an older signup' % win.days
        else:
            reason = 'goal %s is not allowed (not measurable)' % gid
        if reason:
            res.fail('eligibility', reason, 'all')
        else:
            res.ok('eligibility')
        ev = {g['goal_id']: g for g in self.pack.goals['goals']}[gid]['success'].get('event')
        if not ev or ev not in self.observed_types:
            res.fail('measurable', 'success signal %r is not observable' % ev, 'all')
        else:
            res.ok('measurable')

    def check_evidence(self, res, uid, refs, now):
        bad = []
        resolvable = 0
        for r in refs:
            kind, _, val = r.partition(':')
            if kind == 'event':
                e = self.events.get(val)
                if e is None or e.user_id != uid or e.known_at > now:
                    bad.append(r)
                else:
                    resolvable += 1
            elif kind == 'profile':
                user, _, upd = val.partition('@')
                if user != uid or not any(x['updated_at'] == upd for x in self.pack.profile_rows.get(uid, [])):
                    bad.append(r)
                else:
                    resolvable += 1
            elif kind == 'catalogue':
                if val not in self.pack.titles:
                    bad.append(r)
                else:
                    resolvable += 1
            elif kind in ('goal_rule', 'derived', 'pricing', 'trigger'):
                continue
            else:
                bad.append(r)   # includes model:* and anything unknown
        if bad:
            res.fail('evidence', 'unresolvable or non-data evidence: %s' % bad[:3], 'all')
        elif resolvable == 0:
            res.fail('evidence', 'no evidence reference resolves to a row in the pack', 'all')
        else:
            res.ok('evidence')

    # ================================================================== action checks
    def check_common(self, res, uid, trigger, now):
        if self.pack.arms.get(uid) != 'agent':
            res.fail('holdout', 'viewer is in arm %r; no action of any kind is allowed' % self.pack.arms.get(uid),
                     'all')
        else:
            res.ok('holdout')
        if trigger is not None:
            if self.memory.processed(trigger.event_id):
                res.fail('idempotency', 'trigger %s already handled' % trigger.event_id, 'all')
            else:
                res.ok('idempotency')

    def _title_ok(self, tid, dialect, at, res, scope, label):
        t = self.pack.titles.get(tid)
        if t is None:
            res.fail(label, '%s not in catalogue' % tid, scope)
            return False
        if t.dialect != dialect:
            res.fail('dialect', '%s is %s, page/message is %s' % (tid, t.dialect, dialect), scope)
            return False
        w = self.content.withdrawn.get(tid)
        if not t.available(at) or (w is not None and w <= at):
            res.fail('availability', '%s not available at %s' % (tid, at.isoformat()), scope)
            return False
        if looks_like_instruction(t.description):
            res.fail('content_quarantine', '%s has instruction-like metadata; held for human review' % tid, scope)
            return False
        return True

    def check_page(self, res, page, trigger, publish_at, expected_dialect=None):
        app = trigger.app_dialect if trigger else expected_dialect
        ran = ['dialect', 'availability', 'page_rules', 'content_quarantine']
        if page['dialect'] != app:
            res.fail('dialect', 'page dialect %s but the app is set to %r' % (page['dialect'], app), 'page')
        if trigger is not None:
            ran.append('freshness')
            if trigger.buffered_s > self.cfg['freshness']['stale_trigger_s']:
                res.fail('freshness', 'trigger sat %.0f s in an offline buffer; the viewer is not in session'
                         % trigger.buffered_s, 'page')
        rows = page.get('rows') or []
        if len(rows) > self.max_rows:
            res.fail('page_rules', '%d rows > %d' % (len(rows), self.max_rows), 'page')
        seen = set()
        ids = ([page['hero_title_id']] if page.get('hero_title_id') else []) + [t for r in rows for t in r['title_ids']]
        for r in rows:
            if r['row_type'] not in self.row_types:
                res.fail('page_rules', 'row type %r not allowed' % r['row_type'], 'page')
            if not (self.min_row <= len(r['title_ids']) <= self.max_row):
                res.fail('page_rules', 'row %s has %d titles' % (r['row_type'], len(r['title_ids'])), 'page')
        for tid in ids:
            t = self.pack.titles.get(tid)
            if t and t.slug in seen:
                res.fail('page_rules', 'content %s appears twice' % t.slug, 'page')
            if t:
                seen.add(t.slug)
            self._title_ok(tid, page['dialect'], publish_at, res, 'page', 'catalogue')
        for n in ran:
            res.ok(n)

    def _consent(self, uid, channel, now):
        rows = self.pack.profile_rows.get(uid, [])
        field = 'notifications_opt_in' if channel == 'push' else 'whatsapp_opt_in'
        if not rows or any(r[field] != 'true' for r in rows):
            return False
        latest = max(parse(r['updated_at']) for r in rows)
        for e in self._raw_events(uid, now):
            if e.event == 'notification_opt_out' and channel in _optout_channels(e.props) and e.received_at > latest:
                return False
        return True

    def _copy_problems(self, text):
        """Policy problems in customer-facing text: forbidden phrases, prices that do not exist, offers."""
        norm = normalise(text)
        out = ['forbidden phrase %r' % p for p in self.forbidden if p in norm]
        for m in PRICE.finditer(norm):
            amount = (m.group(1) or m.group(2)).replace(',', '')
            if '.' in amount and float(amount) != int(float(amount)) or int(float(amount)) not in self.prices:
                out.append('price %r is not in pricing.json' % m.group(0))
        if OFFER.search(norm):
            out.append('offer or discount language; no offers exist in pricing.json')
        return out

    def _resume_problems(self, uid, msg, text, now):
        """A message that says 'continue' or names an episode must be about a series the viewer started, and that
        episode must exist."""
        norm = normalise(text)
        if not (EPISODE.search(norm) or 'continue' in norm or 'jaari' in norm):
            return []
        t = self.pack.titles.get(msg['title_id'])
        if t is None or t.content_type == 'film':
            return ['resume wording about %s, which is not a series' % msg['title_id']]
        started = any(e.event == 'play_started' and self.pack.titles.get(e.props.get('title_id')) is not None
                      and self.pack.titles[e.props['title_id']].slug == t.slug for e in self._raw_events(uid, now))
        if not started:
            return ['resume wording about %s, which the viewer never started' % t.slug]
        m = EPISODE.search(norm)
        if m:
            eps = self.pack.episodes.get(t.slug, [])
            season = int(m.group(1)) if m.group(1) else None
            ep = int(m.group(2))
            if not any(e[1] == ep and (season is None or e[0] == season) for e in eps):
                return ['episode %s of %s does not exist' % (m.group(0), t.slug)]
            if season is None and len({e[0] for e in eps}) > 1:
                return ['episode number without a season for multi-season %s' % t.slug]
        return []

    def blast_cap(self, now):
        m = self.cfg['messaging']
        cutoff = now - timedelta(hours=24)
        while self.reach_log and self.reach_log[0][0] < cutoff:
            self.reach_log.popleft()
        reached = len({u for _, u in self.reach_log})
        return max(m['blast_radius_max_per_hour'], int(m.get('blast_radius_share_per_hour', 0.25) * reached))

    def record_reach(self, at, uid):
        self.reach_log.append((at, uid))

    def check_message(self, res, uid, msg, trigger, now, scheduled_for_day):
        ch = msg['type']
        send_at = msg['send_at']
        ran = ['dialect', 'availability', 'content_quarantine']
        if trigger is not None:
            dialect = trigger.app_dialect
        else:
            # At send time the message must still match the dialect the app reports now: a later visit may have
            # switched it, and a title in the old dialect would then be the wrong dialect.
            dialect = msg.get('_dialect')
            latest = self._latest_app_dialect(uid, now)
            if latest != dialect:
                res.fail('dialect', 'message is about a %s title but the app now reports %r' % (dialect, latest),
                         'message')
        lang = trigger.app_language if trigger is not None else msg.get('_language')
        if ch in ('push', 'whatsapp'):
            ran += ['consent', 'quiet_hours', 'frequency_cap', 'blast_radius', 'copy']
            if not self._consent(uid, ch, now):
                res.fail('consent', 'no %s consent in every snapshot row, or a later opt-out' % ch, 'message')
            if in_quiet_hours(send_at):
                res.fail('quiet_hours', 'send_at %s is inside 22:00-08:00 IST' % send_at.isoformat(), 'message')
            day = ist_day(send_at)
            delivered = sum(1 for e in self._raw_events(uid, now) if e.event == 'message_delivered'
                            and ist_day(e.est_at) == day)
            if delivered + scheduled_for_day(uid, day) >= 1:
                res.fail('frequency_cap', 'already 1 push/WhatsApp for %s IST' % day, 'message')
            window = self.memory.count_messages_between(iso(send_at - timedelta(hours=1)), iso(send_at))
            cap = self.blast_cap(now)
            if window >= cap:
                res.fail('blast_radius', '%d messages already scheduled in the hour before %s (cap %d); messaging '
                         'paused' % (window, iso(send_at), cap), 'message')
        self._title_ok(msg['title_id'], dialect, send_at, res, 'message', 'catalogue')
        if ch == 'whatsapp':
            ran.append('template')
            tpl = self.templates.get(msg.get('template_id'))
            if tpl is None:
                res.fail('template', 'template %r is not approved' % msg.get('template_id'), 'message')
            else:
                v = msg.get('variables', {})
                if set(v) != set(tpl['variables']):
                    res.fail('template', 'variables %s != %s' % (sorted(v), tpl['variables']), 'message')
                t = self.pack.titles.get(msg['title_id'])
                if t and 'title_name' in v and v['title_name'] != t.name:
                    res.fail('template', 'title_name is not the catalogue name', 'message')
                if t and 'dialect_name' in v and v['dialect_name'] != t.dialect:
                    res.fail('template', 'dialect_name does not match the title dialect', 'message')
                if lang != tpl['language']:
                    res.fail('template', 'template is %s, viewer app language is %r (unknown fails closed)' % (
                        tpl['language'], lang), 'message')
                text = tpl['text']
                for k, val in v.items():
                    text = text.replace('{{%s}}' % k, str(val))
                for p in self._copy_problems(text):
                    res.fail('copy', p + ' (rendered WhatsApp text)', 'message')
        if ch == 'push':
            text = msg.get('text', {})
            if len(text.get('title', '')) > self.title_max or len(text.get('body', '')) > self.body_max:
                res.fail('copy', 'push copy over length limits', 'message')
            full = text.get('title', '') + ' ' + text.get('body', '')
            for p in self._copy_problems(full) + self._resume_problems(uid, msg, full, now):
                res.fail('copy', p, 'message')
            if msg.get('deeplink') != 'ott://title/%s' % msg['title_id']:
                res.fail('copy', 'deeplink does not match the promoted title', 'message')
        for n in ran:
            res.ok(n)

    def check_budget(self, res, budget):
        if budget.model_calls > budget.max_model or budget.tool_calls > budget.max_tool:
            res.fail('budget', 'call budget exceeded %s' % budget.to_json(), 'all')
        else:
            res.ok('budget')
