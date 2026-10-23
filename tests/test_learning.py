"""Learning across viewers (oua learn) and across days (replay --carry-state --learned)."""
import json
import os
import unittest

from helpers import MiniPack, ev, ist, series_viewer, trig
from oua.judge import Judge, load_outcomes, observation_end
from oua.learn import learn
from oua.perturb import perturb
from oua.runtime import Replay, load_config
from oua.timeutil import parse


def rows(goal, n, agent_ok, holdout_ok, p=0.27, bucket='ep1/3-6d', **extra):
    out = []
    for i in range(n):
        out.append(dict(goal_id=goal, viewer_id='U-A%05d' % i, shadow=False, p=p, ok=int(i < agent_ok),
                        bucket=bucket, **extra))
        out.append(dict(goal_id=goal, viewer_id='U-H%05d' % i, shadow=True, p=p, ok=int(i < holdout_ok),
                        bucket=bucket, **extra))
    return out


class LearnTests(unittest.TestCase):
    def test_baseline_moves_toward_holdout_and_unmovable_goal_is_halved(self):
        prior = {'G07': {'p': 0.27, 'n': 10000, 'source': 'history', 'buckets': {'ep1/3-6d': {'p': 0.25, 'n': 3000}}},
                 'G01': {'p': 0.33, 'n': 700, 'source': 'history', 'buckets': {}}}
        out = learn(None, rows('G07', 360, 12, 16) + rows('G01', 50, 21, 15), prior)
        g7 = out['baseline_changes']['G07']
        self.assertAlmostEqual(g7['holdout_observed'], 16 / 360, places=4)
        self.assertLess(g7['after'], 0.12, 'the 10,000-row prior from another period is capped, so 360 new rows move it')
        self.assertIn('G07', out['goal_multipliers'])
        self.assertNotIn('G01', out['goal_multipliers'], '50 per arm is too little evidence to change a goal')
        self.assertGreater(out['goal_lift']['G01']['diff'], 0)


    def test_goals_left_alone_correct_the_baseline_but_do_not_count_as_lift(self):
        # The floor skips G05 everywhere (predicted 0.6%); without tracking what happened to the skipped goals the
        # prediction could never be corrected. They update the baseline, and since nothing was pursued, no lift.
        prior = {'G05': {'p': 0.0056, 'n': 1073, 'source': 'pilot', 'buckets': {}}}
        out = learn(None, rows('G05', 150, 7, 7, p=0.0056, bucket=None, abstained=True), prior)
        self.assertGreater(out['baseline_changes']['G05']['after'], 0.02)
        self.assertNotIn('G05', out['goal_lift'])
        self.assertNotIn('G05', out['goal_multipliers'])

    def test_out_of_sample_check_scores_the_update_on_viewers_it_did_not_see(self):
        prior = {'G07': {'p': 0.27, 'n': 10000, 'source': 'history', 'buckets': {}}}
        oos = learn(None, rows('G07', 400, 16, 16, bucket=None), prior)['out_of_sample']
        self.assertEqual(oos['n_test'], 400, 'every holdout goal is scored once, by the model learned without it')
        self.assertLess(oos['brier']['learned'], oos['brier']['prior'])
        self.assertAlmostEqual(oos['by_goal']['G07']['mean_p']['prior'], 0.27, places=4)


class DeterminismTests(unittest.TestCase):
    def test_fixed_compute_time_makes_a_replay_reproduce_exactly(self):
        runs = []
        for _ in range(2):
            pk = MiniPack()
            try:
                for i in range(6):
                    uid = 'U-%05d' % (300 + i)
                    series_viewer(pk, uid)
                    # Fixed ids, as in the real pack: simulated store latency is derived from the event id.
                    pk.triggers += [trig(uid, ist(10, i), eid='E-%d' % i, session='S-%d' % i),
                                    trig(uid, ist(10, i), eid='E-%d' % i, session='S-%d' % i)]
                res = pk.run({'latency.fixed_compute_ms': 5})
                runs.append([(d['decision_id'], d['status'], d.get('published_at'),
                              (d.get('latency') or {}).get('compute_ms')) for d in res.decisions])
            finally:
                pk.cleanup()
        self.assertEqual(runs[0], runs[1])
        self.assertTrue(all(r[3] == 5 for r in runs[0] if r[3] is not None))


    def test_the_same_seed_builds_the_same_unseen_day(self):
        # Peak-hour copies used random ids, so every rebuilt day timed (and decided) a little differently.
        pk = MiniPack()
        try:
            for i in range(4):
                uid = 'U-%05d' % (310 + i)
                series_viewer(pk, uid)
                pk.triggers.append(trig(uid, ist(20, i), eid='E-%d' % i, session='S-%d' % i))
            pk.write()
            built = []
            for name in ('a', 'b'):
                out = os.path.join(pk.dir, name)
                perturb(pk.dir, out, seed='s', peak_load=3.0)
                with open(os.path.join(out, 'replay', 'triggers.jsonl'), encoding='utf-8') as f:
                    built.append(f.read())
            self.assertGreater(len(built[0].splitlines()), 4, 'peak-hour copies were made')
            self.assertEqual(built[0], built[1])
        finally:
            pk.cleanup()


class CarryStateTests(unittest.TestCase):
    def test_day_two_starts_from_day_one_memory_and_learned_policy(self):
        day1 = MiniPack()
        day2 = MiniPack()
        try:
            series_viewer(day1, 'U-00200')
            day1.triggers.append(trig('U-00200', ist(19)))
            res = day1.run()
            page = res.actions_for('U-00200', ['home_page'])[0]
            push = res.actions_for('U-00200', ['push'])[0]
            sim = os.path.join(res.out, 'sim')
            os.makedirs(sim)
            with open(os.path.join(sim, 'outcome_events.jsonl'), 'w') as f:
                for e in (ev('U-00200', 'home_page_shown', ist(19, 1), {'personalised': True,
                                                                       'page_action_id': page['action_id']}),
                          ev('U-00200', 'message_failed', parse(push['send_at']),
                             {'channel': 'push', 'reason': 'token_invalid', 'action_id': push['action_id']})):
                    f.write(json.dumps(e) + '\n')
            store = load_outcomes(sim)
            Judge(res.replay.ctx, store, observation_end(store, ist(0))).run()
            res.replay.close()
            day1._replays = []
            # Day 2, 16 days later, a separate process: same viewer, new visit, policy learned from day 1.
            series_viewer(day2, 'U-00200', days_ago=20)
            day2.triggers.append(trig('U-00200', ist(10, day=16)))
            day2.write()
            cfg = load_config()
            with open(os.path.join(res.out, 'baseline_table.json'), encoding='utf-8') as f:
                learned = {'baseline_table': json.load(f), 'goal_multipliers': {'G07': {'multiplier': 0.5,
                                                                                         'why': 'test'}}}
            out = os.path.join(day2.dir, 'run')
            r2 = Replay(day2.dir, out, cfg, learned=learned, carry_state=res.out).run()
            r2.write()
            d = [x for x in r2.decisions if x['kind'] == 'trigger'][0]
            st = r2.ctx.memory.load('U-00200')
            r2.close()
            self.assertTrue(st['channel_state']['push']['suppressed'], 'day 1 learning survived the restart')
            self.assertEqual(st['goal_adjustments'], {'G07': -0.1})
            sel = d['goal_selection']
            self.assertEqual(sel.get('learned_adjustments'), {'G07': -0.1})
            g7 = next(c for c in sel['considered'] if c['goal_id'] == 'G07')
            self.assertTrue(g7['eligible'])
            self.assertFalse(any(a['type'] == 'push' for a in r2.actions), 'push was learned to be unusable')
            self.assertEqual(g7.get('learned_multiplier', {}).get('multiplier'), 0.5)
        finally:
            day1.cleanup()
            day2.cleanup()


if __name__ == '__main__':
    unittest.main()
