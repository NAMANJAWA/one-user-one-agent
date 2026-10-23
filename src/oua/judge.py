"""Closes the loop: judges every goal instance from attributable signals and writes what was learned back into the
viewer's state.

Statuses
  succeeded      a success event (per the rule version that applies) inside [goal start, deadline]
  failed         the deadline passed inside the observation window with no success event
  inconclusive   the window extends past what we can observe, or the goal's premise changed (invalidated)

Attribution is reported separately and is deliberately modest. A single viewer's success can never prove the agent
caused it; "plausible" only means an intervention reached the viewer before the success. Causal impact is measured
only at arm level against the randomised holdout (impact.py).
"""
import os
from collections import defaultdict
from datetime import timedelta

from .events import EventStore, normalize
from .facts import opt_out_channels
from .goals import PAID_GOALS, success_events
from .pack import read_jsonl
from .timeutil import iso, parse

NEXT_GOAL_HINT = {'G01': 'G02', 'G02': 'G03 or G07', 'G03': 'G02 or G07', 'G04': 'G08', 'G05': 'G08',
                  'G06': 'G08', 'G07': 'G09', 'G08': 'G09', 'G09': 'G08', 'G12': 'G07 or G09'}


def load_outcomes(sim_dir):
    store = EventStore()
    for raw in read_jsonl(os.path.join(sim_dir, 'outcome_events.jsonl')):
        store.ingest(normalize(raw, 'outcome'))
    return store


class Judge:
    def __init__(self, ctx, outcome_store, observation_end):
        self.ctx = ctx
        self.pack, self.memory, self.registry = ctx.pack, ctx.memory, ctx.registry
        self.out = outcome_store
        self.end = observation_end

    def _exposures(self, uid, gi):
        """Our interventions for this goal that demonstrably reached the viewer, from simulator/platform events."""
        acts = self.memory.actions_for(uid)
        mine = {a['action_id']: a for a, _ in acts}
        status = {a['action_id']: s for a, s in acts}
        goal_actions = {aid for aid, a in mine.items() if a.get('_goal_instance_id') == gi['instance_id']}
        exp, delivery = [], defaultdict(int)
        for e in self.out.events_for(uid):
            aid = e.props.get('page_action_id') or e.props.get('action_id')
            if aid not in goal_actions:
                continue
            if e.event == 'home_page_shown' and e.props.get('personalised'):
                exp.append(('page_shown', aid, e))
                delivery['pages_shown'] += 1
            elif e.event == 'message_delivered':
                exp.append(('message_delivered', aid, e))
                delivery['messages_delivered'] += 1
            elif e.event == 'message_clicked':
                exp.append(('message_clicked', aid, e))
                delivery['messages_clicked'] += 1
            elif e.event == 'message_failed':
                delivery['message_failed_' + str(e.props.get('reason'))] += 1
        delivery['pages_published'] = sum(1 for a in goal_actions if mine[a]['type'] == 'home_page')
        delivery['messages_sent'] = sum(1 for a in goal_actions if mine[a]['type'] in ('push', 'whatsapp')
                                        and status[a] == 'sent')
        delivery['messages_cancelled'] = sum(1 for a in goal_actions if mine[a]['type'] in ('push', 'whatsapp')
                                             and status[a] == 'cancelled')
        return sorted(exp, key=lambda x: x[2].est_at), dict(delivery)

    def judge_goal(self, gi):
        uid, gid = gi['viewer_id'], gi['goal_id']
        start, deadline = parse(gi['started_at']), parse(gi['deadline'])
        rule = self.registry.rule_for_judging(gid, start)
        hits = success_events(self.pack, gid, rule.success, start, deadline, self.out.events_for(uid),
                              gi.get('detail'))
        exposures, delivery = ([], {}) if gi.get('shadow') else self._exposures(uid, gi)
        res = {'goal_instance_id': gi['instance_id'], 'viewer_id': uid, 'arm': gi['arm'], 'goal_id': gid,
               'shadow': gi.get('shadow', False), 'decision_id': gi['decision_id'], 'started_at': gi['started_at'],
               'deadline': gi['deadline'], 'rule_version_used': rule.version,
               'rule_version_at_start': gi['rule_version'], 'judged_at': iso(self.end), 'delivery': delivery}
        if rule.version != gi['rule_version']:
            res['rule_note'] = 'judged with the corrected rule %s (correction applies to goals open when it ' \
                               'arrived)' % rule.version
        if hits:
            h = hits[0]
            res.update(status='succeeded', success_event=h.ref, success_at=iso(h.est_at),
                       time_to_success_h=round((h.est_at - start).total_seconds() / 3600, 2))
        elif gi.get('status') == 'invalidated':
            res.update(status='inconclusive', reason='goal premise changed before the deadline (%s)' %
                       gi.get('close_note', 'invalidated'))
        elif deadline <= self.end:
            res.update(status='failed', reason='no %s matching "%s" between start and deadline' % (
                rule.success.get('event'), rule.success.get('condition')))
        else:
            res.update(status='inconclusive', reason='deadline %s is after the end of observable outcomes %s' % (
                gi['deadline'], iso(self.end)))
        if gi.get('shadow'):
            res['attribution'] = 'holdout shadow: no intervention; used only for arm-level comparison'
        elif res['status'] == 'succeeded':
            before = [x for x in exposures if x[2].est_at <= parse(res['success_at'])]
            if before:
                k, aid, e = before[0]
                res['attribution'] = 'plausible: %s %s at %s before the success; causality is measured only ' \
                                     'against the holdout' % (k, aid, iso(e.est_at))
            else:
                res['attribution'] = 'not attributable: nothing the agent published reached the viewer before ' \
                                     'the success'
            if gid in PAID_GOALS and res['time_to_success_h'] < 0.25:
                res['attribution'] += '; purchase within 15 min of the decision, so intent very likely predates it'
        elif res['status'] == 'failed':
            res['attribution'] = ('intervention reached the viewer (%d exposures) but the goal was not reached'
                                  % len(exposures)) if exposures else \
                'intervention never reached the viewer (page not shown, message not delivered)'
        else:
            res['attribution'] = 'n/a'
        res['learned'] = self._learn(gi, res, exposures, delivery)
        return res

    def _learn(self, gi, res, exposures, delivery):
        if gi.get('shadow'):
            return []
        out = []
        gid = gi['goal_id']
        if res['status'] == 'succeeded':
            out.append({'kind': 'goal_reached', 'text': '%s reached in %.1f h; next lifecycle goal to consider: %s'
                        % (gid, res['time_to_success_h'], NEXT_GOAL_HINT.get(gid, 'any eligible')),
                        'evidence': [res['success_event']]})
            t = self.pack.titles.get(self.out.get(res['success_event'][6:]).props.get('title_id')) \
                if self.out.get(res['success_event'][6:]) else None
            if t:
                out.append({'kind': 'taste', 'text': 'played %s (%s, %s)' % (t.title_id, t.dialect,
                                                                       '/'.join(t.genres)),
                            'evidence': [res['success_event'], 'catalogue:' + t.title_id]})
        elif res['status'] == 'failed':
            if exposures:
                out.append({'kind': 'goal_penalty', 'goal_id': gid, 'delta': -0.1,
                            'text': '%s not reached despite %d exposures; lower its priority next time' % (
                                gid, len(exposures)), 'evidence': [x[1] for x in exposures[:3]]})
            else:
                out.append({'kind': 'reach', 'text': 'page published but never shown (viewer did not reach home '
                            'while it was live, or switched dialect); prefer a consented message next time',
                            'evidence': []})
        if delivery.get('message_failed_token_invalid'):
            out.append({'kind': 'channel', 'channel': 'push', 'suppress': True,
                        'text': 'push token invalid (message_failed); stop using push until a new token is seen',
                        'evidence': []})
        for e in self.out.events_for(gi['viewer_id']):
            if e.event == 'notification_opt_out' and e.est_at >= parse(gi['started_at']):
                for ch in sorted(opt_out_channels(e.props)):
                    out.append({'kind': 'channel', 'channel': ch, 'suppress': True,
                                'text': 'viewer opted out of %s after our message' % ch, 'evidence': [e.ref]})
        return out

    def run(self):
        """Judges every goal. Idempotent: running it again rewrites outcomes.jsonl but does not append outcomes or
        learnings a second time. The goal's lifecycle `status` (open / closed_success_observed / invalidated) is
        kept; the verdict goes in `judgment`."""
        results = []
        for gi in self.memory.all_goals():
            r = self.judge_goal(gi)
            results.append(r)
            state = self.memory.load(r['viewer_id'])
            key = 'shadow_goal' if r['shadow'] else 'active_goal'
            g = state.get(key)
            if g and g['instance_id'] == r['goal_instance_id']:
                g['judgment'] = r['status']
            already = any(o['goal_instance_id'] == r['goal_instance_id'] for o in state['outcomes'])
            if not already:
                state['outcomes'].append({k: r[k] for k in ('goal_instance_id', 'goal_id', 'status', 'attribution')})
                for l in r['learned']:
                    l = dict(l, at=iso(self.end), goal_instance_id=r['goal_instance_id'])
                    state['learnings'].append(l)
                    if l['kind'] == 'channel':
                        state['channel_state'][l['channel']] = {'suppressed': True, 'reason': l['text']}
                    if l['kind'] == 'goal_penalty':
                        adj = state.setdefault('goal_adjustments', {})
                        adj[l['goal_id']] = round(adj.get(l['goal_id'], 0) + l['delta'], 2)
                self.memory.journal(self.end, r['viewer_id'], 'outcome', r['goal_instance_id'], r)
            self.memory.save(state, self.end)
            g = dict(gi, judgment=r['status'], judged_at=r['judged_at'])
            self.memory.put_goal(g)
        self.memory.commit()
        return results


def observation_end(outcome_store, replay_start):
    last = max((e.est_at for evs in outcome_store.by_user.values() for e in evs), default=replay_start)
    return max(last, replay_start + timedelta(days=15))
