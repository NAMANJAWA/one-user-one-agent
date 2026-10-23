"""Late, duplicate and same-session triggers; wrong phone clocks; holdout viewers."""
import unittest
import uuid
from datetime import timedelta

from helpers import MiniPack, ist, iso, series_viewer, trig


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.pk = MiniPack()

    def tearDown(self):
        self.pk.cleanup()

    def test_duplicate_trigger_is_idempotent(self):
        series_viewer(self.pk, 'U-00001')
        t = trig('U-00001', ist(10))
        dup = dict(t, deliver_at=iso(ist(10) + timedelta(seconds=40)))   # same event id, redelivered later
        self.pk.triggers += [t, dup]
        res = self.pk.run()
        ds = res.for_user('U-00001')
        self.assertEqual([d['status'] for d in ds], ['PUBLISHED', 'DUPLICATE_IGNORED'])
        self.assertIn(ds[0]['decision_id'], ds[1]['no_action_reason'])
        self.assertEqual(len(res.actions_for('U-00001', ['home_page'])), 1)

    def test_offline_buffered_trigger_gets_no_page_and_no_reads(self):
        series_viewer(self.pk, 'U-00001')
        self.pk.triggers.append(trig('U-00001', ist(10, day=-3), buffered_s=3 * 86400))   # sat 3 days on the phone
        res = self.pk.run()
        d = res.for_user('U-00001')[0]
        self.assertEqual(d['status'], 'NO_ACTION')
        self.assertTrue(d['no_action_reason'].startswith('stale trigger'))
        self.assertEqual(d['budget']['tool_calls'], 0, 'a stale trigger must not spend data-store reads')
        self.assertEqual(res.actions_for('U-00001'), [])

    def test_wrong_phone_clock_is_not_mistaken_for_staleness(self):
        series_viewer(self.pk, 'U-00001')
        # phone clock 5 hours ahead, but the event was sent immediately: it is fresh
        self.pk.triggers.append(trig('U-00001', ist(10), skew_s=5 * 3600))
        res = self.pk.run()
        d = res.for_user('U-00001')[0]
        self.assertEqual(d['status'], 'PUBLISHED')
        self.assertEqual(d['trigger']['buffered_s'], 0)
        self.assertGreater(d['trigger']['clock_skew_s'], 4 * 3600)

    def test_login_then_app_open_in_one_session_gives_one_page(self):
        series_viewer(self.pk, 'U-00001')
        s = str(uuid.uuid4())
        self.pk.triggers += [trig('U-00001', ist(10), event='login_success', session=s),
                             trig('U-00001', ist(10) + timedelta(seconds=6), session=s)]
        res = self.pk.run()
        ds = res.for_user('U-00001')
        self.assertEqual(ds[0]['status'], 'PUBLISHED')
        self.assertEqual(ds[1]['status'], 'NO_ACTION')
        self.assertIn('already acted in this session', ds[1]['no_action_reason'])
        self.assertEqual(len(res.actions_for('U-00001', ['home_page'])), 1)

    def test_holdout_viewer_gets_shadow_decision_and_no_model_call(self):
        series_viewer(self.pk, 'U-00002')
        self.pk.arms['U-00002'] = 'holdout'
        t = trig('U-00002', ist(10))
        self.pk.triggers.append(t)
        self.pk.faults.append({'trigger_event_id': t['event_id'], 'user_id': 'U-00002', 'fault': 'timeout'})
        res = self.pk.run()
        d = res.for_user('U-00002')[0]
        self.assertEqual(d['status'], 'SHADOW_NO_ACTION')
        self.assertEqual(d['action'], 'NO_ACTION')
        self.assertEqual(d['goal_selection']['model']['outcome'], 'not_called_holdout_shadow')
        self.assertEqual(d['shadow']['goal_id'], 'G07')
        self.assertEqual(res.actions_for('U-00002'), [])


if __name__ == '__main__':
    unittest.main()
