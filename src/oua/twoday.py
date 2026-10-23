"""`oua two-day`: shows learning changing decisions on a second, unseen day.

Day 1 is a judged run (replay, simulate, judge). Day 2 is an unseen day built by `perturb`, dated 15 days later so that
everything learned from day 1's 14-day outcome window is genuinely in the past. Day 2 is replayed twice from the
same starting point (day 1's agent memory, with day 1's outcomes as history):

  carry_only : the day-1 baseline and goal policy, unchanged
  learned    : `oua learn` applied (baseline updated with day 1's holdout outcomes, goal multipliers)

Everything else being equal, the differences between the two are the effect of what was learned across viewers.
Per-viewer learning (suppressed channels, goal adjustments) is in the memory both runs start from, and is counted.
The day-2 runs live in a work directory outside the repo (they are large and the perturbed pack is personal data)."""
import json
import os
import tempfile
from collections import Counter

from .audit import audit
from .pack import load_json, read_jsonl
from .perturb import perturb
from .runtime import Replay

FIXED_COMPUTE_MS = 5   # about the median measured decision compute time on the sample day


def _label(d):
    g = d.get('goal') or {}
    r = d.get('no_action_reason') or ''
    if d['status'] == 'PUBLISHED' and g.get('committed') == 'new':
        return 'goal ' + g['id']
    if d['status'] == 'PUBLISHED' and g.get('committed') == 'continuing':
        return 'continue goal ' + g['id']
    if r.startswith('not worth'):
        return 'not worth an intervention'
    if 'still live' in r or r.startswith('already acted in this session'):
        return 'earlier page still live'
    return d['status'].lower().replace('_', ' ')


def _first_decisions(run_dir):
    out = {}
    for d in read_jsonl(os.path.join(run_dir, 'decisions.jsonl')):
        if d['kind'] == 'trigger' and d['arm'] == 'agent' and d['status'] != 'DUPLICATE_IGNORED':
            out.setdefault(d['trigger']['event_id'], d)
    return out


def two_day(pack_dir, run_dir, cfg, work=None, seed='day2'):
    work = work or os.path.join(tempfile.gettempdir(), 'oua_two_day')
    learned_path = os.path.join(run_dir, 'learned.json')
    learned = load_json(learned_path)
    day2_pack = os.path.join(work, 'day2_pack')
    perturb(pack_dir, day2_pack, seed=seed, shift_days=15)
    # Both runs use a fixed compute time so the virtual clock, and therefore the comparison, reproduces exactly.
    cfg = json.loads(json.dumps(cfg))
    cfg['latency']['fixed_compute_ms'] = FIXED_COMPUTE_MS
    runs = {}
    for name, use_learned in (('carry_only', None), ('learned', learned)):
        out = os.path.join(work, name)
        r = Replay(day2_pack, out, json.loads(json.dumps(cfg)), learned=use_learned, carry_state=run_dir).run()
        r.write()
        r.close()
        runs[name] = {'dir': out, 'audit': audit(day2_pack, out)['problem_count'],
                      'decisions': _first_decisions(out)}
    a, b = runs['carry_only']['decisions'], runs['learned']['decisions']
    changed = Counter()
    examples = {}
    reason_only = 0   # nothing done either way; only the recorded reason for doing nothing differs
    for eid, d in b.items():
        if eid in a and _label(a[eid]) != _label(d):
            k = (_label(a[eid]), _label(d))
            changed[k] += 1
            reason_only += a[eid]['action'] == 'NO_ACTION' and d['action'] == 'NO_ACTION'
            examples.setdefault(k, (d['decision_id'], d['viewer_id'], (d.get('no_action_reason') or '')[:200]))
    mix = {n: Counter(_label(d) for d in runs[n]['decisions'].values()) for n in runs}
    per_viewer = {
        'decisions_using_goal_adjustments': sum(1 for d in b.values()
                                                if (d.get('goal_selection') or {}).get('learned_adjustments')),
        'messages_blocked_by_learned_channel_suppression': sum(
            1 for d in b.values() if 'suppressed by what was learned' in str((d.get('why_not') or {}).get('message'))),
        'viewers_on_day2_with_day1_state': sum(1 for d in b.values() if d.get('goal_selection', {}).get(
            'previous_goal') or (d.get('goal_selection') or {}).get('learned_adjustments')),
    }
    summary = {'work_dir': work, 'fixed_compute_ms': FIXED_COMPUTE_MS,
               'learned': {k: learned.get(k) for k in ('baseline_changes', 'goal_lift', 'goal_multipliers',
                                                       'out_of_sample', 'rules')},
               'audit_problems': {n: runs[n]['audit'] for n in runs},
               'decision_mix': {n: dict(mix[n].most_common()) for n in mix},
               'changed': [{'from': k[0], 'to': k[1], 'n': n, 'example': examples[k]}
                           for k, n in changed.most_common()],
               'changed_reason_only': reason_only,
               'per_viewer_learning': per_viewer}
    with open(os.path.join(run_dir, 'learning_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=1)
    write_learning_report(os.path.join(run_dir, 'learning_report.md'), summary)
    return summary


def write_learning_report(path, s):
    L = []
    w = L.append
    w('# Learning report: day 1 results changing day 2 decisions')
    w('')
    w('Generated by `oua two-day`. Day 2 is an unseen day (`oua perturb --shift-days 15`), replayed twice from day 1\'s '
      'agent memory with day 1\'s outcomes as history: once with the day-1 policy, once with what `oua learn` derived '
      'from day 1. Only the learned policy differs between the two runs.')
    w('')
    w('## What was learned from day 1')
    w('')
    w('Organic baseline (probability of reaching the goal without the agent), updated with day 1\'s holdout goals:')
    w('')
    w('| Goal | Before | Holdout observed on day 1 (n) | After |')
    w('|---|---|---|---|')
    for g, v in sorted(s['learned']['baseline_changes'].items()):
        w('| %s | %s | %.1f%% (%d) | %.1f%% |' % (g, '%.1f%%' % (100 * v['before']) if v['before'] is not None
                                                 else 'n/a', 100 * v['holdout_observed'], v['n'], 100 * v['after']))
    w('')
    oos = s['learned'].get('out_of_sample')
    if oos:
        b = oos['brier']
        w('Does the update improve predictions? Checked out of sample: the holdout is split in two by viewer, the '
          'baseline is updated from one half and scored on the other, both ways round (%d goals scored). Brier score, '
          'lower is better:' % oos['n_test'])
        w('')
        w('| Prediction | Brier |')
        w('|---|---|')
        w('| Pre-replay baseline (pilot + history) | %.3f |' % b['prior'])
        w('| Updated with the other half\'s holdout outcomes | %.3f |' % b['learned'])
        w('| Constant: the other half\'s overall success rate | %.3f |' % b['constant'])
        w('')
        w('| Goal | n | Observed | Mean p before | Mean p after | Brier before | Brier after |')
        w('|---|---|---|---|---|---|---|')
        for g, v in oos['by_goal'].items():
            w('| %s | %d | %.1f%% | %.1f%% | %.1f%% | %.3f | %.3f |' % (
                g, v['n'], 100 * v['observed'], 100 * v['mean_p']['prior'], 100 * v['mean_p']['learned'],
                v['brier']['prior'], v['brier']['learned']))
        w('')
        w('The gain is almost all G07 and G09, the goals with the most holdout data; small cells barely move, by '
          'design (the prior counts as up to %d observations). The goal multiplier below is not checked this way: '
          'it rests on the measured lift alone.' % s['learned']['rules']['prior_cap'])
        w('')
    w('Measured lift by goal (agent arm vs holdout, same goal; both arms used the same planner):')
    w('')
    w('| Goal | Agent | Holdout | Lift | 95% upper bound | n agent / holdout |')
    w('|---|---|---|---|---|---|')
    for g, v in sorted(s['learned']['goal_lift'].items()):
        w('| %s | %.1f%% | %.1f%% | %+.1f pp | %+.1f pp | %d / %d |' % (
            g, 100 * v['agent'], 100 * v['holdout'], 100 * v['diff'], 100 * v['upper95'], v['n_agent'],
            v['n_holdout']))
    w('')
    if s['learned']['goal_multipliers']:
        for g, v in s['learned']['goal_multipliers'].items():
            w('- **%s score x%.1f**: %s.' % (g, v['multiplier'], v['why']))
    else:
        w('- No goal had enough evidence to change its score.')
    w('')
    w('## What changed on day 2')
    w('')
    total = sum(c['n'] for c in s['changed'])
    ro = s.get('changed_reason_only') or 0
    w('%d agent-arm decisions differ between the two day-2 runs: %d change what the viewer gets (a different goal, '
      'or a page instead of nothing, or the reverse); in %d the agent does nothing either way and only the recorded '
      'reason differs. Both runs advance the virtual clock by a fixed %d ms per decision and the unseen day is '
      'built from a fixed seed, so from the same day-1 memory they compare exactly. Day 1 itself uses measured '
      'compute times; its timestamps differ by milliseconds between fresh runs, which could in principle move '
      'these counts slightly.' % (
          total, total - ro, ro, s.get('fixed_compute_ms') or 0))
    w('')
    w('Goals the floor left alone on day 1 were tracked, so a prediction that was too low can be corrected; once the '
      'corrected score clears the floor, the agent pursues that goal on day 2. A changed first visit also changes '
      'what later visits by the same viewer do (continue the new goal, or leave its page live).')
    w('')
    w('| Without learning | With learning | Decisions | Example |')
    w('|---|---|---|---|')
    for c in s['changed'][:10]:
        w('| %s | %s | %d | %s (%s) |' % (c['from'], c['to'], c['n'], c['example'][0], c['example'][1]))
    w('')
    pv = s['per_viewer_learning']
    w('Per-viewer learning carried in memory (both runs): %d day-2 decisions applied a goal adjustment learned for '
      'that viewer; %d messages were not sent because a channel was learned to be unusable (invalid token or '
      'opt-out).' % (pv['decisions_using_goal_adjustments'], pv['messages_blocked_by_learned_channel_suppression']))
    w('')
    w('Independent audit of both day-2 runs: %s problems.' % s['audit_problems'])
    w('')
    w('## Limits')
    w('')
    w('- Day 2 has no simulated outcomes: the pack\'s simulator hard-codes the replay date, so whether the changed '
      'decisions did better is not measured here. A third day would judge them.')
    w('- Viewers not triggered on day 1 have no recorded activity between 30 September and day 2, so many look '
      'absent and qualify for "return after absence" (G12). That is a gap in the data, not a choice of the agent.')
    w('- One day of holdout data moves the baseline only where the evidence is large (G07, n=361); small cells barely '
      'move, by design.')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
