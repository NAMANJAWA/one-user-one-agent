"""The per-viewer agent.

One logical agent per viewer: it owns the viewer's single active goal, the plan for it, and the history of what it
decided, did and observed. All state for a viewer lives in one memory document, written only here, and the harness
serialises work per viewer, so no two components can pursue different goals for the same viewer.

Decision pipeline for a trigger (each step can end in a recorded NO_ACTION):
  1 idempotency (event id, then session)   2 freshness of the trigger   3 read facts (budgeted, with timeout)
  4 holdout -> shadow decision only        5 continue or choose the goal (model only on close calls)
  6 compose page + optional follow-up      7 independent verification   8 publish / queue, record everything
"""
import hashlib
import json
from datetime import timedelta

from .compose import Composer
from .deps import Budget, StoreTimeout
from .facts import ViewerView
from .goals import PAID_GOALS, success_events
from .timeutil import iso, ist_day, parse
from .verify import CheckResult

class Pending:
    """A decision that has been made but not yet stamped with its publish time."""

    def __init__(self, rec, state):
        self.rec, self.state = rec, state
        self.page = None
        self.message = None
        self.goal_instance = None
        self.deps_ms = 0


class Agent:
    def __init__(self, ctx):
        self.ctx = ctx
        self.pack, self.cfg = ctx.pack, ctx.cfg
        self.memory, self.verifier, self.gateway = ctx.memory, ctx.verifier, ctx.gateway
        self.planner, self.registry, self.content = ctx.planner, ctx.registry, ctx.content
        self.composer = Composer(self.pack, self.cfg, self.content, self.planner)
        self.cache = {}   # last successful data-store read per viewer: (rows, events, read_at)

    # ================================================================== helpers
    def describe_ref(self, ref, now):
        kind, _, val = ref.partition(':')
        d = {'ref': ref}
        if kind == 'event':
            e = self.ctx.events.get(val)
            if e:
                d.update(what=e.event, observed_at=iso(e.est_at), known_at=iso(e.known_at),
                         age_s=int((now - e.known_at).total_seconds()))
        elif kind == 'profile':
            uid, _, upd = val.partition('@')
            rows = [r for r in self.pack.profile_rows.get(uid, []) if r['updated_at'] == upd]
            if rows:
                snap = parse(rows[0]['snapshot_at'])
                d.update(what='profile snapshot row', observed_at=upd, known_at=rows[0]['snapshot_at'],
                         age_s=int((now - snap).total_seconds()))
        elif kind == 'catalogue':
            t = self.pack.titles.get(val)
            if t:
                d.update(what='catalogue %s %s %s' % (t.dialect, t.content_type, 'premium' if t.premium else 'free'))
        elif kind == 'goal_rule':
            d.update(what='goal rule version')
        elif kind == 'derived':
            d.update(what='derived from the refs above')
        return d

    def _base(self, kind, uid, arm, now, trig=None):
        did = self.memory.next_id('D')
        rec = {'decision_id': did, 'viewer_id': uid, 'arm': arm, 'kind': kind, 'decided_at': iso(now)}
        if trig is not None:
            rec['trigger'] = {
                'event': trig.event, 'event_id': trig.event_id, 'session_id': trig.session_id,
                'received_at': iso(trig.received_at), 'deliver_at': iso(trig.known_at),
                'estimated_occurred_at': iso(trig.est_at),
                'event_age_s': int((trig.known_at - trig.est_at).total_seconds()),
                'buffered_s': int(trig.buffered_s), 'clock_skew_s': int(trig.clock_skew_s),
                'app_dialect_raw': trig.app_dialect_raw, 'app_language': trig.app_language}
        return rec

    def _scheduled_for_day(self, uid, day):
        n = 0
        for a, status in self.memory.actions_for(uid):
            if a['type'] in ('push', 'whatsapp') and status in ('queued', 'sent') and a.get('send_at') \
                    and ist_day(parse(a['send_at'])) == day:
                n += 1
        return n

    def _live_page(self, uid, now):
        for a, status in reversed(self.memory.actions_for(uid)):
            if a['type'] == 'home_page' and status == 'live' and parse(a['_expires_at']) > now:
                return a
        return None

    # ================================================================== reading facts
    def _read(self, uid, now, budget, key):
        timeout = self.cfg['latency']['store_timeout_ms']
        deps = 0
        try:
            rows, lat1 = self.ctx.datastore.read('profile', uid, now, timeout, budget, key)
            deps += lat1
            events, lat2 = self.ctx.datastore.read('events', uid, now, timeout, budget, key)
            deps += lat2
            self.cache[uid] = (rows, events, now)
            return rows, events, deps, None
        except StoreTimeout as e:
            deps += e.latency_ms
            cached = self.cache.get(uid)
            if cached:
                rows, events, read_at = cached
                # Events pushed to the agent since the cached read are already appended by on_event.
                return rows, events, deps, {'cache_read_at': iso(read_at), 'reason': str(e)}
            return None, None, deps, {'cache_read_at': None, 'reason': str(e)}

    # ================================================================== trigger
    def decide(self, trig, now, queue_ms):
        uid = trig.user_id
        arm = self.pack.arms.get(uid, 'unassigned')
        shadow = arm != 'agent'
        state = self.memory.load(uid, arm)
        p = Pending(self._base('trigger', uid, arm, now, trig), state)
        rec = p.rec
        budget = Budget(self.cfg['budgets']['max_model_calls'], self.cfg['budgets']['max_tool_calls'])
        p.budget = budget

        prev = self.memory.processed(trig.event_id)
        if prev:
            return self._no_action(p, 'DUPLICATE_IGNORED', 'trigger event %s was already handled by %s; a '
                                   'redelivered event never causes a second action' % (trig.event_id, prev),
                                   mark=False)
        stale_s = self.cfg['freshness']['stale_trigger_s']
        if trig.buffered_s > stale_s:
            return self._no_action(p, 'NO_ACTION', 'stale trigger: the app open happened about %s ago and sat in '
                                   'the phone\'s offline buffer; the viewer is not in this session, so a page would '
                                   'never be seen. Activity recorded, nothing published.' % _human(trig.buffered_s))
        if not shadow and trig.app_dialect:
            # The app now reports this dialect; follow-ups queued about titles in another dialect would arrive in
            # the wrong dialect.
            moved = self._cancel_queued(uid, now, lambda a: a.get('_dialect') != trig.app_dialect,
                                        'a later visit shows the app in %s; queued message was about a title in '
                                        'another dialect' % trig.app_dialect)
            if moved:
                rec['cancelled_actions'] = moved
        acted = state['sessions_acted'].get(trig.session_id)
        if acted and not shadow:
            live = self._live_page(uid, now)
            if live and live['dialect'] == trig.app_dialect:
                return self._no_action(p, 'NO_ACTION', 'already acted in this session (%s); page %s is live until '
                                       '%s in %s' % (acted, live['action_id'], live['_expires_at'], live['dialect']))

        rows, events, deps, degraded = self._read(uid, now, budget, trig.event_id)
        p.deps_ms += deps
        if events is None:
            return self._degraded_fallback(p, trig, now, degraded, shadow)
        view = ViewerView(self.pack, self.cfg, uid, events, rows, now, trigger=trig, from_cache=bool(degraded))
        p.view = view
        if shadow:
            # Measurement-only record: keep the facts that decide the goal, not the full snapshot.
            rec['facts'] = {k: v for k, v in view.facts_json().items()
                            if k in ('subscription_status', 'display_dialect', 'signup_at', 'consent_push')}
        else:
            rec['facts'] = view.facts_json()
        rec['dialect'] = self._dialect_block(view)
        if degraded:
            rec['degraded'] = degraded

        goal, selection = self._goal(p, view, now, queue_ms, shadow, degraded)
        rec['goal_selection'] = selection
        if goal is None:
            return self._no_action(p, 'SHADOW_NO_ACTION' if shadow else 'NO_ACTION', selection['why_no_goal'])
        return self._act(p, view, goal, now, shadow, degraded)

    def _dialect_block(self, view):
        f = view.facts['display_dialect']
        d = {'used': f.value, 'source': f.source, 'conflicts': f.conflicts}
        if f.resolution:
            d['resolution'] = f.resolution
        if view.watched_dialects:
            d['supporting'] = {'plays_by_title_dialect': view.watched_dialects}
        if view.campaign:
            d['campaign'] = view.campaign
        return d

    # ================================================================== goal
    def _goal(self, p, view, now, queue_ms, shadow, degraded):
        state = p.state
        key = 'shadow_goal' if shadow else 'active_goal'
        g = state.get(key)
        closed_note = None
        if g and g.get('status') == 'open' and parse(g['deadline']) > now:
            rule = self.registry.rule_for_judging(g['goal_id'], parse(g['started_at']))
            hits = success_events(self.pack, g['goal_id'], rule.success, parse(g['started_at']),
                                  parse(g['deadline']), view.events, g.get('detail'))
            if not hits:
                sel = {'mode': 'continuing', 'goal_instance_id': g['instance_id'],
                       'why': 'one active goal per viewer: %s committed at %s, open until %s' % (
                           g['goal_id'], g['started_at'], g['deadline'])}
                return dict(g, committed='continuing'), sel
            g['status'] = 'closed_success_observed'
            g['closed_at'] = iso(now)
            g['close_note'] = 'success signal %s seen before this visit; final judgment at the deadline' % hits[0].ref
            self.memory.put_goal(g)
            closed_note = 'previous goal %s %s reached (%s); choosing the next goal' % (
                g['instance_id'], g['goal_id'], hits[0].ref)
        evals = self.planner.evaluate(view)
        adj = state.get('goal_adjustments') or {}
        for e in evals:
            if e.eligible and adj.get(e.goal_id):
                e.score = max(0.0, e.score + adj[e.goal_id])
                e.detail['learned_adjustment'] = adj[e.goal_id]
        ranked = self.planner.rank(evals)
        sel = {'mode': 'new', 'considered': [e.to_json() for e in evals]}
        if adj:
            sel['learned_adjustments'] = adj
        if closed_note:
            sel['previous_goal'] = closed_note
        if not ranked:
            sel['why_no_goal'] = 'no measurable goal the viewer is eligible for: ' + '; '.join(
                '%s %s' % (e.goal_id, e.reason) for e in evals if e.measurable)[:900]
            return None, sel
        det = ranked[0]
        floor = self.cfg['goals']['min_expected_value']
        if det.score < floor:
            # A deliberate abstention, not a mechanical one: the viewer is eligible, but no goal scores enough to be
            # worth an intervention. Movability 4p(1-p) assumes sure things and near-impossible goals barely move;
            # that is a modelling choice, so the goal left alone is tracked (finalize) to test its prediction.
            d = det.detail
            sel['abstain'] = 'low_expected_value'
            sel['abstained_goal'] = {'goal_id': det.goal_id, 'p_without_agent': d.get('p_without_agent'),
                                     'bucket': (d.get('baseline') or {}).get('bucket'), 'score': round(det.score, 3),
                                     'detail': {k: d[k] for k in ('series',) if k in d}}
            sel['why_no_goal'] = (
                'not worth an intervention: the best goal is %s with value %.1f x movability %.2f = %.3f < %.2f '
                '(it happens without the agent with p=%s, %s). Leaving the default page.' % (
                    det.goal_id, d.get('value', 0), d.get('movability', 0), det.score, floor,
                    d.get('p_without_agent'), (d.get('baseline') or {}).get('source')))
            return None, sel
        sel['deterministic_choice'] = det.goal_id
        chosen = det
        # Model usage (config model.mode):
        #   log_only (default): the model is consulted on every new goal choice that has a real alternative, off the
        #     critical path. Its answer is validated and the verdict recorded (agree / disagree / rejected), but the
        #     agent always acts on the planner's choice and the call adds no latency to the page. Logging alone cannot
        #     show the model adds value; see KNOWN_LIMITATIONS.md.
        #   advise: the model's validated choice is followed and its latency is on the page path (shed under load).
        mode = self.cfg['model'].get('mode', 'log_only')
        if shadow:
            sel['model'] = {'called': False, 'outcome': 'not_called_holdout_shadow'}
        elif len(ranked) < 2:
            sel['model'] = {'called': False, 'outcome': 'not_called_single_candidate',
                            'why': 'only one eligible goal; there is no choice for the model to inform'}
        elif degraded:
            sel['model'] = {'called': False, 'outcome': 'not_called_degraded_data'}
        elif mode == 'advise' and queue_ms > self.cfg['latency']['shed_model_when_queue_ms']:
            self.gateway.stats['skipped_load'] += 1
            sel['model'] = {'called': False, 'outcome': 'skipped_load_shedding', 'queue_ms': queue_ms}
        else:
            request, aliases = self._model_request(view, ranked[:4])
            m = self.gateway.choose_goal(request, aliases, p.rec['trigger']['event_id'], now, p.budget)
            m['mode'] = mode
            sel['model'] = m
            valid = m.get('outcome') == 'accepted'
            if valid:
                m['agrees_with_planner'] = m['goal_id'] == det.goal_id
            if mode == 'advise':
                p.deps_ms += m.get('latency_ms', 0)
                if valid:
                    chosen = next(e for e in ranked if e.goal_id == m['goal_id'])
            else:
                p.shadow_model_ms = m.get('latency_ms', 0)
                m['followed'] = False
        sel['chosen'] = chosen.goal_id
        sel['why_not_others'] = [{'goal_id': e.goal_id, 'score': round(e.score, 3),
                                  'why': ('lower score than %s' % chosen.goal_id) if e.score <= chosen.score else
                                  ('higher score, but the model preferred %s citing evidence it was given'
                                   % chosen.goal_id)} for e in ranked if e is not chosen]
        goal = {'goal_id': chosen.goal_id, 'name': self.registry.name(chosen.goal_id), 'evidence': chosen.evidence,
                'eligibility_reason': chosen.reason, 'assumptions': chosen.assumptions, 'detail': chosen.detail,
                'actionable': chosen.actionable, 'score': round(chosen.score, 3),
                'deterministic_goal_id': det.goal_id, 'deterministic_detail': det.detail,
                'model_override': chosen is not det, 'committed': 'new', 'ranked': ranked}
        return goal, sel

    def _model_request(self, view, ranked):
        """Aliased, derived features only. No viewer id, no event ids, no free text from the data."""
        aliases, rev = {}, {}

        def alias(ref):
            if ref not in rev:
                a = 'R%d' % (len(rev) + 1)
                rev[ref], aliases[a] = a, ref
            return rev[ref]
        cands = [{'goal_id': e.goal_id, 'name': self.registry.name(e.goal_id), 'score': round(e.score, 3),
                  'evidence': [alias(r) for r in e.evidence if not r.startswith('derived:')][:4]} for e in ranked]
        tv = view.of('title_viewed')
        played = {e.props.get('title_id') for e in view.plays}
        tv_np = [e for e in tv if e.props.get('title_id') not in played]
        signals = {
            'paywall_views_7d': view.value('paywall_views_7d') or 0,
            'paywall_refs': [alias(r) for r in view.facts['paywall_views_7d'].refs],
            'title_views_without_play': len(tv_np),
            'title_view_refs': [alias(e.ref) for e in tv_np[-2:]],
            'play_days_14d': view.value('play_days_14d'),
            'campaign_link': alias(view.campaign['ref']) if view.campaign else None,
            'cold_start': (view.value('plays_total') or 0) == 0,
        }
        return {'task': 'choose_goal', 'candidates': cands, 'signals': signals}, aliases

    # ================================================================== act
    def _act(self, p, view, goal, now, shadow, degraded):
        rec, state = p.rec, p.state
        uid = view.user_id
        if goal['committed'] == 'continuing' and not shadow:
            live = self._live_page(uid, now)
            if live and live['dialect'] == view.value('display_dialect') and \
                    live.get('_goal_instance_id') == goal.get('instance_id'):
                rec['goal'] = self._goal_block(goal, None, None)
                return self._no_action(p, 'NO_ACTION', 'page %s for the same goal is still live until %s in %s and '
                                       'nothing about the viewer has changed; republishing it would add nothing'
                                       % (live['action_id'], live['_expires_at'], live['dialect']))
        publish_est = now + timedelta(milliseconds=p.deps_ms)
        page, why_page = self.composer.home_page(view, goal, publish_est)
        res = CheckResult()
        if not shadow:
            self.verifier.check_common(res, uid, view.trigger, now)
        self._check_goal(res, uid, goal, now, view.trigger)
        # The verifier disagreed with the planner on the goal: try the next eligible goal once.
        if res.blocks('all') and goal['committed'] == 'new' and any(f['check'] == 'eligibility' for f in res.failed):
            rec.setdefault('verifier_disagreements', []).append({'goal_id': goal['goal_id'], 'failed': res.failed})
            alt = [e for e in goal['ranked'] if e.goal_id != goal['goal_id']]
            if alt:
                e = alt[0]
                goal = dict(goal, goal_id=e.goal_id, name=self.registry.name(e.goal_id), evidence=e.evidence,
                            eligibility_reason=e.reason, assumptions=e.assumptions, detail=e.detail,
                            actionable=e.actionable, score=round(e.score, 3))
                rec['goal_selection']['chosen_after_verifier'] = e.goal_id
                page, why_page = self.composer.home_page(view, goal, publish_est)
                res = CheckResult()
                if not shadow:
                    self.verifier.check_common(res, uid, view.trigger, now)
                self._check_goal(res, uid, goal, now, view.trigger)
        if res.blocks('all'):
            rec['checks'] = res.to_json()
            return self._no_action(p, 'SHADOW_NO_ACTION' if shadow else 'BLOCKED',
                                   'independent checks blocked the decision: ' +
                                   '; '.join('%s: %s' % (f['check'], f['reason']) for f in res.failed))
        minimal = False
        if page is not None:
            self.verifier.check_page(res, page, view.trigger, publish_est)
            if res.blocks('page'):
                # Smaller safe action: one popular row, re-checked from scratch.
                page2, _ = self.composer.home_page(view, goal, publish_est, minimal=True)
                res2 = CheckResult()
                if page2 is not None:
                    self.verifier.check_page(res2, page2, view.trigger, publish_est)
                rec.setdefault('fallbacks', []).append({'from': 'full_page', 'failed': res.failed})
                res = CheckResult()
                if not shadow:
                    self.verifier.check_common(res, uid, view.trigger, now)
                self._check_goal(res, uid, goal, now, view.trigger)
                if page2 is not None and not res2.blocks('page'):
                    page, minimal = page2, True
                    res.passed += res2.passed
                else:
                    page, why_page = None, 'page failed checks: ' + '; '.join(f['reason'] for f in res2.failed[:2])
        if page is None:
            rec['checks'] = res.to_json()
            rec['goal'] = self._goal_block(goal, None, None)
            rec['goal']['committed'] = 'not_committed' if goal['committed'] == 'new' else 'continuing'
            return self._no_action(p, 'SHADOW_NO_ACTION' if shadow else 'NO_ACTION',
                                   'cannot show anything useful: %s. Goal %s not committed; nothing published.'
                                   % (why_page, goal['goal_id']))
        msg, why_msg = None, None
        conf, conf_reason = self._confidence(view, goal, degraded, minimal, rec)
        if not self.cfg['messaging']['enabled']:
            why_msg = 'messaging disabled in config'
        elif goal['committed'] != 'new':
            why_msg = 'continuing goal: page refreshed only; at most one follow-up message per goal'
        elif degraded:
            why_msg = 'facts come from a cache while the data store is slow; messages need fresh consent and status'
        elif conf < 0.4:
            why_msg = 'confidence %.2f < 0.40; a message is a bigger intervention than a page' % conf
        elif minimal:
            why_msg = 'only a minimal page could be built'
        elif (view.value('active_days_14d') or 0) > self.cfg['messaging']['max_active_days_14d_for_followup']:
            why_msg = ('viewer opened the app on %d of the last 14 days; a habitual viewer will see the page on '
                       'their own, so a message would be disproportionate' % view.value('active_days_14d'))
        elif goal.get('score', 0) < self.cfg['messaging']['min_score_for_followup']:
            why_msg = ('goal score %.2f < %.2f: the goal is too unlikely to move for a message to be proportionate'
                       % (goal.get('score', 0), self.cfg['messaging']['min_score_for_followup']))
        else:
            suppressed = {ch for ch, st in (state.get('channel_state') or {}).items() if st.get('suppressed')}
            msg, why_msg = self.composer.message(view, goal, page, now, suppressed)
            if msg:
                msg['_dialect'] = page['dialect']
                self.verifier.check_message(res, uid, msg, view.trigger, now, self._scheduled_for_day)
                if res.blocks('message'):
                    why_msg = 'message dropped by checks: ' + '; '.join(
                        f['reason'] for f in res.failed if f['blocks'] == 'message')
                    msg = None
            if msg and not shadow:
                # Randomised follow-up experiment: whether follow-ups add anything beyond the page is unknown, so
                # each eligible viewer is assigned send/withhold at random (stable per viewer) and the effect is
                # measured in impact_report.md.
                share = self.cfg['messaging']['followup_share']
                send = _h01('followup', uid) < share
                rec['followup_experiment'] = {'eligible': True, 'assigned': 'send' if send else 'withhold',
                                              'p_send': share}
                if not send:
                    why_msg = ('withheld at random (p_send=%.2f) to measure whether follow-ups help' % share)
                    msg = None
        self.verifier.check_budget(res, p.budget)
        if res.blocks('all'):
            rec['checks'] = res.to_json()
            return self._no_action(p, 'BLOCKED', 'budget check failed')
        p.page, p.message = page, msg
        rec['checks'] = res.to_json()
        rec['confidence'] = conf
        rec['confidence_reason'] = conf_reason
        rec['goal'] = self._goal_block(goal, None, None)
        p.goal = goal
        rec['action'] = 'HOME_PAGE' + ('+' + msg['type'].upper() if msg else '')
        rec['content'] = ['title:' + t for t in ([page['hero_title_id']] if page.get('hero_title_id') else []) +
                          [t for r in page['rows'] for t in r['title_ids']]]
        if minimal:
            rec['fallbacks'] = rec.get('fallbacks', []) + [{'to': 'minimal_page'}]
        rec['why_not'] = {'message': why_msg} if why_msg else {}
        ev = list(dict.fromkeys(goal['evidence'] + view.facts['display_dialect'].refs +
                                ['catalogue:' + t for t in ([page['hero_title_id']] if page.get('hero_title_id')
                                                            else [])] +
                                ['goal_rule:' + self.registry.current_rule(goal['goal_id'], now).version]))
        rec['evidence'] = [self.describe_ref(r, now) for r in ev]
        rec['assumptions'] = self._assumptions(view, goal, page)
        det = goal.get('detail') or {}
        rec['p_without_agent'] = det.get('p_without_agent')
        rec['p_without_agent_basis'] = det.get('baseline')
        rec['goal_score'] = {'value': det.get('value'), 'movability': det.get('movability'),
                             'score': goal.get('score')}
        if shadow:
            rec['status'] = 'SHADOW_NO_ACTION'
            rec['no_action_reason'] = ('%s arm: the agent records what it would have done (for measurement) and '
                                       'takes no action of any kind' % rec['arm'])
            rec['shadow'] = {'would_publish': rec['action'], 'goal_id': goal['goal_id']}
            rec['action'] = 'NO_ACTION'
        else:
            rec['status'] = 'PUBLISHED'
        return p

    def _check_goal(self, res, uid, goal, now, trigger):
        if goal['committed'] == 'new':
            self.verifier.check_goal(res, uid, goal, now, trigger)
        else:
            # Verified when the goal was committed; re-checking now would punish success. Recorded as carried, not
            # as a check that ran.
            res.ok('eligibility_carried_from_%s' % goal.get('decision_id', 'commit'))
        self.verifier.check_evidence(res, uid, goal['evidence'], now)

    def _goal_block(self, goal, started, deadline):
        start = started or goal.get('started_at')
        # The rule that will judge this goal: the one in force at commit, or a later correction that applies to
        # goals still open, so the success signal shown is the one the outcome judge will use.
        rule = self.registry.rule_for_judging(goal['goal_id'], parse(start)) if start else \
            self.registry.current_rule(goal['goal_id'])
        return {'id': goal['goal_id'], 'name': goal['name'], 'committed': goal['committed'],
                'instance_id': goal.get('instance_id'), 'started_at': start,
                'deadline': deadline or goal.get('deadline'), 'rule_version': rule.version,
                'rule_version_at_commit': goal.get('rule_version', rule.version),
                'success_signal': {'event': rule.success.get('event'), 'condition': rule.success.get('condition'),
                                   'window_hours': rule.success.get('window_hours')},
                'eligibility': goal.get('eligibility_reason'),
                'deterministic_goal_id': goal.get('deterministic_goal_id')}

    def _confidence(self, view, goal, degraded, minimal, rec):
        c, why = 0.80, []
        if degraded:
            c -= 0.25
            why.append('facts from a cache because the data store is slow')
        n_conf = len(view.facts['display_dialect'].conflicts)
        if n_conf:
            c -= min(0.2, 0.1 * n_conf)
            why.append('app setting disagrees with %s (app setting decides what is shown)' %
                       ', '.join(view.facts['display_dialect'].conflicts))
        sub = view.facts['subscription_status']
        if sub.conflicts:
            c -= 0.1
            why.append('subscription sources disagreed (%s)' % sub.resolution)
        if (view.value('plays_total') or 0) == 0 and not view.rows:
            c -= 0.15
            why.append('brand-new viewer with almost no history')
        age = view.value('profile_age_h')
        if age is not None and age > self.cfg['freshness']['profile_max_age_h'] and goal['goal_id'] in PAID_GOALS:
            c -= 0.1
            why.append('profile snapshot is %.0f h old and the goal depends on it' % age)
        if not goal.get('actionable', True):
            c -= 0.2
            why.append('goal is only partly actionable: %s' % goal['detail'].get('why_not_actionable'))
        if minimal:
            c -= 0.1
            why.append('only a minimal page could be built')
        m = rec.get('goal_selection', {}).get('model', {})
        if m.get('mode') == 'advise' and (str(m.get('outcome', '')).startswith('rejected')
                                          or str(m.get('outcome', '')).startswith('error')):
            c -= 0.05
            why.append('model advice was unusable (%s); deterministic choice used' % m['outcome'])
        if goal.get('model_override'):
            c -= 0.1
            why.append('the model overrode the planner (%s -> %s); its reason text is unverified' % (
                goal.get('deterministic_goal_id'), goal['goal_id']))
        wd = view.watched_dialects
        d = view.value('display_dialect')
        if wd and d in wd and wd[d] == max(wd.values()):
            c += 0.05
            why.append('most past plays are in the display dialect')
        if not why:
            why.append('fresh trigger, consistent sources, goal eligibility shown by events/profile')
        return round(max(0.05, min(0.95, c)), 2), '; '.join(why)

    def _assumptions(self, view, goal, page):
        a = list(goal.get('assumptions') or [])
        a.append('the app requests the home page for its current dialect setting (%s)' % page['dialect'])
        if not view.value('full_history'):
            a.append('history before %s is not visible; "never" claims are not made for this viewer'
                     % self.cfg['history']['coverage_start'][:10])
        if view.facts['subscription_status'].source == 'profile':
            a.append('subscription status from a snapshot taken %s h before this decision; no billing event since'
                     % view.value('profile_age_h'))
        if goal['goal_id'] == 'G07':
            a.append('viewer has not watched episode %s of %s elsewhere' % (
                goal['detail']['series']['next_episode'], goal['detail']['series']['slug']))
        return a

    # ================================================================== degraded / no action
    def _degraded_fallback(self, p, trig, now, degraded, shadow):
        """Data store unreachable and nothing cached. The trigger itself still tells us the app's dialect, so the
        smallest safe action is a non-personalised popular-in-dialect page with no goal and no message."""
        rec = p.rec
        rec['degraded'] = degraded
        if shadow:
            return self._no_action(p, 'SHADOW_NO_ACTION', 'holdout arm (data store unavailable)')
        d = trig.app_dialect
        if not d or not self.content.has_content(d):
            return self._no_action(p, 'NO_ACTION', 'data store timed out, nothing cached, and the app dialect %r '
                                   'has no content: no safe action' % trig.app_dialect_raw)
        publish_est = now + timedelta(milliseconds=p.deps_ms)
        lang = trig.app_language
        titles = [t for t in (self.content.pick_variant(s, lang, publish_est)
                              for s in self.content.ranked_slugs(d, publish_est)) if t][:10]
        page = {'dialect': d, 'hero_title_id': None,
                'rows': [{'row_type': 'popular_in_dialect', 'title_ids': [t.title_id for t in titles]}]}
        res = CheckResult()
        self.verifier.check_common(res, trig.user_id, trig, now)
        self.verifier.check_page(res, page, trig, publish_est)
        rec['checks'] = res.to_json()
        if res.blocks('page') or res.blocks('all') or len(titles) < 3:
            return self._no_action(p, 'NO_ACTION', 'data store timed out and the fallback page failed checks')
        p.page = page
        p.goal = None
        rec['status'] = 'PUBLISHED_FALLBACK'
        rec['action'] = 'HOME_PAGE'
        rec['goal'] = None
        rec['no_goal_reason'] = 'data store timed out (%s) and no cached facts: eligibility cannot be shown, so ' \
                                'no goal is committed' % degraded['reason']
        rec['content'] = ['title:' + t for t in page['rows'][0]['title_ids']]
        rec['confidence'] = 0.3
        rec['confidence_reason'] = 'no viewer facts; only the trigger\'s app dialect and catalogue popularity'
        rec['evidence'] = [self.describe_ref('event:' + trig.event_id, now)]
        rec['assumptions'] = ['popular titles in the app dialect are a safe default when nothing is known']
        return p

    def _no_action(self, p, status, reason, mark=True):
        p.rec['status'] = status
        p.rec['action'] = 'NO_ACTION'
        p.rec['no_action_reason'] = reason
        p.no_mark = not mark
        return p

    # ================================================================== finalize
    def finalize(self, p, published_at, timing):
        """Stamps the publish time, commits the goal, writes actions and memory. Returns published actions."""
        rec, state = p.rec, p.state
        uid = rec['viewer_id']
        self._recheck_at_publish(p, published_at)
        rec['published_at'] = iso(published_at)
        rec['latency'] = timing
        rec['decided_in_ms'] = timing['decided_in_ms']
        rec['budget'] = p.budget.to_json() if hasattr(p, 'budget') else None
        actions = []
        trig = rec.get('trigger')
        shadow_goal = rec.get('status') == 'SHADOW_NO_ACTION' and rec.get('shadow')
        goal = getattr(p, 'goal', None)
        if goal and (rec.get('status') == 'PUBLISHED' or shadow_goal):
            if goal['committed'] == 'new':
                gi = self._commit_goal(rec, state, goal, published_at, shadow=bool(shadow_goal))
            else:
                gi = state['shadow_goal' if shadow_goal else 'active_goal']
            rec['goal'] = self._goal_block(gi, gi['started_at'], gi['deadline'])
            rec['goal']['instance_id'] = gi['instance_id']
            rec['goal']['committed'] = goal['committed']
            rec['judged_by'] = {'deadline': gi['deadline'], 'signal': rec['goal']['success_signal'],
                                'rule_version': rec['goal']['rule_version']}
        ab = (rec.get('goal_selection') or {}).get('abstained_goal')
        if ab and rec.get('status') in ('NO_ACTION', 'SHADOW_NO_ACTION'):
            self._track_abstention(rec, state, ab, published_at)
        if rec.get('status') in ('PUBLISHED', 'PUBLISHED_FALLBACK') and p.page:
            a = self._page_action(rec, p.page, published_at)
            actions.append(a)
            self.verifier.record_reach(published_at, uid)
            if trig:
                state['sessions_acted'][trig['session_id']] = rec['decision_id']
        if rec.get('status') == 'PUBLISHED' and p.message:
            m = self._message_action(rec, p.message, published_at)
            actions.append(m)
            state['plan'] = dict(state.get('plan') or {}, message_action_id=m['action_id'])
        rec['actions'] = [a['action_id'] for a in actions]
        if trig and not getattr(p, 'no_mark', False):
            self.memory.mark_processed(trig['event_id'], uid, trig['session_id'], rec['decision_id'], published_at)
        state['decisions'].append(rec['decision_id'])
        if trig and (not state.get('last_trigger_at') or trig['estimated_occurred_at'] > state['last_trigger_at']):
            state['last_trigger_at'] = trig['estimated_occurred_at']
        if rec.get('facts'):
            state['facts'] = rec['facts']
        self.memory.journal(published_at, uid, 'decision', rec['decision_id'], rec)
        for a in actions:
            self.memory.journal(published_at, uid, 'action_' + ('queued' if a['type'] in ('push', 'whatsapp')
                                                                else 'published'), a['action_id'], a)
        self.memory.save(state, published_at)
        return [a for a in actions if a['type'] == 'home_page']

    def _recheck_at_publish(self, p, published_at):
        """The decision was composed at its start time; the harness pre-applies catalogue withdrawals that land
        before the publish time. Anything that became unavailable in between is removed, never published."""
        rec = p.rec
        page = p.page
        if page and rec.get('status') in ('PUBLISHED', 'PUBLISHED_FALLBACK'):
            gone = {t for t in ([page.get('hero_title_id')] + [x for r in page['rows'] for x in r['title_ids']])
                    if t and not self._still_available(t, published_at)}
            if gone:
                page = dict(page, hero_title_id=None if page.get('hero_title_id') in gone else page['hero_title_id'],
                            rows=[dict(r, title_ids=[x for x in r['title_ids'] if x not in gone])
                                  for r in page['rows']])
                page['rows'] = [r for r in page['rows'] if len(r['title_ids']) >= 3]
                rec['publish_time_recheck'] = {'removed': sorted(gone), 'why': 'withdrawn before publish time'}
                rec['content'] = ['title:' + t for t in ([page['hero_title_id']] if page.get('hero_title_id')
                                                         else []) + [x for r in page['rows'] for x in r['title_ids']]]
                if not page['rows'] and not page['hero_title_id']:
                    page = None
                    p.message = None
                    rec.update(status='NO_ACTION', action='NO_ACTION', no_action_reason=(
                        'every title on the page was withdrawn between decision and publish time'))
                p.page = page
        if p.message and p.message['title_id'] and not self._still_available(p.message['title_id'], published_at):
            rec.setdefault('publish_time_recheck', {})['message_dropped'] = p.message['title_id']
            rec['why_not'] = dict(rec.get('why_not') or {}, message='title withdrawn before publish time')
            p.message = None
            if rec.get('action', '').startswith('HOME_PAGE'):
                rec['action'] = 'HOME_PAGE'

    def _still_available(self, tid, at):
        w = self.content.withdrawn.get(tid)
        return w is None or w > at

    def _track_abstention(self, rec, state, ab, at):
        """Doing nothing is also an observation: what happens to the goal the agent left alone is exactly what the
        baseline predicted. It is tracked (once per goal window) so the baseline can be checked and corrected.
        Without it, a goal the floor always skips is never observed, and a wrong prediction for it can never be
        fixed. Not a goal instance: nothing is pursued, published or judged for the viewer."""
        prev = state.get('abstained_goal')
        if prev and prev['goal_id'] == ab['goal_id'] and parse(prev['deadline']) > at:
            return
        obs = dict(ab, viewer_id=rec['viewer_id'], arm=rec['arm'], decision_id=rec['decision_id'],
                   started_at=iso(at), deadline=iso(at + self.registry.window(ab['goal_id'])))
        state['abstained_goal'] = {k: obs[k] for k in ('goal_id', 'decision_id', 'started_at', 'deadline')}
        self.memory.journal(at, rec['viewer_id'], 'abstention_tracked', rec['decision_id'], obs)

    def _commit_goal(self, rec, state, goal, at, shadow=False):
        rule = self.registry.current_rule(goal['goal_id'], at)
        gi = {'instance_id': self.memory.next_id('GI'), 'viewer_id': rec['viewer_id'], 'goal_id': goal['goal_id'],
              'name': goal['name'], 'rule_version': rule.version, 'status': 'open',
              'started_at': iso(at), 'deadline': iso(at + self.registry.window(goal['goal_id'])),
              'decision_id': rec['decision_id'], 'deterministic_goal_id': goal.get('deterministic_goal_id'),
              'arm': rec['arm'], 'shadow': shadow, 'detail': goal.get('detail') or {},
              'deterministic_detail': goal.get('deterministic_detail') or {},
              'model_override': bool(goal.get('model_override')),
              'eligibility_reason': goal.get('eligibility_reason'),
              'evidence': goal['evidence'], 'committed_with_confidence': rec.get('confidence'),
              'committed': 'new'}
        self.memory.put_goal(gi)
        key = 'shadow_goal' if shadow else 'active_goal'
        old = state.get(key)
        if old:
            state['goal_history'].append({k: old[k] for k in ('instance_id', 'goal_id', 'status', 'started_at')})
        state[key] = gi
        if not shadow:
            state['plan'] = {'goal_instance_id': gi['instance_id'],
                             'steps': ['home page in the app dialect at the visit',
                                       'one follow-up message if consent, cap and quiet hours allow',
                                       'judge at deadline from attributable events']}
        self.memory.journal(at, rec['viewer_id'], 'goal_committed', gi['instance_id'], gi)
        return gi

    def _page_action(self, rec, page, at):
        a = {'action_id': self.memory.next_id('A'), 'decision_id': rec['decision_id'], 'user_id': rec['viewer_id'],
             'trigger_event_id': rec.get('trigger', {}).get('event_id'), 'type': 'home_page',
             'published_at': iso(at), 'dialect': page['dialect'], 'hero_title_id': page.get('hero_title_id'),
             'rows': page['rows']}
        a['_expires_at'] = iso(at + timedelta(minutes=self.pack.interventions['home_page']['ttl_minutes']))
        a['_session_id'] = rec.get('trigger', {}).get('session_id')
        a['_goal_instance_id'] = (rec.get('goal') or {}).get('instance_id')
        self.memory.put_action(a, 'live')
        st = self.memory.load(rec['viewer_id'])
        st['actions'].append(a['action_id'])
        return a

    def _message_action(self, rec, msg, at):
        a = {'action_id': self.memory.next_id('A'), 'decision_id': rec['decision_id'], 'user_id': rec['viewer_id'],
             'trigger_event_id': rec.get('trigger', {}).get('event_id'), 'type': msg['type'],
             'published_at': iso(at), 'send_at': iso(msg['send_at']), 'title_id': msg['title_id']}
        if msg['type'] == 'push':
            a['text'] = msg['text']
            a['deeplink'] = msg['deeplink']
        else:
            a['template_id'] = msg['template_id']
            a['variables'] = msg['variables']
        a['_dialect'] = msg['_dialect']
        a['_language'] = msg.get('_language')
        a['_resume'] = msg.get('_resume')
        a['_goal_instance_id'] = (rec.get('goal') or {}).get('instance_id')
        self.memory.put_action(a, 'queued')
        st = self.memory.load(rec['viewer_id'])
        st['actions'].append(a['action_id'])
        return a

    # ================================================================== outbox
    def release_due(self, now):
        """Queued messages are re-checked at send time with everything known by then (opt-outs, purchases, title
        withdrawals, goal closed). Returns actions to emit."""
        out = []
        rows = self.memory.db.execute("SELECT doc FROM actions WHERE status='queued' AND send_at<=? ORDER BY send_at",
                                      (iso(now),)).fetchall()
        for (doc,) in rows:
            out.extend(self._release(json.loads(doc), now))
        return out

    def flush_outbox(self, now):
        rows = self.memory.db.execute("SELECT doc FROM actions WHERE status='queued' ORDER BY send_at").fetchall()
        out = []
        for (doc,) in rows:
            out.extend(self._release(json.loads(doc), now, final=True))
        return out

    def _release(self, a, now, final=False):
        uid = a['user_id']
        state = self.memory.load(uid)
        send_at = parse(a['send_at'])
        res = CheckResult()
        self.verifier.check_common(res, uid, None, now)
        msg = dict(a, send_at=send_at)
        self.memory.set_action_status(a['action_id'], 'checking')
        self.verifier.check_message(res, uid, msg, None, now, self._scheduled_for_day)
        g = state.get('active_goal')
        reason = None
        if res.blocks('message') or res.blocks('all'):
            reason = '; '.join(f['reason'] for f in res.failed)
        elif a.get('_goal_instance_id') and (not g or g['instance_id'] != a['_goal_instance_id']
                                             or g.get('status') != 'open'):
            reason = 'goal %s is no longer open (%s)' % (a.get('_goal_instance_id'),
                                                         g.get('status') if g else 'replaced')
        if reason:
            self.memory.set_action_status(a['action_id'], 'cancelled')
            self.memory.journal(now, uid, 'message_cancelled', a['action_id'], {'reason': reason, 'action': a})
            self.ctx.log_event('message_cancelled', uid, {'action_id': a['action_id'], 'reason': reason})
            return []
        self.memory.set_action_status(a['action_id'], 'sent')
        note = {'released_at': iso(now), 'rechecked_with_knowledge_as_of': iso(now)}
        if final:
            note['note'] = 'send time is after the replay window; re-checked with knowledge at replay end'
        self.memory.journal(now, uid, 'message_released', a['action_id'], note)
        return [a]

    # ================================================================== live events
    def on_event(self, ev, now):
        """Re-plan only when the viewer's situation changes (purchase, lapse, opt-out, dialect change)."""
        uid = ev.user_id
        if uid in self.cache:
            rows, events, read_at = self.cache[uid]
            if all(e.event_id != ev.event_id for e in events):
                self.cache[uid] = (rows, events + [ev], read_at)
        state = self.memory.load(uid, self.pack.arms.get(uid))
        if not state['decisions']:
            return None
        # Goal-state transitions apply identically to the agent's goal and to a holdout viewer's shadow goal, so the
        # per-arm outcome comparison stays symmetric. Messaging changes only exist for the agent arm.
        shadow = state['arm'] != 'agent'
        g = state.get('shadow_goal' if shadow else 'active_goal')
        changes, cancelled = [], []

        def close(status, note):
            g.update(status=status, closed_at=iso(now), close_note=note)
            self.memory.put_goal(g)
            changes.append('goal %s %s -> %s: %s' % (g['instance_id'], g['goal_id'], status, note))
            if not shadow:
                cancelled.extend(self._cancel_queued(
                    uid, now, lambda a: a.get('_goal_instance_id') == g['instance_id'],
                    'goal %s is %s; its follow-up is no longer needed' % (g['instance_id'], status)))

        if g and g['status'] == 'open':
            rule = self.registry.rule_for_judging(g['goal_id'], parse(g['started_at']))
            if success_events(self.pack, g['goal_id'], rule.success, parse(g['started_at']), parse(g['deadline']),
                              [ev], g.get('detail')):
                note = 'success signal %s observed live %s after the goal started; final judgment at the deadline' % (
                    ev.ref, _human((ev.est_at - parse(g['started_at'])).total_seconds()))
                if g['goal_id'] in PAID_GOALS:
                    note += '; credit is not claimed per viewer'
                close('closed_success_observed', note)
        paid = ev.event == 'purchase_completed' and not ev.props.get('is_trial') or ev.event == 'subscription_renewed'
        if paid:
            changes.append('billing event %s received at %s; any snapshot "never paid"/"lapsed" is now stale' % (
                ev.event, iso(ev.received_at)))
            if g and g['status'] == 'open' and g['goal_id'] in PAID_GOALS:
                # Paying, but not through the goal's success signal (e.g. a renewal): the premise changed and the
                # catalogue's rule cannot count it, so it is judged inconclusive rather than claimed as a success.
                close('invalidated', 'viewer is paying (%s), which the %s success rule does not count'
                      % (ev.event, g['goal_id']))
        elif ev.event == 'subscription_lapsed':
            changes.append('subscription lapsed')
            if g and g['status'] == 'open' and g['goal_id'] in ('G08', 'G09'):
                close('invalidated', 'subscriber goal no longer applies after a lapse')
        elif ev.event == 'notification_opt_out' and not shadow:
            ch = ev.props.get('channel')
            changes.append('opted out of %s' % ch)
            state['channel_state'][ch] = {'suppressed': True, 'reason': 'opt-out ' + ev.ref}
            cancelled += self._cancel_queued(uid, now, lambda a: a['type'] == ch, 'viewer opted out of ' + ch)
        elif ev.event == 'app_dialect_changed' and not shadow:
            new = ev.props.get('to')
            changes.append('app dialect changed %s -> %s' % (ev.props.get('from'), new))
            cancelled += self._cancel_queued(uid, now, lambda a: a.get('_dialect') != new,
                                             'queued message is about a title in the old dialect')
        if not changes:
            return None
        if shadow:
            # Holdout viewers have no plan to revise: record the fact update, not a re-plan decision.
            self.memory.journal(now, uid, 'shadow_update', ev.ref, {'cause': ev.event, 'changes': changes})
            self.memory.save(state, now)
            return None
        rec = self._base('replan', uid, state['arm'], now)
        rec.update(status='REPLANNED', action='NO_ACTION', cause={'event': ev.event, 'ref': ev.ref},
                   changes=changes, cancelled_actions=cancelled,
                   no_action_reason='situation changed outside a visit; plan revised, nothing new published')
        state['decisions'].append(rec['decision_id'])
        self.memory.journal(now, uid, 'replan', rec['decision_id'], rec)
        self.memory.save(state, now)
        return rec

    def _cancel_queued(self, uid, now, pred, reason):
        out = []
        for a, status in self.memory.actions_for(uid):
            if status == 'queued' and pred(a):
                self.memory.set_action_status(a['action_id'], 'cancelled')
                self.memory.journal(now, uid, 'message_cancelled', a['action_id'], {'reason': reason})
                out.append({'action_id': a['action_id'], 'reason': reason})
        return out

    # ================================================================== control events
    def on_title_withdrawn(self, control, now):
        tid = control['title_id']
        self.content.withdraw(tid, parse(control['at']))
        affected = self.memory.actions_with_title(tid, ['live', 'queued'])
        impact = {'type': 'title_withdrawn', 'title_id': tid, 'at': control['at'], 'pages_replaced': [],
                  'pages_superseded': [], 'pages_expired': [], 'messages_cancelled': [], 'viewers': set()}
        republish = []
        for a, status in affected:
            uid = a['user_id']
            if status == 'queued':
                impact['viewers'].add(uid)
                self.memory.set_action_status(a['action_id'], 'cancelled')
                self.memory.journal(now, uid, 'message_cancelled', a['action_id'],
                                    {'reason': 'title %s withdrawn: %s' % (tid, control.get('reason'))})
                impact['messages_cancelled'].append(a['action_id'])
            elif parse(a['_expires_at']) <= now:
                self.memory.set_action_status(a['action_id'], 'expired')
                impact['pages_expired'].append(a['action_id'])
            elif self._newest_page(uid)['action_id'] != a['action_id']:
                # The app only shows a viewer's newest live page; re-publishing an older one would bring it back
                # on top of the page that replaced it.
                self.memory.set_action_status(a['action_id'], 'superseded')
                impact['pages_superseded'].append(a['action_id'])
            else:
                impact['viewers'].add(uid)
                republish.append(a)
        published = []
        for a in republish:
            page = {'dialect': a['dialect'], 'hero_title_id': a['hero_title_id'] if a['hero_title_id'] != tid else None,
                    'rows': [dict(r, title_ids=[t for t in r['title_ids'] if t != tid]) for r in a['rows']]}
            page['rows'] = [r for r in page['rows'] if len(r['title_ids']) >= 3]
            uid = a['user_id']
            state = self.memory.load(uid)
            rec = self._base('control', uid, state['arm'], now)
            res = CheckResult()
            self.verifier.check_common(res, uid, None, now)
            if page['rows'] or page['hero_title_id']:
                self.verifier.check_page(res, page, None, now, expected_dialect=a['dialect'])
            self.memory.set_action_status(a['action_id'], 'replaced')
            rec['checks'] = res.to_json()
            if (not page['rows'] and not page['hero_title_id']) or res.blocks('page') or res.blocks('all'):
                rec.update(status='NO_ACTION', action='NO_ACTION', no_action_reason='page %s withdrawn: nothing '
                           'valid left to show' % a['action_id'])
            else:
                gi = self.memory.goal(a.get('_goal_instance_id')) or {}
                rec.update(status='PUBLISHED', action='HOME_PAGE', cause={'control': 'title_withdrawn',
                                                                          'title_id': tid},
                           replaces=a['action_id'],
                           goal={'id': gi.get('goal_id'), 'instance_id': a.get('_goal_instance_id'),
                                 'committed': 'continuing'},
                           why='a live page carried a withdrawn title; replaced with the same page minus that title')
                # A decision still in flight when the control arrived can carry a publish time after `now`; the
                # replacement must be newer than the page it replaces or the app keeps showing the old one.
                at = max(now, parse(a['published_at'])) + timedelta(milliseconds=1)
                ttl = timedelta(minutes=self.pack.interventions['home_page']['ttl_minutes'])
                new = {'action_id': self.memory.next_id('A'), 'decision_id': rec['decision_id'], 'user_id': uid,
                       'trigger_event_id': a.get('trigger_event_id'), 'type': 'home_page', 'published_at': iso(at),
                       'dialect': page['dialect'], 'hero_title_id': page['hero_title_id'], 'rows': page['rows'],
                       '_expires_at': iso(at + ttl), '_session_id': a.get('_session_id'),
                       '_goal_instance_id': a.get('_goal_instance_id')}
                self.memory.put_action(new, 'live')
                state['actions'].append(new['action_id'])
                rec['actions'] = [new['action_id']]
                published.append(new)
                impact['pages_replaced'].append({'old': a['action_id'], 'new': new['action_id']})
            rec['published_at'] = (rec.get('actions') and published[-1]['published_at']) or iso(now)
            state['decisions'].append(rec['decision_id'])
            self.memory.journal(now, uid, 'control_decision', rec['decision_id'], rec)
            self.memory.save(state, now)
            self.ctx.emit_decision(rec)
        impact['viewers'] = sorted(impact['viewers'])
        impact['viewers_affected'] = len(impact['viewers'])
        return impact, published

    def _newest_page(self, uid):
        pages = [a for a, s in self.memory.actions_for(uid) if a['type'] == 'home_page' and s == 'live']
        return max(pages, key=lambda a: (a['published_at'], a['action_id'])) if pages else None

    def on_goal_rule_corrected(self, control, now):
        rv = self.registry.apply_correction(control)
        gid = control['goal_id']
        if not rv.applies_to_open:
            why = ('the correction text is ambiguous about goals already open (%r); applied to new goals only and '
                   'flagged for a human to confirm' % control.get('applies_to')) if rv.ambiguous else \
                'the correction applies to goals opened after it; open goals keep the rule they started with'
            return {'type': 'goal_rule_corrected', 'goal_id': gid, 'at': control['at'], 'new_version': rv.version,
                    'applies_to_open_goals': False, 'needs_human_review': rv.ambiguous, 'open_goals_affected': 0,
                    'viewers_affected': [], 'other_goals_touched': 0, 'note': why}
        # Not yet judged and still inside their window when the correction arrived; exactly the goals that
        # rule_for_judging will judge with the new rule, so this report and the judge cannot disagree.
        affected = [gi for gi in self.memory.goals_for(gid, ('open', 'closed_success_observed', 'invalidated'))
                    if parse(gi['started_at']) < rv.effective_from < parse(gi['deadline'])]
        for gi in affected:
            gi['judge_with'] = rv.version
            gi.setdefault('rule_history', []).append({'at': control['at'], 'version': rv.version,
                                                      'note': control.get('note')})
            self.memory.put_goal(gi)
            state = self.memory.load(gi['viewer_id'])
            for key in ('active_goal', 'shadow_goal'):
                if state.get(key) and state[key]['instance_id'] == gi['instance_id']:
                    state[key] = gi
            self.memory.journal(now, gi['viewer_id'], 'goal_rule_corrected', gi['instance_id'],
                                {'goal_id': gid, 'new_rule': rv.version, 'success': rv.success})
            self.memory.save(state, now)
        return {'type': 'goal_rule_corrected', 'goal_id': gid, 'at': control['at'], 'new_version': rv.version,
                'applies_to_open_goals': True, 'needs_human_review': False,
                'open_goals_affected': len(affected),
                'viewers_affected': sorted({g['viewer_id'] for g in affected}),
                'other_goals_touched': 0,
                'note': 'only %s goals whose window was running at the correction were re-pointed to the corrected '
                        'rule; no other viewer or decision was reprocessed' % gid}


def _h01(*parts):
    """Stable pseudo-random number in [0, 1) for randomised assignment (same viewer, same arm, every run)."""
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:8], 16) / 16 ** 8


def _human(seconds):
    s = abs(seconds)
    if s < 120:
        return '%d s' % s
    if s < 7200:
        return '%d min' % (s / 60)
    if s < 172800:
        return '%.1f h' % (s / 3600)
    return '%.1f days' % (s / 86400)
