"""Goal rules: a corrected success condition, measurability, 'never' claims, success matching, judging."""
import json
import os
import unittest
from datetime import timedelta

from helpers import MiniPack, ev, iso, ist, new_viewer, series_viewer, trig
from oua.context import load_ctx
from oua.events import normalize
from oua.goals import success_events
from oua.judge import Judge, load_outcomes, observation_end
from oua.runtime import load_config

CORRECTION = {'at': None, 'type': 'goal_rule_corrected', 'goal_id': 'G03',
              'old_success': {'event': 'app_open', 'condition': 'on a later IST calendar day within the window'},
              'new_success': {'event': 'play_started', 'condition': 'on a later IST calendar day within the window'},
              'applies_to': 'every G03 goal that is still open, including goals opened before this correction',
              'note': 'an app open alone was never meant to count as a return'}


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.pk = MiniPack()

    def tearDown(self):
        self.pk.cleanup()

    def _write_outcomes(self, run_dir, events):
        sim = os.path.join(run_dir, 'sim')
        os.makedirs(sim, exist_ok=True)
        with open(os.path.join(sim, 'outcome_events.jsonl'), 'w', encoding='utf-8') as f:
            for e in events:
                f.write(json.dumps(e) + '\n')
        return sim

    def test_corrected_rule_touches_only_open_goals_and_is_used_for_judging(self):
        # A and B are new viewers who already played and finished an episode, so G03 is their best goal.
        for uid in ('U-00040', 'U-00041'):
            new_viewer(self.pk, uid, signed_up_days_ago=1, played=True, completed=True)
        series_viewer(self.pk, 'U-00042')                       # unrelated G07 goal, must not be touched
        self.pk.triggers += [trig('U-00040', ist(9)), trig('U-00042', ist(9, 5)), trig('U-00041', ist(16))]
        self.pk.control.append(dict(CORRECTION, at=iso(ist(14))))
        res = self.pk.run()
        a, b = res.for_user('U-00040')[0], res.for_user('U-00041')[0]
        self.assertEqual(a['goal']['id'], 'G03')
        self.assertEqual(b['goal']['id'], 'G03')
        self.assertEqual(a['goal']['rule_version'], 'G03@2026-09-30', 'opened under the original rule')
        self.assertNotEqual(b['goal']['rule_version'], 'G03@2026-09-30', 'opened after: uses the corrected rule')
        impact = next(c for c in res.replay.control_impacts if c['type'] == 'goal_rule_corrected')
        self.assertEqual(impact['open_goals_affected'], 1)
        self.assertEqual(impact['viewers_affected'], ['U-00040'])

        # Judge: A only opened the app the next day (old rule: success; corrected rule: not a return).
        #        B played the next day (success under the corrected rule).
        sim = self._write_outcomes(res.out, [
            ev('U-00040', 'app_open', ist(11, day=1)),
            ev('U-00041', 'app_open', ist(18, day=1)),
            ev('U-00041', 'play_started', ist(18, 2, day=1), {'title_id': 'T0004', 'episode_id': 'slug-t0004-s1e1'}),
        ])
        ctx = load_ctx(self.pk.dir, res.out, load_config())
        store = load_outcomes(sim)
        results = {r['viewer_id']: r for r in Judge(ctx, store, observation_end(store, ist(0))).run()}
        ctx.memory.close()
        self.assertEqual(results['U-00040']['status'], 'failed')
        self.assertIn('corrected rule', results['U-00040']['rule_note'])
        self.assertEqual(results['U-00041']['status'], 'succeeded')
        self.assertEqual(results['U-00042']['rule_version_used'], 'G07@2026-09-30')

    def test_unmeasurable_goals_are_never_chosen(self):
        series_viewer(self.pk, 'U-00043')
        self.pk.triggers.append(trig('U-00043', ist(10)))
        res = self.pk.run()
        considered = {c['goal_id']: c for c in res.for_user('U-00043')[0]['goal_selection']['considered']}
        self.assertFalse(considered['G10']['measurable'])
        self.assertIn('names no event', considered['G10']['reason'])
        self.assertFalse(considered['G11']['measurable'])
        self.assertIn('tv_app_open', considered['G11']['reason'])

    def test_never_claims_need_the_whole_history(self):
        series_viewer(self.pk, 'U-00044', signup='2025-03-01T00:00:00Z')    # signed up long before the window
        self.pk.triggers.append(trig('U-00044', ist(10)))
        d = self.pk.run().for_user('U-00044')[0]
        g02 = next(c for c in d['goal_selection']['considered'] if c['goal_id'] == 'G02')
        self.assertFalse(g02['eligible'])
        self.assertIn('cannot prove', g02['reason'])

    def test_success_matching_uses_estimated_time_and_distinct_days(self):
        pk = self.pk
        pk.write()
        from oua.pack import Pack
        pack = Pack(pk.dir)
        start = ist(10)
        plays = [normalize(ev('U-1', 'play_started', ist(20, day=d), {'title_id': 'T0001'}), 'outcome')
                 for d in (0, 0, 1)]
        g09 = {'event': 'play_started', 'condition': 'on 3 distinct IST days'}
        self.assertEqual(success_events(pack, 'G09', g09, start, start + timedelta(days=14), plays), [])
        plays.append(normalize(ev('U-1', 'play_started', ist(9, day=3), {'title_id': 'T0001'}), 'outcome'))
        self.assertEqual(len(success_events(pack, 'G09', g09, start, start + timedelta(days=14), plays)), 1)
        # an event whose phone buffered it for 2 days really happened before the goal started
        late = normalize(ev('U-1', 'play_started', start - timedelta(days=2), {'title_id': 'T0001'},
                            buffered_s=2 * 86400 + 3600), 'outcome')
        g01 = {'event': 'play_started', 'condition': 'any title'}
        self.assertEqual(success_events(pack, 'G01', g01, start, start + timedelta(hours=24), [late]), [])


if __name__ == '__main__':
    unittest.main()
