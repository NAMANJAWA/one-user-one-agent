"""Tool and model failures: data store timeouts, model outage and circuit breaker, unsupported claims, malformed
output, model timeouts. Every failure must degrade to a smaller safe action or to no action, on time."""
import unittest
from datetime import timedelta

from helpers import MiniPack, iso, ist, new_viewer, series_viewer, trig


def outage(dep, start, end, kind='dependency_unavailable'):
    return {'at': iso(start), 'type': kind, 'dependency': dep, 'until': iso(end), 'detail': 'test'}


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.pk = MiniPack()

    def tearDown(self):
        self.pk.cleanup()

    def test_data_store_down_with_nothing_cached_gives_goal_free_fallback_page(self):
        series_viewer(self.pk, 'U-00010')
        self.pk.control.append(outage('user_data_store', ist(9), ist(11)))
        self.pk.triggers.append(trig('U-00010', ist(10)))
        res = self.pk.run()
        d = res.for_user('U-00010')[0]
        self.assertEqual(d['status'], 'PUBLISHED_FALLBACK')
        self.assertIsNone(d['goal'])
        self.assertIn('no goal is committed', d['no_goal_reason'])
        self.assertLess(d['decided_in_ms'], 3000)
        self.assertEqual(res.actions_for('U-00010', ['push', 'whatsapp']), [], 'no messages on unverified facts')
        page = res.actions_for('U-00010', ['home_page'])[0]
        self.assertEqual([r['row_type'] for r in page['rows']], ['popular_in_dialect'])

    def test_data_store_down_uses_cached_read_and_flags_it(self):
        series_viewer(self.pk, 'U-00011')
        self.pk.triggers += [trig('U-00011', ist(8)), trig('U-00011', ist(10, 30))]
        self.pk.control.append(outage('user_data_store', ist(10), ist(11)))
        res = self.pk.run()
        d = res.for_user('U-00011')[1]
        self.assertEqual(d['status'], 'PUBLISHED')
        self.assertIn('cache_read_at', d['degraded'])
        self.assertIn('cache', d['confidence_reason'])

    def test_model_outage_falls_back_and_opens_circuit(self):
        # five viewers 20 s apart during the outage, then one after the 120 s cooldown and the outage
        for i in range(5):
            new_viewer(self.pk, 'U-0002%d' % i, signed_up_days_ago=1)
            self.pk.triggers.append(trig('U-0002%d' % i, ist(10) + timedelta(seconds=20 * i)))
        new_viewer(self.pk, 'U-00029', signed_up_days_ago=1)
        self.pk.triggers.append(trig('U-00029', ist(10, 10)))
        self.pk.control.append(outage('model_provider', ist(9), ist(10, 5)))
        res = self.pk.run()
        outcomes = [res.for_user('U-0002%d' % i)[0]['goal_selection']['model']['outcome'] for i in range(5)]
        self.assertEqual(outcomes[:3], ['error_unavailable'] * 3)
        self.assertEqual(outcomes[3:], ['skipped_circuit_open'] * 2, 'after 3 failures the agent stops calling')
        self.assertEqual(res.for_user('U-00029')[0]['goal_selection']['model']['outcome'], 'accepted',
                         'after the cooldown the agent tries the model again')
        for i in range(5):
            d = res.for_user('U-0002%d' % i)[0]
            self.assertEqual(d['status'], 'PUBLISHED')
            self.assertEqual(d['goal']['id'], d['goal_selection']['deterministic_choice'])

    def test_unsupported_model_claim_is_rejected(self):
        new_viewer(self.pk, 'U-00030', signed_up_days_ago=1)
        t = trig('U-00030', ist(10))
        self.pk.triggers.append(t)
        self.pk.faults.append({'trigger_event_id': t['event_id'], 'user_id': 'U-00030', 'fault': 'unsupported_claim',
                               'model_output': {'goal_id': 'G04', 'eligible': True, 'confidence': 0.97,
                                                'reason': 'The user is on an active trial (confirmed in billing).'}})
        d = self.pk.run().for_user('U-00030')[0]
        m = d['goal_selection']['model']
        self.assertEqual(m['outcome'], 'rejected_unsupported_claim')
        self.assertNotEqual(d['goal']['id'], 'G04')
        self.assertFalse(any(e['ref'].startswith('model') for e in d['evidence']), 'model text is never evidence')

    def test_malformed_model_output_falls_back(self):
        new_viewer(self.pk, 'U-00031', signed_up_days_ago=1)
        t = trig('U-00031', ist(10))
        self.pk.triggers.append(t)
        self.pk.faults.append({'trigger_event_id': t['event_id'], 'user_id': 'U-00031', 'fault': 'malformed_output',
                               'model_output': '{"goal_id": "G02", "eligible": tr'})
        d = self.pk.run().for_user('U-00031')[0]
        self.assertEqual(d['goal_selection']['model']['outcome'], 'rejected_malformed_output')
        self.assertEqual(d['status'], 'PUBLISHED')

    def test_model_timeout_still_publishes_within_page_budget(self):
        new_viewer(self.pk, 'U-00032', signed_up_days_ago=1)
        t = trig('U-00032', ist(10))
        self.pk.triggers.append(t)
        self.pk.faults.append({'trigger_event_id': t['event_id'], 'user_id': 'U-00032', 'fault': 'timeout'})
        d = self.pk.run().for_user('U-00032')[0]
        self.assertEqual(d['goal_selection']['model']['outcome'], 'error_timeout')
        self.assertEqual(d['status'], 'PUBLISHED')
        self.assertLess(d['decided_in_ms'], 3000)

    def test_budget_limits_are_enforced(self):
        new_viewer(self.pk, 'U-00033', signed_up_days_ago=1)
        self.pk.triggers.append(trig('U-00033', ist(10)))
        d = self.pk.run({'budgets.max_model_calls': 0}).for_user('U-00033')[0]
        self.assertEqual(d['goal_selection']['model']['outcome'], 'skipped_budget')
        self.assertEqual(d['budget']['model_calls'], 0)
        self.assertEqual(d['status'], 'PUBLISHED')


if __name__ == '__main__':
    unittest.main()
