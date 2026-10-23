"""`oua perturb`: builds an unseen replay day from the pack, to show nothing is tuned to the sample day.

The evaluators run a second, unseen replay day. This generator makes one: it hands every trigger to a different
viewer (so different histories, profiles, arms and dialect conflicts), jitters times, withdraws a different popular
title at a different time, moves the rule correction and outages, re-targets the model faults at other triggers, and
can multiply traffic (`--load 3`) to stress the worker pool and load shedding.

History, catalogue, profiles and config are hard-linked (or copied) unchanged; only replay/ is rewritten.
"""
import json
import os
import random
import shutil
import uuid
from collections import Counter
from datetime import timedelta

from .pack import load_csv, load_json, read_jsonl
from .timeutil import iso, parse

TIME_FIELDS = ('occurred_at', 'sent_at', 'received_at', 'deliver_at')


def _link_or_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _shift(e, delta):
    e = dict(e)
    for k in TIME_FIELDS:
        if e.get(k):
            e[k] = iso(parse(e[k]) + delta)
    return e


def perturb(pack_dir, out_dir, seed='unseen-1', load=1.0, peak_load=3.0, shift_days=0, correct_goal=None):
    rng = random.Random(seed)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    for sub in ('data', 'config', 'pilot', 'simulator'):
        src = os.path.join(pack_dir, sub)
        for name in os.listdir(src):
            _link_or_copy(os.path.join(src, name), os.path.join(out_dir, sub, name))
    _link_or_copy(os.path.join(pack_dir, 'replay', 'arms.csv'), os.path.join(out_dir, 'replay', 'arms.csv'))
    users = sorted(r['user_id'] for r in load_csv(os.path.join(pack_dir, 'replay', 'arms.csv')))
    perm = users[:]
    rng.shuffle(perm)
    remap = dict(zip(users, perm))
    # The pack's simulator hard-codes the replay date, so by default the day keeps its date; everything else moves.
    day = timedelta(days=shift_days)
    triggers = [json.loads(json.dumps(t)) for t in read_jsonl(os.path.join(pack_dir, 'replay', 'triggers.jsonl'))]
    jitter = {}
    out_tr = []
    for t in triggers:
        # same event id => same jitter, so redelivered duplicates stay duplicates
        j = jitter.setdefault(t['event_id'], timedelta(seconds=rng.randint(-1200, 1200)))
        e = _shift(t, day + j)
        e['user_id'] = remap[t['user_id']]
        # New ids, consistent within the day (a redelivered trigger keeps its copy's id): an unseen day never
        # reuses the sample day's event ids, so a run that carries state over is not fooled into "duplicates".
        e['event_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, '%s:%s' % (seed, t['event_id'])))
        e['session_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, '%s:s:%s' % (seed, t['session_id'])))
        out_tr.append(e)
    # extra load: copies of fresh triggers for other viewers at the same moments
    extra = []
    fresh = [t for t in out_tr if (parse(t['sent_at']) - parse(t['occurred_at'])).total_seconds() < 60]
    peak_hour = Counter(parse(t['deliver_at']).strftime('%H') for t in fresh).most_common(1)[0][0]
    for t in fresh:
        k = load * (peak_load if parse(t['deliver_at']).strftime('%H') == peak_hour else 1.0)
        n = int(k) - 1 + (1 if rng.random() < (k - int(k)) else 0)
        for i in range(max(0, n)):
            # Ids derived from the seed, not random: the same seed must build the same day (simulated store latency
            # is derived from the event id, so random ids would change timing and decisions from run to run).
            cid = '%s:copy%d:%s' % (seed, i, t['event_id'])
            c = dict(t, event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, cid)),
                     session_id=str(uuid.uuid5(uuid.NAMESPACE_URL, cid + ':s')), user_id=rng.choice(users))
            c = _shift(c, timedelta(seconds=rng.randint(0, 50)))
            extra.append(c)
    out_tr = sorted(out_tr + extra, key=lambda e: e['deliver_at'])
    live = [_shift(dict(e, user_id=remap[e['user_id']],
                        event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, '%s:%s' % (seed, e['event_id']))),
                        session_id=str(uuid.uuid5(uuid.NAMESPACE_URL, '%s:s:%s' % (seed, e.get('session_id'))))),
                   day + timedelta(seconds=rng.randint(-600, 600)))
            for e in read_jsonl(os.path.join(pack_dir, 'replay', 'live_events.jsonl'))]
    live.sort(key=lambda e: e['deliver_at'])
    cat = load_csv(os.path.join(pack_dir, 'data', 'catalogue.csv'))
    controls = []
    for c in read_jsonl(os.path.join(pack_dir, 'replay', 'control_events.jsonl')):
        c = dict(c)
        move = day + timedelta(minutes=rng.randint(-180, 180))
        c['at'] = iso(parse(c['at']) + move)
        if c.get('until'):
            c['until'] = iso(parse(c['until']) + move)
        if c['type'] == 'title_withdrawn':
            c['title_id'] = rng.choice([r['title_id'] for r in cat if not r['available_until']
                                        and r['dialect'] in ('d1', 'd2')])
        if c['type'] == 'goal_rule_corrected' and correct_goal:
            # Stress variant: aim the correction at a goal that is actually open in volume (on the sample day only
            # G03 is corrected and almost no G03 goal is open), so the correction path runs at scale with outcomes.
            goals = {g['goal_id']: g for g in load_json(os.path.join(pack_dir, 'config', 'goals.json'))['goals']}
            old = goals[correct_goal]['success']
            c.update(goal_id=correct_goal, old_success={'event': old['event'], 'condition': old['condition']},
                     new_success={'event': 'episode_completed', 'condition': old['condition']},
                     note='stress variant generated by oua perturb: finishing an episode, not just starting one')
        controls.append(c)
    controls.sort(key=lambda c: c['at'])
    faults = []
    for f in read_jsonl(os.path.join(pack_dir, 'replay', 'model_faults.jsonl')):
        t = rng.choice(fresh)
        faults.append(dict(f, trigger_event_id=t['event_id'], user_id=t['user_id']))

    def dump(name, rows):
        with open(os.path.join(out_dir, 'replay', name), 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')
    dump('triggers.jsonl', out_tr)
    dump('live_events.jsonl', live)
    dump('control_events.jsonl', controls)
    dump('model_faults.jsonl', faults)
    summary = {'seed': seed, 'triggers': len(out_tr), 'extra_load_triggers': len(extra), 'peak_hour_utc': peak_hour,
               'controls': controls, 'faults': faults}
    with open(os.path.join(out_dir, 'PERTURBATION.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    return summary
