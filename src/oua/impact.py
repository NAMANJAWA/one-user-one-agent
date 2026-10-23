"""Impact measurement against the randomised holdout.

Unit: every viewer who sent at least one trigger on the replay day (the randomised population that the agent could
have acted on). Arm comes from replay/arms.csv, assigned before the replay. We compare ALL agent-arm viewers with ALL
holdout viewers (intent-to-treat), never "viewers we served" with anyone, because who gets served is decided by the
agent and is correlated with how active a viewer already is.

Metrics are fixed in advance (below), measured from the trigger time of each viewer, identically in both arms.
"""
import math
import os
import random
import subprocess
import sys
from collections import defaultdict
from datetime import timedelta

from .goals import success_events
from .judge import load_outcomes
from .pack import load_csv, read_jsonl
from .timeutil import ist_day, parse

METRICS = [
    ('play_24h', 'started any episode within 24 h of the first trigger', 'pre_play_days_14'),
    ('complete_72h', 'completed an episode within 72 h', 'pre_completions_30'),
    ('active_days_14', 'distinct IST days with an app open in the 14 days after the replay day', 'pre_active_days_14'),
    ('purchase_7d', 'bought a paid plan within 7 days', 'pre_ever_paid'),
    ('goal_success', 'reached the goal the deterministic planner assigned (shadow goal for holdout)', None),
]


def _mean(xs):
    return sum(xs) / len(xs) if xs else float('nan')


def _var(xs):
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1) if len(xs) > 1 else 0.0


def diff_ci(t, c):
    d = _mean(t) - _mean(c)
    se = math.sqrt(_var(t) / len(t) + _var(c) / len(c)) if t and c else float('nan')
    return d, se, (d - 1.96 * se, d + 1.96 * se)


def perm_p(t, c, n=1000, seed=7):
    obs = abs(_mean(t) - _mean(c))
    pool = t + c
    rng = random.Random(seed)
    k = len(t)
    hits = 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(_mean(pool[:k]) - _mean(pool[k:])) >= obs - 1e-12:
            hits += 1
    return (hits + 1) / (n + 1)


def cuped(rows, metric, cov):
    xs = [r[cov] for r in rows]
    ys = [r[metric] for r in rows]
    mx, my = _mean(xs), _mean(ys)
    vx = _var(xs)
    if vx == 0:
        return None
    theta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1) / vx
    adj = [{'arm': r['arm'], 'y': r[metric] - theta * (r[cov] - mx)} for r in rows]
    t = [a['y'] for a in adj if a['arm'] == 'agent']
    c = [a['y'] for a in adj if a['arm'] == 'holdout']
    d, se, ci = diff_ci(t, c)
    return {'theta': round(theta, 4), 'diff': d, 'se': se, 'ci': ci}


class Population:
    def __init__(self, ctx, run_dir, sim_dir, actions_path=None):
        self.ctx = ctx
        pack = ctx.pack
        self.out = load_outcomes(sim_dir)
        triggers = list(read_jsonl(pack.paths['triggers']))
        first = {}
        for t in triggers:
            at = parse(t['deliver_at'])
            if t['user_id'] not in first or at < first[t['user_id']]:
                first[t['user_id']] = at
        self.replay_start = min(first.values())
        actions = list(read_jsonl(actions_path or os.path.join(run_dir, 'actions.jsonl')))
        self.served_ids = defaultdict(set)
        for a in actions:
            self.served_ids[a['user_id']].add(a['action_id'])
        goals = {}
        for gi in ctx.memory.all_goals():
            if gi['viewer_id'] not in goals or gi['started_at'] < goals[gi['viewer_id']]['started_at']:
                goals[gi['viewer_id']] = gi
        hist = defaultdict(list)
        for e in read_jsonl(pack.paths['events']):
            hist[e['user_id']].append(e)
        end = max((e.est_at for evs in self.out.by_user.values() for e in evs), default=self.replay_start)
        self.end = end
        self.rows = []
        for uid, t0 in sorted(first.items()):
            arm = pack.arms.get(uid)
            if arm not in ('agent', 'holdout'):
                continue
            evs = self.out.events_for(uid)
            r = {'viewer_id': uid, 'arm': arm, 't0': t0}

            def any_in(name, hours, pred=lambda e: True):
                return int(any(e.event == name and t0 <= e.est_at <= t0 + timedelta(hours=hours) and pred(e)
                               for e in evs))
            r['play_24h'] = any_in('play_started', 24)
            r['complete_72h'] = any_in('episode_completed', 72)
            r['purchase_7d'] = any_in('purchase_completed', 168, lambda e: not e.props.get('is_trial'))
            day0 = ist_day(t0)
            r['active_days_14'] = len({ist_day(e.est_at) for e in evs if e.event == 'app_open'
                                       and 0 < (ist_day(e.est_at) - day0).days <= 14})
            h = hist.get(uid, [])
            cut = self.replay_start
            pre = [e for e in h if parse(e['received_at']) < cut]
            r['pre_play_days_14'] = len({str(ist_day(parse(e['received_at']))) for e in pre
                                         if e['event'] == 'play_started'
                                         and parse(e['received_at']) >= cut - timedelta(days=14)})
            r['pre_active_days_14'] = len({str(ist_day(parse(e['received_at']))) for e in pre
                                           if e['event'] == 'app_open'
                                           and parse(e['received_at']) >= cut - timedelta(days=14)})
            r['pre_completions_30'] = min(20, sum(1 for e in pre if e['event'] == 'episode_completed'))
            r['pre_ever_paid'] = int(any(e['event'] in ('purchase_completed', 'subscription_renewed') for e in pre))
            gi = goals.get(uid)
            r['goal_success'] = None
            r['intended_goal'] = None
            if gi:
                gid = gi.get('deterministic_goal_id') or gi['goal_id']
                start = parse(gi['started_at'])
                dl = start + ctx.registry.window(gid)
                if dl <= end:
                    rule = ctx.registry.rule_for_judging(gid, start)
                    # The deterministic goal's own detail (e.g. G07's series) is stored at commit, so the metric
                    # works even when the model overrode the planner's choice.
                    detail = gi.get('detail') if gid == gi['goal_id'] else gi.get('deterministic_detail') or {}
                    r['goal_success'] = int(bool(success_events(pack, gid, rule.success, start, dl, evs, detail)))
                    r['intended_goal'] = gid
            mine = self.served_ids.get(uid, set())
            r['exposed'] = int(any((e.event == 'home_page_shown' and e.props.get('personalised')
                                    and e.props.get('page_action_id') in mine) or
                                   (e.event == 'message_delivered' and e.props.get('action_id') in mine)
                                   for e in evs))
            r['served'] = int(bool(mine))
            self.rows.append(r)

    def estimate(self, metric, cov=None, rows=None):
        rows = [r for r in (rows or self.rows) if r[metric] is not None]
        t = [r[metric] for r in rows if r['arm'] == 'agent']
        c = [r[metric] for r in rows if r['arm'] == 'holdout']
        d, se, ci = diff_ci(t, c)
        out = {'metric': metric, 'n_agent': len(t), 'n_holdout': len(c), 'mean_agent': _mean(t),
               'mean_holdout': _mean(c), 'itt': d, 'se': se, 'ci95': ci, 'p_permutation': perm_p(t, c),
               'mde_80pct_power': 2.8 * se}
        if cov:
            out['cuped'] = cuped(rows, metric, cov)
            out['cuped_covariate'] = cov
        exp_rate = _mean([r['exposed'] for r in rows if r['arm'] == 'agent'])
        out['exposure_rate_agent_arm'] = exp_rate
        out['effect_on_exposed'] = d / exp_rate if exp_rate else None
        return out

    def naive(self, metric):
        ag = [r for r in self.rows if r['arm'] == 'agent' and r[metric] is not None]
        exp = [r[metric] for r in ag if r['exposed']]
        unexp = [r[metric] for r in ag if not r['exposed']]
        everyone_else = [r[metric] for r in self.rows if r[metric] is not None and not (r['arm'] == 'agent'
                                                                                        and r['exposed'])]
        pre_exp = _mean([r['pre_active_days_14'] for r in ag if r['exposed']])
        pre_unexp = _mean([r['pre_active_days_14'] for r in ag if not r['exposed']])
        return {'exposed_mean': _mean(exp), 'unexposed_mean': _mean(unexp), 'n_exposed': len(exp),
                'n_unexposed': len(unexp), 'exposed_vs_unexposed': _mean(exp) - _mean(unexp),
                'exposed_vs_everyone_else': _mean(exp) - _mean(everyone_else),
                'pre_active_days_exposed': pre_exp, 'pre_active_days_unexposed': pre_unexp}

    def balance(self):
        out = {}
        for cov in ('pre_play_days_14', 'pre_active_days_14', 'pre_completions_30', 'pre_ever_paid'):
            t = [r[cov] for r in self.rows if r['arm'] == 'agent']
            c = [r[cov] for r in self.rows if r['arm'] == 'holdout']
            out[cov] = {'agent': _mean(t), 'holdout': _mean(c), 'diff': _mean(t) - _mean(c)}
        return out

    def by_goal(self):
        out = {}
        for g in sorted({r['intended_goal'] for r in self.rows if r['intended_goal']}):
            rows = [r for r in self.rows if r['intended_goal'] == g]
            t = [r['goal_success'] for r in rows if r['arm'] == 'agent']
            c = [r['goal_success'] for r in rows if r['arm'] == 'holdout']
            if t and c:
                d, se, ci = diff_ci(t, c)
                out[g] = {'n_agent': len(t), 'n_holdout': len(c), 'agent': _mean(t), 'holdout': _mean(c),
                          'diff': d, 'ci95': ci}
        return out


def judged_goals(ctx, out_store):
    """Every goal instance with its predicted p_without_agent and whether it succeeded (same rule the judge uses).
    Goals whose window extends past the observable outcomes are left out."""
    end = max((e.est_at for evs in out_store.by_user.values() for e in evs), default=None)
    rows = []
    for gi in ctx.memory.all_goals():
        start, dl = parse(gi['started_at']), parse(gi['deadline'])
        p = (gi.get('detail') or {}).get('p_without_agent')
        if p is None or end is None or dl > end or gi.get('status') == 'invalidated':
            continue
        rule = ctx.registry.rule_for_judging(gi['goal_id'], start)
        ok = bool(success_events(ctx.pack, gi['goal_id'], rule.success, start, dl, out_store.events_for(gi['viewer_id']),
                                 gi.get('detail')))
        rows.append({'goal_id': gi['goal_id'], 'viewer_id': gi['viewer_id'], 'shadow': gi.get('shadow', False),
                     'p': p, 'ok': int(ok),
                     'bucket': ((gi.get('detail') or {}).get('baseline') or {}).get('bucket')})
    # Goals the agent left alone ("not worth an intervention"): nothing was pursued, so they test the baseline but
    # not the agent's lift. Holdout ones count as holdout observations, like the shadow goals above.
    for ab in ctx.memory.journal_of_kind('abstention_tracked'):
        start, dl = parse(ab['started_at']), parse(ab['deadline'])
        if ab.get('p_without_agent') is None or end is None or dl > end:
            continue
        rule = ctx.registry.rule_for_judging(ab['goal_id'], start)
        ok = bool(success_events(ctx.pack, ab['goal_id'], rule.success, start, dl,
                                 out_store.events_for(ab['viewer_id']), ab.get('detail')))
        rows.append({'goal_id': ab['goal_id'], 'viewer_id': ab['viewer_id'], 'shadow': ab['arm'] != 'agent',
                     'p': ab['p_without_agent'], 'ok': int(ok), 'bucket': ab.get('bucket'), 'abstained': True})
    return rows


def calibration(rows):
    """The holdout arm got nothing, so its shadow goals test the organic baseline directly."""
    hold = [r for r in rows if r['shadow']]
    agent = [r for r in rows if not r['shadow'] and not r.get('abstained')]
    by_goal = {}
    for g in sorted({r['goal_id'] for r in rows}):
        h = [r for r in hold if r['goal_id'] == g]
        a = [r for r in agent if r['goal_id'] == g]
        if not h:
            continue
        obs = _mean([r['ok'] for r in h])
        se = math.sqrt(obs * (1 - obs) / len(h)) if len(h) > 1 else float('nan')
        by_goal[g] = {'n_holdout': len(h), 'n_left_alone': sum(1 for r in h if r.get('abstained')),
                      'predicted': _mean([r['p'] for r in h]), 'observed_holdout': obs,
                      'ci95': (obs - 1.96 * se, obs + 1.96 * se), 'n_agent': len(a),
                      'observed_agent': _mean([r['ok'] for r in a]) if a else None}
    brier = _mean([(r['p'] - r['ok']) ** 2 for r in hold]) if hold else None
    base = _mean([r['ok'] for r in hold]) if hold else None
    brier_const = _mean([(base - r['ok']) ** 2 for r in hold]) if hold else None
    return {'by_goal': by_goal, 'brier': brier, 'brier_constant': brier_const, 'n_holdout': len(hold)}


def followup_experiment(ctx, run_dir, out_store):
    """Randomised within the agent arm: follow-up sent vs withheld (assigned per viewer, p=0.5). Outcomes are
    measured from the decision that assigned it."""
    rows = []
    seen = set()
    for d in read_jsonl(os.path.join(run_dir, 'decisions.jsonl')):
        exp = d.get('followup_experiment')
        if not exp or d['viewer_id'] in seen:
            continue
        seen.add(d['viewer_id'])
        t0 = parse(d['published_at'])
        evs = out_store.events_for(d['viewer_id'])
        g = d.get('goal') or {}
        gi = ctx.memory.goal(g.get('instance_id')) if g.get('instance_id') else None
        ok = None
        if gi:
            rule = ctx.registry.rule_for_judging(gi['goal_id'], parse(gi['started_at']))
            ok = int(bool(success_events(ctx.pack, gi['goal_id'], rule.success, parse(gi['started_at']),
                                         parse(gi['deadline']), evs, gi.get('detail'))))
        rows.append({'send': exp['assigned'] == 'send', 'goal_success': ok,
                     'play_72h': int(any(e.event == 'play_started' and t0 <= e.est_at <= t0 + timedelta(hours=72)
                                         for e in evs)),
                     'clicked': int(any(e.event == 'message_clicked' for e in evs))})
    out = {'n_send': sum(r['send'] for r in rows), 'n_withhold': sum(not r['send'] for r in rows)}
    for m in ('goal_success', 'play_72h'):
        t = [r[m] for r in rows if r['send'] and r[m] is not None]
        c = [r[m] for r in rows if not r['send'] and r[m] is not None]
        if t and c:
            d, se, ci = diff_ci(t, c)
            out[m] = {'send': _mean(t), 'withhold': _mean(c), 'diff': d, 'ci95': ci, 'p': perm_p(t, c),
                      'mde': 2.8 * se}
    out['clicks'] = sum(r['clicked'] for r in rows if r['send'])
    return out


def pilot_reanalysis(pack):
    path = os.path.join(pack.root, 'pilot', 'pilot_outcomes.csv')
    rows = load_csv(path)
    s = lambda rs: (len(rs), sum(r['succeeded'] == 'true' for r in rs))

    def rate(rs):
        n, k = s(rs)
        return k / n if n else float('nan')
    t = [int(r['succeeded'] == 'true') for r in rows if r['arm'] == 'treatment']
    c = [int(r['succeeded'] == 'true') for r in rows if r['arm'] == 'holdout']
    d, se, ci = diff_ci(t, c)
    served = [r for r in rows if r['served'] == 'true']
    others = [r for r in rows if r['served'] != 'true']
    by_goal = {}
    for g in sorted({r['goal_id'] for r in rows}):
        by_goal[g] = {'treatment': rate([r for r in rows if r['goal_id'] == g and r['arm'] == 'treatment']),
                      'holdout': rate([r for r in rows if r['goal_id'] == g and r['arm'] == 'holdout']),
                      'served': rate([r for r in served if r['goal_id'] == g]),
                      'share_of_served': sum(r['goal_id'] == g for r in served) / len(served),
                      'share_of_all': sum(r['goal_id'] == g for r in rows) / len(rows)}
    serve_rate = sum(r['served'] == 'true' for r in rows if r['arm'] == 'treatment') / len(t)
    return {'reported': {'served': rate(served), 'everyone_else': rate(others), 'n_served': len(served),
                         'n_else': len(others)},
            'itt': {'treatment': _mean(t), 'holdout': _mean(c), 'diff': d, 'ci95': ci, 'p': perm_p(t, c),
                    'n_t': len(t), 'n_c': len(c)},
            'serve_rate': serve_rate, 'effect_on_served': d / serve_rate if serve_rate else None,
            'by_goal': by_goal}


def run_simulator(pack, actions, out_dir, seed):
    cmd = [sys.executable, pack.paths['simulator'], '--pack', pack.root, '--actions', actions, '--out', out_dir,
           '--seed', seed]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def multi_seed(ctx, run_dir, seeds):
    """Robustness: the evaluators use their own seed. For each seed we simulate twice, with our actions and with
    no actions at all. The no-action run is an A/A test (any 'effect' there is pure noise), and, only because this
    is a simulator, the difference between the two runs for the same agent-arm viewers is the true simulated effect,
    which lets us check that the ITT estimator recovers it."""
    empty = os.path.join(run_dir, 'sim_seeds', 'empty_actions.jsonl')
    os.makedirs(os.path.dirname(empty), exist_ok=True)
    open(empty, 'w').close()
    out = []
    for s in seeds:
        d1 = os.path.join(run_dir, 'sim_seeds', s, 'with_agent')
        d0 = os.path.join(run_dir, 'sim_seeds', s, 'no_actions')
        run_simulator(ctx.pack, os.path.join(run_dir, 'actions.jsonl'), d1, s)
        run_simulator(ctx.pack, empty, d0, s)
        p1 = Population(ctx, run_dir, d1)
        p0 = Population(ctx, run_dir, d0, actions_path=empty)
        row = {'seed': s}
        for m in ('play_24h', 'complete_72h', 'active_days_14'):
            e1, e0 = p1.estimate(m), p0.estimate(m)
            y1 = {r['viewer_id']: r[m] for r in p1.rows if r['arm'] == 'agent'}
            y0 = {r['viewer_id']: r[m] for r in p0.rows if r['arm'] == 'agent'}
            true = _mean([y1[u] - y0[u] for u in y1])
            row[m] = {'itt': e1['itt'], 'ci95': e1['ci95'], 'aa_diff_no_actions': e0['itt'],
                      'true_simulated_effect': true, 'ci_covers_truth': e1['ci95'][0] <= true <= e1['ci95'][1],
                      'itt_minus_aa': e1['itt'] - e0['itt']}
        out.append(row)
        for d in (d0, d1):   # keep the simulator's summary, drop the bulky raw events
            ev = os.path.join(d, 'outcome_events.jsonl')
            if os.path.exists(ev):
                os.remove(ev)
    return out


def pct(x, signed=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 'n/a'
    return ('%+.1f' if signed else '%.1f') % (100 * x)


def conclusions(ests):
    """Plain-language conclusions generated from the intervals, so the report can never claim an effect the data
    does not show (and says so when there is none, e.g. on an A/A run)."""
    up, down, flat = [], [], []
    for m, desc, _ in METRICS:
        e = ests[m]
        lo, hi = e['ci95']
        label = '%s (%s)' % (m, ('%+.1f pp' % (100 * e['itt'])) if m != 'active_days_14' else '%+.2f days' % e['itt'])
        (up if lo > 0 else down if hi < 0 else flat).append(label)
    out = []
    if up:
        out.append('Detectable increases (95%% interval above zero): %s.' % ', '.join(up))
    if down:
        out.append('Detectable decreases (95%% interval below zero): %s.' % ', '.join(down))
    if flat:
        out.append('No detectable change (interval includes zero): %s. The sample cannot rule out effects smaller '
                   'than the minimum detectable effect.' % ', '.join(flat))
    if not up and not down:
        out.append('Nothing in this run shows an effect of the agent.')
    if 'purchase_7d' in ' '.join(flat):
        out.append('No revenue claim is made.')
    return out


def write_calibration_and_experiment(w, cal, fx):
    w('## Is the organic baseline right? (calibration on the holdout)')
    w('')
    w('The agent scores goals with p_without_agent, estimated only from pre-replay data (pilot holdout + 30-day '
      'history). Holdout viewers received nothing, so their shadow goals show what actually happens without the '
      'agent in this environment. Goals left alone as "not worth an intervention" are tracked too: otherwise a goal '
      'the floor always skips would never be observed and its prediction never corrected.')
    w('')
    w('| Goal | Holdout goals | Predicted p | Observed (95% CI) | Agent-arm goals | Agent-arm observed |')
    w('|---|---|---|---|---|---|')
    for g, v in cal['by_goal'].items():
        w('| %s | %d%s | %s%% | %s%% (%s to %s) | %d | %s |' % (
            g, v['n_holdout'], (' (%d left alone)' % v['n_left_alone']) if v.get('n_left_alone') else '',
            pct(v['predicted']), pct(v['observed_holdout']), pct(v['ci95'][0]),
            pct(v['ci95'][1]), v['n_agent'], ('%s%%' % pct(v['observed_agent'])) if v['observed_agent'] is not None
            else 'n/a'))
    w('')
    if cal['brier'] is not None:
        w('Brier score on %d holdout goals: %.3f, against %.3f for predicting the overall rate for everyone (lower is '
          'better).' % (cal['n_holdout'], cal['brier'], cal['brier_constant']))
        w('')
    bad = [g for g, v in cal['by_goal'].items() if not v['ci95'][0] <= v['predicted'] <= v['ci95'][1]
           and v['n_holdout'] >= 20]
    if bad:
        w('Miscalibrated (prediction outside the 95%% interval, n >= 20): %s. The history these rates come from and '
          'the simulator that produced the outcomes do not behave the same way for these goals. `oua learn` folds '
          'these holdout outcomes back into the baseline, so the next day is scored with what happens here; '
          '`learning_report.md` checks out of sample whether that improves the predictions.' % ', '.join(bad))
        w('')
    w('## Do follow-up messages help? (randomised within the agent arm)')
    w('')
    if fx.get('play_72h'):
        w('Viewers eligible for a follow-up were assigned "send" or "withhold" at random (p=0.5): %d sent, %d '
          'withheld. Sent messages drew %d clicks.' % (fx['n_send'], fx['n_withhold'], fx['clicks']))
        w('')
        w('| Metric | Sent | Withheld | Difference (95% CI) | p | Smallest detectable |')
        w('|---|---|---|---|---|---|')
        for m in ('goal_success', 'play_72h'):
            if m in fx:
                v = fx[m]
                w('| %s | %s%% | %s%% | %s (%s to %s) | %.2f | %s pp |' % (
                    m, pct(v['send']), pct(v['withhold']), pct(v['diff'], True), pct(v['ci95'][0], True),
                    pct(v['ci95'][1], True), v['p'], pct(v['mde'])))
        w('')
        w('With this few eligible viewers only a large effect could be detected; the design, not one day, is the '
          'point: keep randomising and the estimate tightens.')
    else:
        w('No viewer was eligible for a follow-up in this run.')
    w('')


def write_report(path, pop, pilot, seeds, sim_report, policy_audit=None, cal=None, fx=None):
    L = []
    w = L.append
    w('# Impact report')
    w('')
    run_name = os.path.basename(os.path.dirname(os.path.abspath(path)))
    w('All numbers below are computed by `oua impact` from `%s/sim/outcome_events.jsonl` (simulator seed `%s`) and '
      'the randomised arms in `replay/arms.csv`. Nothing here is hand-edited; the conclusions are generated from '
      'the intervals.' % (run_name, sim_report.get('seed')))
    w('')
    w('## Headline (intent-to-treat, agent arm vs randomised holdout)')
    w('')
    w('Population: every viewer who sent a trigger on the replay day, in the arm they were randomised to, whether '
      'or not the agent acted on them. Differences are in percentage points (pp) unless noted.')
    w('')
    w('| Metric | n agent / holdout | Agent | Holdout | Difference (95% CI) | p (permutation) | '
      'Adjusted for past activity (95% CI) |')
    w('|---|---|---|---|---|---|---|')
    ests = {}
    for m, desc, cov in METRICS:
        e = pop.estimate(m, cov)
        ests[m] = e
        is_rate = m != 'active_days_14'
        f = (lambda x, s=False: pct(x, s)) if is_rate else (lambda x, s=False: ('%+.2f' if s else '%.2f') % x)
        cu = e.get('cuped')
        cu_s = '%s (%s to %s)' % (f(cu['diff'], True), f(cu['ci'][0], True), f(cu['ci'][1], True)) if cu else 'n/a'
        unit = '' if is_rate else ' days'
        w('| %s: %s | %d / %d | %s%s | %s%s | **%s** (%s to %s) | %.3f | %s |' % (
            m, desc, e['n_agent'], e['n_holdout'], f(e['mean_agent']), '%' if is_rate else unit,
            f(e['mean_holdout']), '%' if is_rate else unit,
            f(e['itt'], True), f(e['ci95'][0], True), f(e['ci95'][1], True), e['p_permutation'], cu_s))
    w('')
    n_t, n_c = ests['play_24h']['n_agent'], ests['play_24h']['n_holdout']
    w('goal_success is measured only for viewers who got a goal (identically in both arms), hence its smaller n.')
    w('')
    w('Viewers: %d agent, %d holdout. The smallest effect this sample can reliably detect (80%% power, 5%% '
      'significance) is about %s pp for play_24h and %s pp for purchase_7d: smaller true effects can look like '
      'zero here.' % (n_t, n_c, pct(ests['play_24h']['mde_80pct_power']), pct(ests['purchase_7d']['mde_80pct_power'])))
    w('')
    e = ests['play_24h']
    w('**Effect on viewers the agent actually reached.** Only %s%% of agent-arm viewers saw a personalised page or '
      'received a message (the rest arrived stale, in a dialect with no content, or never reached home while the '
      'page was live). Dividing the intent-to-treat effect by that share gives the effect on reached viewers: '
      '%s pp for play_24h. This holds because holdout viewers cannot be reached by construction.' % (
          pct(e['exposure_rate_agent_arm']), pct(e['effect_on_exposed'], True)))
    w('')
    w('## What this does and does not show')
    w('')
    for line in conclusions(ests):
        w('- ' + line)
    w("- Any effect here is an effect inside the pack's simulator, which raises play probability for every valid "
      'row on a live page in the right dialect; real viewers respond less mechanically. Treat a size as an upper '
      'bound on what this policy could do, and the sign and the method as the result.')
    w('- The simulator picks the title a viewer plays without looking at our page (unless a message was clicked), '
      'so goals tied to a specific title, such as resuming a series, are hard to move here.')
    w('')
    w('## Were the arms comparable before the replay?')
    w('')
    w('| Pre-period covariate (from data/events.jsonl) | Agent | Holdout | Difference |')
    w('|---|---|---|---|')
    for k, v in pop.balance().items():
        w('| %s | %.3f | %.3f | %+.3f |' % (k, v['agent'], v['holdout'], v['diff']))
    w('')
    w('Randomisation balances arms only on average. Where the arms differ before the replay, the adjusted column '
      'above removes the part of the outcome explained by past activity (CUPED regression adjustment). It narrows '
      'the interval and corrects for chance imbalance without introducing selection.')
    w('')
    w('## Why simpler comparisons would mislead')
    w('')
    nv = pop.naive('play_24h')
    w('| Comparison (play_24h) | Value | What is wrong with it |')
    w('|---|---|---|')
    w('| Reached vs not reached, agent arm only | %s pp | Reaching a viewer depends on them being active (a fresh '
      'visit, reaching home). Reached viewers had %.2f active days in the previous 14 vs %.2f for the others, so '
      'the gap is mostly who they are. |' % (pct(nv['exposed_vs_unexposed'], True), nv['pre_active_days_exposed'],
                                             nv['pre_active_days_unexposed']))
    w('| Reached vs everyone else (the pilot report\'s method) | %s pp | Same selection, plus it mixes in holdout '
      'and unreached viewers. This is the method behind the pilot\'s "2.3x uplift". |' % pct(
          nv['exposed_vs_everyone_else'], True))
    w('| **Agent arm vs holdout (this report)** | **%s pp** | Randomised, so the only systematic difference '
      'is the agent. |' % pct(ests['play_24h']['itt'], True))
    w('')
    w('Two more traps: (1) per-goal success rates without a holdout reward easy goals. The pilot\'s weekly habit '
      'goal (G09) succeeded %s%% of the time in the holdout with no agent at all. (2) Before/after comparisons '
      'confuse the agent with day-of-week, campaigns and viewers who were already about to come back.' % pct(
          pilot['by_goal'].get('G09', {}).get('holdout')))
    w('')
    w('## Goal success by intended goal (same deterministic planner in both arms)')
    w('')
    w('The holdout viewers got a *shadow* decision: the agent computed the goal it would have chosen, then did '
      'nothing. That makes goal success comparable goal by goal. Small cells are noisy, so read the intervals.')
    w('')
    w('| Goal | n agent | n holdout | Agent | Holdout | Difference (95% CI) |')
    w('|---|---|---|---|---|---|')
    for g, v in pop.by_goal().items():
        if min(v['n_agent'], v['n_holdout']) < 10:
            w('| %s | %d | %d | %s%% | %s%% | too few viewers to compare |' % (
                g, v['n_agent'], v['n_holdout'], pct(v['agent']), pct(v['holdout'])))
            continue
        w('| %s | %d | %d | %s%% | %s%% | %s (%s to %s) |' % (g, v['n_agent'], v['n_holdout'], pct(v['agent']),
                                                              pct(v['holdout']), pct(v['diff'], True),
                                                              pct(v['ci95'][0], True), pct(v['ci95'][1], True)))
    w('')
    if cal is not None and fx is not None:
        write_calibration_and_experiment(w, cal, fx)
    if seeds:
        w('## Robustness across simulator seeds')
        w('')
        w('Evaluators use a different seed. For each seed the simulator ran twice, with our actions and with no '
          'actions. Because it is a simulator we can compute the true effect on the same agent-arm viewers (with '
          'minus without actions), which is impossible in production, and check that the intent-to-treat interval '
          'contains it.')
        w('')
        w('The holdout\'s outcomes are identical in both runs (each viewer has their own random stream and the '
          'simulator ignores actions on holdout viewers). So the no-action A/A difference is exactly the ITT minus '
          'the true effect: the two columns are one check, not two, and "covers the truth" means the chance '
          'imbalance between arms stayed inside the interval.')
        w('')
        w('| Seed | play_24h ITT (95% CI) | True simulated effect | Chance imbalance (= no-action A/A) | '
          'CI covers truth |')
        w('|---|---|---|---|---|')
        for r in seeds:
            x = r['play_24h']
            w('| %s | %s (%s to %s) | %s | %s | %s |' % (r['seed'], pct(x['itt'], True), pct(x['ci95'][0], True),
                                                     pct(x['ci95'][1], True), pct(x['true_simulated_effect'], True),
                                                     pct(x['aa_diff_no_actions'], True),
                                                     'yes' if x['ci_covers_truth'] else 'no'))
        w('')
        cov = sum(r['play_24h']['ci_covers_truth'] for r in seeds)
        w('The interval contained the true simulated effect in %d of %d seeds.' % (cov, len(seeds)))
        w('')
    w('## The pilot report, re-analysed')
    w('')
    pr = pilot['reported']
    it = pilot['itt']
    w('The internal pilot report claimed **%s%% vs %s%%, a "2.3x uplift"**, comparing %d served viewers with %d '
      '"other users". That compares viewers the agent chose to serve with everyone else, including the holdout.' %
      (pct(pr['served']), pct(pr['everyone_else']), pr['n_served'], pr['n_else']))
    w('')
    w('The randomised comparison in the same file: treatment %s%% vs holdout %s%%, **difference %s pp (95%% CI %s to '
      '%s, p=%.2f)**. Only %s%% of the treatment arm was ever served, so even the implied effect on served viewers '
      '(%s pp) is indistinguishable from zero. **The pilot shows no evidence that agent v0 helped.**' % (
          pct(it['treatment']), pct(it['holdout']), pct(it['diff'], True), pct(it['ci95'][0], True),
          pct(it['ci95'][1], True), it['p'], pct(pilot['serve_rate']), pct(pilot['effect_on_served'], True)))
    w('')
    w('Where the 2.3x came from: served viewers were concentrated in easy goals.')
    w('')
    w('| Goal | Share of served | Share of all | Served success | Treatment success | Holdout success |')
    w('|---|---|---|---|---|---|')
    for g, v in pilot['by_goal'].items():
        w('| %s | %s%% | %s%% | %s%% | %s%% | %s%% |' % (g, pct(v['share_of_served']), pct(v['share_of_all']),
                                                     pct(v['served']), pct(v['treatment']), pct(v['holdout'])))
    w('')
    w('## Guardrails (from the simulator report)')
    w('')
    c = sim_report.get('counts', {})
    w('| Guardrail | Value |')
    w('|---|---|')
    w('| Policy problems flagged by the simulator | %d |' % sim_report.get('policy_problem_count', 0))
    w('| Actions on holdout viewers | %d |' % c.get('holdout_actions_ignored', 0))
    for k in sorted(c):
        if 'opt_out' in k or 'blocked' in k or 'rejected' in k or 'undeliverable' in k or 'not_shown' in k \
                or 'dropped' in k:
            w('| %s | %d |' % (k, c[k]))
    if policy_audit is not None:
        w('| Problems found by our independent audit (`oua audit`) | %d |' % policy_audit)
    w('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    return ests
