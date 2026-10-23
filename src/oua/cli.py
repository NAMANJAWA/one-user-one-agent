"""oua: One User, One Agent.

  python main.py run-all  --pack ../one_user_one_agent_pack --out sample_run    # everything below, in order
  python main.py replay   --pack P --out RUN          # agent over the replay day -> decisions, actions, state
  python main.py simulate --pack P --run RUN          # pack simulator on our actions -> RUN/sim
  python main.py judge    --pack P --run RUN          # goal outcomes + learnings -> outcomes.jsonl, viewer_state/
  python main.py audit    --pack P --run RUN          # independent policy audit -> audit_report.json
  python main.py impact   --pack P --run RUN          # ITT vs holdout -> impact_report.md
  python main.py report   --pack P --run RUN          # final_report.md + audit_viewer.html
  python main.py explain  U-01991 --run RUN [--at 2026-10-01T08:00:00Z]
  python main.py perturb  --pack P --out UNSEEN_PACK [--seed s] [--load 3]   # unseen-day / stress test
  python main.py learn    --run RUN                    # day's results -> RUN/learned.json for the next day
  python main.py replay   --pack DAY2 --out RUN2 --learned RUN/learned.json --carry-state RUN
"""
import argparse
import json
import os
import sys

from .pack import load_json  # noqa: E402

# The default pack sits next to the repo, wherever the command is run from (not relative to the shell's cwd).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PACK = os.environ.get('OUA_PACK', os.path.join(REPO_ROOT, '..', 'one_user_one_agent_pack'))


def _check_inputs(a):
    """Fail early with a readable message instead of a traceback deep inside a loader."""
    pack = getattr(a, 'pack', None)
    if pack is not None and not os.path.exists(os.path.join(pack, 'config', 'goals.json')):
        sys.exit('oua: no evidence pack at %s (expected config/goals.json). Pass --pack PATH or set OUA_PACK.'
                 % os.path.abspath(pack))
    if getattr(a, 'provider', 'mock') == 'claude':
        import importlib.util
        if importlib.util.find_spec('anthropic') is None:
            sys.exit('oua: --provider claude needs the Anthropic SDK (pip install anthropic) and credentials. '
                     'The default --provider mock needs nothing.')


def _cfg(a):
    from .runtime import load_config
    return load_config(getattr(a, 'config', None))


def cmd_replay(a):
    from .runtime import Replay
    learned = load_json(a.learned) if getattr(a, 'learned', None) else None
    r = Replay(a.pack, a.out, _cfg(a), provider=a.provider, learned=learned,
               carry_state=getattr(a, 'carry_state', None)).run()
    m = r.write()
    r.close()
    print('replay: %d decisions, %d actions in %.1f s (setup %.1f s) -> %s' % (
        m['decisions'], m['actions'], m['replay_wall_s'], m['setup_s'], a.out), file=sys.stderr)
    return 0


def cmd_simulate(a):
    from .impact import run_simulator
    from .pack import Pack
    pack = Pack(a.pack)
    out = os.path.join(a.run, 'sim')
    run_simulator(pack, os.path.join(a.run, 'actions.jsonl'), out, a.seed)
    rep = load_json(os.path.join(out, 'simulator_report.json'))
    print('simulate: seed %s, %d events, %d policy problems -> %s' % (
        a.seed, rep['events_written'], rep['policy_problem_count'], out), file=sys.stderr)
    return 0


def cmd_judge(a):
    from collections import Counter
    from .context import load_ctx
    from .judge import Judge, load_outcomes, observation_end
    from .report import export_viewer_state
    from .timeutil import iso, parse
    ctx = load_ctx(a.pack, a.run, _cfg(a))
    store = load_outcomes(os.path.join(a.run, 'sim'))
    end = observation_end(store, parse(ctx.manifest['replay_start']))
    results = Judge(ctx, store, end).run()
    with open(os.path.join(a.run, 'outcomes.jsonl'), 'w', encoding='utf-8') as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n')
    n = export_viewer_state(ctx, os.path.join(a.run, 'viewer_state'))
    c = Counter(('shadow' if r['shadow'] else 'agent', r['status']) for r in results)
    print('judge: %d goal instances judged at %s %s; %d viewer state files' % (
        len(results), iso(end), dict(sorted(c.items())), n), file=sys.stderr)
    return 0


def cmd_impact(a):
    from .context import load_ctx
    from .impact import (Population, calibration, followup_experiment, judged_goals, multi_seed, pilot_reanalysis,
                         write_report)
    ctx = load_ctx(a.pack, a.run, _cfg(a))
    pop = Population(ctx, a.run, os.path.join(a.run, 'sim'))
    cal = calibration(judged_goals(ctx, pop.out))
    fx = followup_experiment(ctx, a.run, pop.out)
    seeds = multi_seed(ctx, a.run, ['seed-%d' % i for i in range(1, a.seeds + 1)]) if a.seeds else []
    sim_report = load_json(os.path.join(a.run, 'sim', 'simulator_report.json'))
    audit_path = os.path.join(a.run, 'audit_report.json')
    audit_n = load_json(audit_path)['problem_count'] if os.path.exists(audit_path) else None
    ests = write_report(os.path.join(a.run, 'impact_report.md'), pop, pilot_reanalysis(ctx.pack), seeds,
                        sim_report, audit_n, cal, fx)
    data = {'estimates': ests, 'seeds': seeds, 'balance': pop.balance(), 'by_goal': pop.by_goal(),
            'naive_play_24h': pop.naive('play_24h'), 'calibration': cal, 'followup_experiment': fx}
    with open(os.path.join(a.run, 'impact.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    e = ests['play_24h']
    print('impact: play_24h ITT %+.1f pp (95%% CI %+.1f to %+.1f) n=%d/%d -> %s' % (
        100 * e['itt'], 100 * e['ci95'][0], 100 * e['ci95'][1], e['n_agent'], e['n_holdout'],
        os.path.join(a.run, 'impact_report.md')), file=sys.stderr)
    return 0


def cmd_audit(a):
    from .audit import audit
    rep = audit(a.pack, a.run)
    print('audit: %s checked, %d problems %s' % (rep['checked'], rep['problem_count'],
                                                {k: v['count'] for k, v in rep['problems'].items()}),
          file=sys.stderr)
    return 1 if rep['problem_count'] and getattr(a, 'strict', False) else 0


def cmd_report(a):
    from .context import load_ctx
    from .report import write_final_report, write_html_viewer
    ctx = load_ctx(a.pack, a.run, _cfg(a))
    write_final_report(ctx, a.run)
    write_html_viewer(ctx, a.run)
    print('report: final_report.md, audit_viewer.html -> %s' % a.run, file=sys.stderr)
    return 0


def cmd_explain(a):
    from .explain import explain
    print(explain(a.run, a.viewer, a.at))
    return 0


def cmd_learn(a):
    from .context import load_ctx
    from .impact import judged_goals
    from .judge import load_outcomes
    from .learn import learn
    ctx = load_ctx(a.pack, a.run, _cfg(a))
    store = load_outcomes(os.path.join(a.run, 'sim'))
    out = learn(ctx, judged_goals(ctx, store), load_json(os.path.join(a.run, 'baseline_table.json')))
    path = os.path.join(a.run, 'learned.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1)
    ctx.memory.close()
    print('learn: baseline %s; goal multipliers %s -> %s' % (
        {g: '%s->%s' % (v['before'], v['after']) for g, v in out['baseline_changes'].items()},
        {g: v['multiplier'] for g, v in out['goal_multipliers'].items()}, path), file=sys.stderr)
    return 0


def cmd_two_day(a):
    from .twoday import two_day
    if not os.path.exists(os.path.join(a.run, 'learned.json')):
        cmd_learn(a)
    s = two_day(a.pack, a.run, _cfg(a), work=a.work)
    print('two-day: %d day-2 decisions changed by learning; audits %s -> %s' % (
        sum(c['n'] for c in s['changed']), s['audit_problems'], os.path.join(a.run, 'learning_report.md')),
        file=sys.stderr)
    return 0


def cmd_perturb(a):
    from .perturb import perturb
    s = perturb(a.pack, a.out, a.seed, a.load, a.peak_load, a.shift_days, a.correct_goal)
    print('perturb: %d triggers (%d extra load), peak hour %s UTC, withdrawn %s -> %s' % (
        s['triggers'], s['extra_load_triggers'], s['peak_hour_utc'],
        [c.get('title_id') for c in s['controls'] if c['type'] == 'title_withdrawn'], a.out), file=sys.stderr)
    return 0


def cmd_run_all(a):
    a.run = a.out
    a.work = getattr(a, 'work', None)
    steps = [cmd_replay, cmd_simulate, cmd_judge, cmd_audit, cmd_impact, cmd_learn]
    if getattr(a, 'two_day', False):
        steps.append(cmd_two_day)
    steps.append(cmd_report)
    for step in steps:
        rc = step(a)
        if rc:
            return rc
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='oua', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def add(name, fn, run=True, help_=None):
        p = sub.add_parser(name, help=help_)
        p.add_argument('--pack', default=DEFAULT_PACK, help='path to the data pack (default: %(default)s)')
        p.add_argument('--config', default=None, help='agent config JSON (default: config/agent.json)')
        if run:
            p.add_argument('--run', default='sample_run', help='run directory (default: %(default)s)')
        p.set_defaults(fn=fn)
        return p

    p = add('replay', cmd_replay, run=False, help_='run the agent over the replay day')
    p.add_argument('--out', default='sample_run')
    p.add_argument('--provider', default='mock', choices=['mock', 'claude'])
    p.add_argument('--learned', default=None, help="learned.json from an earlier day's `oua learn`")
    p.add_argument('--carry-state', dest='carry_state', default=None,
                   help='an earlier run directory: start from its agent memory and treat its outcomes as history')
    add('learn', cmd_learn, help_="update the baseline and goal policy from a judged run -> RUN/learned.json")
    p = add('two-day', cmd_two_day, help_='replay an unseen day 2 with and without what day 1 taught the agent')
    p.add_argument('--work', default=None, help='where the day-2 runs go (default: system temp; not the repo)')
    p = add('simulate', cmd_simulate, help_='run the pack simulator on the actions of a run')
    p.add_argument('--seed', default='oua-practice')
    add('judge', cmd_judge, help_='judge goal outcomes and write learnings')
    p = add('impact', cmd_impact, help_='measure impact against the randomised holdout')
    p.add_argument('--seeds', type=int, default=0, help='extra simulator seeds for the robustness check')
    p = add('audit', cmd_audit, help_='independent policy audit of a run')
    p.add_argument('--strict', action='store_true', help='exit 1 if any problem is found')
    add('report', cmd_report, help_='write final_report.md and audit_viewer.html')
    p = sub.add_parser('explain', help='what the agent knew, decided and observed for one viewer')
    p.add_argument('viewer')
    p.add_argument('--run', default='sample_run')
    p.add_argument('--at', default=None, help='ISO time; show only what was known by then')
    p.set_defaults(fn=cmd_explain)
    p = add('perturb', cmd_perturb, run=False, help_='build an unseen replay day (different viewers, times, faults)')
    p.add_argument('--out', required=True, help='directory for the new pack')
    p.add_argument('--seed', default='unseen-1')
    p.add_argument('--load', type=float, default=1.0, help='traffic multiplier for the whole day')
    p.add_argument('--peak-load', type=float, default=3.0, help='extra multiplier in the busiest hour')
    p.add_argument('--shift-days', type=int, default=0)
    p.add_argument('--correct-goal', dest='correct_goal', default=None,
                   help='aim the rule correction at this goal (stress variant)')
    p = add('run-all', cmd_run_all, run=False, help_='replay, simulate, judge, audit, impact, report')
    p.add_argument('--out', default='sample_run')
    p.add_argument('--provider', default='mock', choices=['mock', 'claude'])
    p.add_argument('--seed', default='oua-practice')
    p.add_argument('--seeds', type=int, default=0)
    p.add_argument('--strict', action='store_true')
    p.add_argument('--two-day', dest='two_day', action='store_true', help='also run the two-day learning demo')
    a = ap.parse_args(argv)
    _check_inputs(a)
    return a.fn(a)
