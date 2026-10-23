"""`oua learn`: turns one day's measured results into policy for the next day.

Two things are learned across viewers (per-viewer learnings such as suppressed channels already live in viewer state):

1. The organic baseline is updated with the day's holdout outcomes. Holdout viewers got nothing, so their goals show
   what happens without the agent in the current environment. The pre-replay prior (pilot + history) comes from a
   different period, so it is capped at PRIOR_CAP pseudo-observations: recent evidence can move it.
2. A goal type pursued for at least MIN_N viewers per arm whose measured lift (agent arm minus holdout, same goal) has
   a 95% upper bound below MIN_LIFT gets its score halved: there is direct evidence the agent's interventions do not
   move it. Goals without enough data keep their score.

Whether step 1 improves predictions is checked out of sample: the holdout is split in two by viewer, the baseline is
learned from one half and scored (Brier) on the other, both ways round.

Output: learned.json, used by `oua replay --learned learned.json`.
"""
import hashlib
import json
import math
from collections import defaultdict

from .baseline import SMOOTHING

PRIOR_CAP = 100
MIN_N = 100
MIN_LIFT = 0.03


def update_baseline(prior_table, rows):
    """Adds the holdout goals in `rows` to a copy of the baseline table. Returns (table, changes)."""
    table = json.loads(json.dumps(prior_table))
    obs = defaultdict(lambda: [0, 0])
    obs_b = defaultdict(lambda: [0, 0])
    for r in rows:
        if r['shadow']:
            obs[r['goal_id']][0] += r['ok']
            obs[r['goal_id']][1] += 1
            if r.get('bucket'):
                obs_b[(r['goal_id'], r['bucket'])][0] += r['ok']
                obs_b[(r['goal_id'], r['bucket'])][1] += 1
    changes = {}
    for gid, (k, n) in obs.items():
        t = table.setdefault(gid, {'p': None, 'n': 0, 'buckets': {}})
        old = t.get('p')
        m = min(t.get('n') or 0, PRIOR_CAP)
        new = (k + m * old) / (n + m) if old is not None else k / n
        t.update(p=round(new, 4), n=n + m, source='%s; updated with %d holdout goals (prior capped at %d)' % (
            t.get('source') or 'no prior', n, PRIOR_CAP))
        changes[gid] = {'before': old, 'after': round(new, 4), 'holdout_observed': round(k / n, 4), 'n': n}
        for (g, b), (kb, nb) in obs_b.items():
            if g != gid:
                continue
            bt = t.setdefault('buckets', {}).setdefault(b, {'p': new, 'n': 0})
            mb = min(bt['n'], SMOOTHING)
            bt.update(p=round((kb + mb * bt['p']) / (nb + mb), 4), n=nb + mb)
    return table, changes


def predict_from_table(table, gid, bucket):
    """The prediction OrganicBaseline makes from a saved table (same smoothing)."""
    t = table.get(gid) or {}
    prior = t.get('p')
    b = (t.get('buckets') or {}).get(bucket) if bucket else None
    if prior is None or not b or not b.get('n'):
        return prior
    if bucket.startswith('success='):
        return b['p']
    k = int(round(b['p'] * b['n']))
    return (k + SMOOTHING * prior) / (b['n'] + SMOOTHING)


def _half(viewer_id):
    return int(hashlib.sha256(('oos|' + viewer_id).encode()).hexdigest(), 16) % 2


def out_of_sample(rows, prior_table):
    """Two-fold check, split by viewer: learn the baseline from one half of the holdout, predict the other half.
    Compared with the pre-replay prediction and with a constant (the training half's overall success rate)."""
    hold = [r for r in rows if r['shadow'] and r.get('viewer_id')]
    folds = [[r for r in hold if _half(r['viewer_id']) == i] for i in (0, 1)]
    if not all(folds):
        return None
    names = ('prior', 'learned', 'constant')
    sq = defaultdict(list)
    per_goal = defaultdict(lambda: defaultdict(list))
    for i in (0, 1):
        train, test = folds[i], folds[1 - i]
        table, _ = update_baseline(prior_table, train)
        const = sum(r['ok'] for r in train) / len(train)
        for r in test:
            pl = predict_from_table(table, r['goal_id'], r.get('bucket'))
            if pl is None:
                pl = r['p']
            g = per_goal[r['goal_id']]
            for name, p in zip(names, (r['p'], pl, const)):
                sq[name].append((p - r['ok']) ** 2)
                g['brier_' + name].append((p - r['ok']) ** 2)
                g['p_' + name].append(p)
            g['ok'].append(r['ok'])
    mean = lambda xs: round(sum(xs) / len(xs), 4)
    return {'n_test': len(sq['prior']), 'split': 'by viewer (sha256 of viewer id), two folds',
            'brier': {k: mean(sq[k]) for k in names},
            'by_goal': {gid: {'n': len(g['ok']), 'observed': mean(g['ok']),
                              'mean_p': {k: mean(g['p_' + k]) for k in names},
                              'brier': {k: mean(g['brier_' + k]) for k in names}}
                        for gid, g in sorted(per_goal.items())}}


def learn(ctx, rows, prior_table):
    """rows: judged goals from impact.judged_goals (goal_id, viewer_id, shadow, p, ok, bucket, abstained).
    Holdout rows, including goals left alone below the floor, update the baseline; only pursued goals measure lift."""
    table, changes = update_baseline(prior_table, rows)
    lifts, multipliers = {}, {}
    pursued = [r for r in rows if not r.get('abstained')]   # lift compares goals the planner pursued in both arms
    for gid in sorted({r['goal_id'] for r in pursued}):
        a = [r['ok'] for r in pursued if r['goal_id'] == gid and not r['shadow']]
        h = [r['ok'] for r in pursued if r['goal_id'] == gid and r['shadow']]
        if not a or not h:
            continue
        pa, ph = sum(a) / len(a), sum(h) / len(h)
        se = math.sqrt(pa * (1 - pa) / len(a) + ph * (1 - ph) / len(h))
        lift = {'agent': round(pa, 4), 'holdout': round(ph, 4), 'diff': round(pa - ph, 4),
                'upper95': round(pa - ph + 1.96 * se, 4), 'n_agent': len(a), 'n_holdout': len(h)}
        lifts[gid] = lift
        if len(a) >= MIN_N and len(h) >= MIN_N and lift['upper95'] < MIN_LIFT:
            multipliers[gid] = {'multiplier': 0.5, 'why': 'pursued for %d/%d viewers; measured lift %+.1f pp, 95%% '
                                'upper bound %+.1f pp < %.0f pp: the interventions do not move this goal' % (
                                    len(a), len(h), 100 * lift['diff'], 100 * lift['upper95'], 100 * MIN_LIFT)}
    return {'baseline_table': table, 'baseline_changes': changes, 'goal_lift': lifts,
            'goal_multipliers': multipliers, 'out_of_sample': out_of_sample(rows, prior_table),
            'rules': {'prior_cap': PRIOR_CAP, 'min_n_per_arm': MIN_N, 'min_lift': MIN_LIFT}}
