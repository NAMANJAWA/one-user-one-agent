"""Goal catalogue with versioned success rules, measurability checks, eligibility and scoring.

Catalogue problems we found and how we handle them (also in KNOWN_LIMITATIONS.md):
  * G10 meaningful_engagement_7d has no success event ("engages meaningfully"): not measurable, never chosen.
  * G11 tv_activation_14d needs `tv_app_open`, which never appears in the event stream or the README schema:
    not measurable, never chosen.
  * G06 says "recently lapsed" without a window. We read it as: a subscription_lapsed event inside the
    visible history (30 days). A profile that says "lapsed" without a visible lapse event has unknown recency.
  * G02 / G01 / G05 say "never ...". "Never" can only be proven when the viewer's whole history is visible, so
    those goals need the viewer to have signed up inside the event window (or a contradicting-free profile).
"""
import re
from dataclasses import dataclass, field
from datetime import timedelta

from .timeutil import iso, ist_day, parse

# Event types the platform can emit (README schema + simulator outputs). A success signal outside this set can
# never be observed, whatever the goal catalogue says.
OBSERVABLE_EVENTS = {
    'signup_completed', 'login_success', 'app_open', 'app_dialect_changed', 'home_viewed', 'title_viewed',
    'search', 'play_started', 'play_progress', 'episode_completed', 'paywall_viewed', 'purchase_completed',
    'subscription_renewed', 'subscription_lapsed', 'message_delivered', 'message_clicked',
    'notification_opt_out', 'home_page_shown', 'message_failed'}

PAID_GOALS = {'G04', 'G05', 'G06'}
STATUS_GOALS = {'G04', 'G05', 'G06', 'G08', 'G09', 'G11'}

# Scope of a goal-rule correction, read from its free-text `applies_to`. An explicit boolean field wins. Wording
# that matches both readings, or neither, is ambiguous: the correction is then applied to new goals only (the
# literal minimum) and flagged in control_impact.json for a human to confirm.
_NEW_ONLY = re.compile(r"\bonly\b[^.;]*\b(new|after)\b|\bkeeps? the (old|previous|original) rule\b|"
                       r"\bnew goals only\b|\bnot (to )?(goals )?already (open|committed)\b", re.IGNORECASE)
_OPEN_TOO = re.compile(r"\bstill open\b|\bincluding (goals|those)\b[^.;]*\bbefore\b|\ball\b[^.;]*\bincluding\b|"
                       r"\b(open|existing|in-flight) goals (too|as well)\b", re.IGNORECASE)


def correction_applies_to_open(control):
    """Returns (applies_to_open_goals, ambiguous)."""
    explicit = control.get('applies_to_open_goals')
    if isinstance(explicit, bool):
        return explicit, False
    text = control.get('applies_to') or ''
    new_only, open_too = bool(_NEW_ONLY.search(text)), bool(_OPEN_TOO.search(text))
    if open_too and not new_only:
        return True, False
    if new_only and not open_too:
        return False, False
    return False, True


@dataclass
class RuleVersion:
    version: str
    success: dict
    effective_from: object
    applies_to_open: bool = False
    note: str = ''
    ambiguous: bool = False


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reason: str
    evidence: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    measurable: bool = True
    actionable: bool = True
    score: float = 0.0
    detail: dict = field(default_factory=dict)

    def to_json(self):
        d = {'goal_id': self.goal_id, 'eligible': self.eligible, 'measurable': self.measurable,
             'reason': self.reason, 'evidence': self.evidence if self.eligible else self.evidence[:1]}
        if self.eligible:
            d['actionable'] = self.actionable
            d['score'] = round(self.score, 3)
            for k in ('p_without_agent', 'movability', 'value', 'baseline', 'learned_multiplier'):
                if k in self.detail:
                    d[k] = self.detail[k]
        if self.assumptions:
            d['assumptions'] = self.assumptions
        return d


class GoalRegistry:
    def __init__(self, goals_json, observed_event_types):
        self.version = goals_json['version']
        self.goals = {g['goal_id']: g for g in goals_json['goals']}
        self.rules = {gid: [RuleVersion('%s@%s' % (gid, self.version), g['success'], None)]
                      for gid, g in self.goals.items()}
        self.observed = set(observed_event_types)
        self.corrections = []

    def name(self, gid):
        return self.goals[gid]['name']

    def window(self, gid):
        return timedelta(hours=self.goals[gid]['success']['window_hours'])

    def current_rule(self, gid, at=None):
        rules = [r for r in self.rules[gid] if r.effective_from is None or at is None or r.effective_from <= at]
        return rules[-1]

    def rule_for_judging(self, gid, goal_started_at):
        """The single definition of which rule judges a goal, used by the agent, the correction handler, the judge
        and the impact analysis: the rule in force when the goal started, unless a later correction that applies
        to open goals arrived while the goal's window was still running (whether or not a success had already
        been observed provisionally; nothing is final until the deadline)."""
        chosen = self.current_rule(gid, goal_started_at)
        deadline = goal_started_at + self.window(gid)
        for r in self.rules[gid]:
            if r.effective_from and r.applies_to_open and goal_started_at < r.effective_from < deadline:
                chosen = r
        return chosen

    def apply_correction(self, control):
        gid = control['goal_id']
        at = parse(control['at'])
        success = dict(self.goals[gid]['success'])
        success.update(control['new_success'])
        to_open, ambiguous = correction_applies_to_open(control)
        rv = RuleVersion('%s@%s' % (gid, control['at']), success, at, applies_to_open=to_open,
                         note=control.get('note', ''), ambiguous=ambiguous)
        self.rules[gid].append(rv)
        self.corrections.append({'goal_id': gid, 'at': control['at'], 'version': rv.version,
                                 'old': control.get('old_success'), 'new': control['new_success'],
                                 'applies_to': control.get('applies_to'), 'applies_to_open_goals': to_open,
                                 'ambiguous_scope': ambiguous})
        return rv

    def measurable(self, gid):
        ev = self.current_rule(gid).success.get('event')
        if not ev:
            return False, 'success condition names no event ("%s")' % self.goals[gid]['success'].get('condition')
        if ev not in OBSERVABLE_EVENTS:
            return False, 'success event %r is not in the platform event schema' % ev
        if ev not in self.observed:
            return False, 'success event %r never appears in the event stream' % ev
        return True, 'success event %r is observable' % ev


class Planner:
    """Deterministic goal planner. It decides which goals a viewer is eligible for, citing evidence, and ranks
    them. The model may only choose among goals this planner marked eligible."""

    def __init__(self, pack, cfg, registry, content, baseline=None):
        self.pack, self.cfg, self.reg, self.content = pack, cfg, registry, content
        self.g = cfg['goals']
        self.baseline = baseline

    def evaluate(self, view):
        out = []
        for gid in sorted(self.reg.goals):
            ok, why = self.reg.measurable(gid)
            if not ok:
                out.append(Eligibility(gid, False, 'not measurable: ' + why, measurable=False))
                continue
            el = getattr(self, '_' + gid.lower())(view)
            if el.eligible:
                el.score = self._score(gid, view, el)
            out.append(el)
        return out

    def rank(self, evaluations):
        return sorted([e for e in evaluations if e.eligible], key=lambda e: (-e.score, e.goal_id))

    # ------------------------------------------------------------------ scoring
    def _score(self, gid, view, el):
        """score = value x movability(p_without_agent) [x 0.5 if the agent cannot act on it in the app's dialect].

        value: how much reaching the goal is worth (config goals.value, a product decision).
        p_without_agent: the organic baseline (baseline.py), from pre-replay data only.
        movability = 4p(1-p): an intervention can only move what is neither already certain nor nearly impossible.
        """
        from .baseline import movability
        value = self.g['value'][gid]
        rule = self.reg.current_rule(gid, view.now)
        p, basis = self.baseline.predict(gid, view, el.detail, rule.success.get('event')) if self.baseline \
            else (None, {'source': 'no baseline'})
        mov = movability(p) if p is not None else 0.5
        s = value * mov
        if not el.actionable:
            s *= self.g['not_actionable_factor']
        learned = (self.cfg.get('_goal_multipliers') or {}).get(gid)
        if learned:
            s *= learned['multiplier']
            el.detail['learned_multiplier'] = learned
        el.detail.update(p_without_agent=p, movability=round(mov, 3), value=value, baseline=basis)
        return s

    # ------------------------------------------------------------------ helpers
    def _signup_days(self, view):
        s = view.value('signup_at')
        if not s:
            return None
        return (view.now - parse(s)).total_seconds() / 86400

    def _status(self, view):
        return view.value('subscription_status')

    def _no(self, gid, reason, evidence=None):
        return Eligibility(gid, False, reason, evidence or [])

    # ------------------------------------------------------------------ goals
    def _g01(self, view):
        d = self._signup_days(view)
        if d is None:
            return self._no('G01', 'signup date unknown')
        if d > 7:
            return self._no('G01', 'signed up %.1f days ago (> 7)' % d, view.facts['signup_at'].refs)
        if view.plays:
            return self._no('G01', 'already started a play', [view.plays[0].ref])
        return Eligibility('G01', True, 'signed up %.1f days ago and no play_started since signup' % d,
                           view.facts['signup_at'].refs + ['derived:no_play_started_since_signup'])

    def _g02(self, view):
        if view.completions:
            return self._no('G02', 'has completed an episode', [view.completions[0].ref])
        if not view.value('full_history'):
            return self._no('G02', 'cannot prove "never completed": ' + (view.facts['full_history'].resolution or ''),
                            view.facts['signup_at'].refs)
        return Eligibility('G02', True, 'no episode_completed since signup (whole history visible)',
                           view.facts['signup_at'].refs + ['derived:no_episode_completed_since_signup'])

    def _g03(self, view):
        d = self._signup_days(view)
        if d is None:
            return self._no('G03', 'signup date unknown')
        if d > 2:
            return self._no('G03', 'signed up %.1f days ago (> 2)' % d, view.facts['signup_at'].refs)
        return Eligibility('G03', True, 'signed up %.1f days ago' % d, view.facts['signup_at'].refs)

    def _g04(self, view):
        st = self._status(view)
        f = view.facts['subscription_status']
        if st != 'trial_active':
            return self._no('G04', 'subscription status is %s' % (st or 'unresolved'), f.refs)
        return Eligibility('G04', True, 'subscription status trial_active (%s)' % f.source, f.refs)

    def _g05(self, view):
        st = self._status(view)
        f = view.facts['subscription_status']
        if st != 'never' or view.value('ever_paid'):
            return self._no('G05', 'subscription status is %s' % (st or 'unresolved'), f.refs)
        return Eligibility('G05', True, 'never paid (%s)' % f.source, f.refs)

    def _g06(self, view):
        st = self._status(view)
        f = view.facts['subscription_status']
        if st != 'lapsed':
            return self._no('G06', 'subscription status is %s' % (st or 'unresolved'), f.refs)
        lapse = view.facts.get('last_lapse_at')
        if not lapse:
            return self._no('G06', 'lapse date unknown: no subscription_lapsed event in the visible history, so '
                            '"recently lapsed" cannot be shown', f.refs)
        days = (view.now - parse(lapse.value)).days
        if days > self.g['recent_lapse_days']:
            return self._no('G06', 'lapsed %d days ago (> %d)' % (days, self.g['recent_lapse_days']), lapse.refs)
        return Eligibility('G06', True, 'lapsed %d days ago' % days, f.refs + lapse.refs,
                           assumptions=['"recently lapsed" read as within %d days' % self.g['recent_lapse_days']])

    def resume_candidates(self, view):
        idle = timedelta(days=self.g['resume_min_idle_days'])
        out = []
        for p in view.progress.values():
            if p['type'] not in ('show', 'micro_drama') or p['max_ep'] >= p['total'] or not p['last_play']:
                continue
            if view.now - p['last_play'] < idle:
                continue
            out.append(p)
        return sorted(out, key=lambda p: p['last_play'], reverse=True)

    def _g07(self, view):
        cands = self.resume_candidates(view)
        if not cands:
            return self._no('G07', 'no started series with episodes left and idle for >= %d days'
                            % self.g['resume_min_idle_days'])
        dialect = view.value('display_dialect')
        shown = [p for p in cands if p['dialect'] == dialect
                 and self.content.pick_variant(p['slug'], view.value('display_language'), view.now) is not None]
        p = (shown or cands)[0]
        el = Eligibility('G07', True, 'stopped %s at episode %d of %d, last played %s' % (
            p['slug'], p['max_ep'], p['total'], iso(p['last_play'])), p['refs'] + ['catalogue:' + p['title_id']])
        el.detail['series'] = {'slug': p['slug'], 'title_id': p['title_id'], 'next_episode': p['max_ep'] + 1}
        if not shown:
            el.actionable = False
            el.detail['why_not_actionable'] = 'series is in %s but the app shows %s' % (p['dialect'], dialect)
        return el

    def _g08(self, view):
        st = self._status(view)
        f = view.facts['subscription_status']
        if st != 'active':
            return self._no('G08', 'subscription status is %s' % (st or 'unresolved'), f.refs)
        el = Eligibility('G08', True, 'active subscriber (%s)' % f.source, f.refs)
        n = len(self.content.new_releases(view.value('display_dialect'), view.now))
        if n < 3:
            el.actionable = False
            el.detail['why_not_actionable'] = '%d recent releases in the display dialect' % n
        return el

    def _g09(self, view):
        st = self._status(view)
        f = view.facts['subscription_status']
        if st != 'active':
            return self._no('G09', 'subscription status is %s' % (st or 'unresolved'), f.refs)
        days = view.value('play_days_14d')
        if days > 1:
            return self._no('G09', 'played on %d days in the last 14 (> 1)' % days, view.facts['play_days_14d'].refs)
        first = view.value('first_seen_at')
        if not first or (view.now - parse(first)).days < self.g['habit_lookback_days']:
            return self._no('G09', 'less than 14 days of visible history; cannot judge a habit')
        return Eligibility('G09', True, 'active subscriber who played on %d day(s) in the last 14' % days,
                           f.refs + view.facts['play_days_14d'].refs)

    def _g10(self, view):
        return self._no('G10', 'not measurable')

    def _g11(self, view):
        return self._no('G11', 'not measurable')

    def _g12(self, view):
        absent = timedelta(days=self.g['absence_days'])
        recent = [e for e in view.prior_opens if view.now - absent <= e.est_at < view.now]
        if recent:
            return self._no('G12', 'opened the app %s (within %d days)' % (iso(recent[-1].est_at),
                                                                         self.g['absence_days']), [recent[-1].ref])
        s = view.value('signup_at')
        if not s or (view.now - parse(s)) < absent:
            return self._no('G12', 'not a returning viewer: signed up less than %d days ago or unknown'
                            % self.g['absence_days'])
        last = view.value('last_open_at')
        return Eligibility('G12', True, 'no app open in the %d days before this trigger (last: %s)' % (
            self.g['absence_days'], last or 'none in visible history'),
            (view.facts['last_open_at'].refs or ['derived:no_app_open_in_window']) + view.facts['signup_at'].refs)


def success_events(pack, gid, success, start, deadline, events, detail=None):
    """Events that satisfy a goal's success rule inside [start, deadline], using estimated (server-anchored)
    event times. Shared by the agent (to notice an already-reached goal) and the outcome judge."""
    ev_type = success.get('event')
    if not ev_type:
        return []
    seen, hits = set(), []
    for e in events:
        if e.event != ev_type or e.event_id in seen or not (start <= e.est_at <= deadline):
            continue
        seen.add(e.event_id)
        hits.append(e)
    if gid in ('G04', 'G05', 'G06'):
        hits = [e for e in hits if not e.props.get('is_trial')]
    elif gid == 'G03':
        hits = [e for e in hits if ist_day(e.est_at) > ist_day(start)]
    elif gid == 'G07':
        slug = ((detail or {}).get('series') or {}).get('slug')
        hits = [e for e in hits if pack.titles.get(e.props.get('title_id')) is not None
                and pack.titles[e.props['title_id']].slug == slug]
    elif gid == 'G08':
        lo = (start - timedelta(days=30)).date()

        def recent(e):
            t = pack.titles.get(e.props.get('title_id'))
            return t is not None and lo <= parse(t.release_date + 'T00:00:00Z').date() <= start.date()
        hits = [e for e in hits if recent(e)]
    elif gid == 'G09':
        days = {}
        for e in sorted(hits, key=lambda x: x.est_at):
            days.setdefault(ist_day(e.est_at), e)
        hits = sorted(days.values(), key=lambda x: x.est_at)[2:3] if len(days) >= 3 else []
    return sorted(hits, key=lambda x: x.est_at)
