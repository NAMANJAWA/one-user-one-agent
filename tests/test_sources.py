"""Conflicting sources: duplicate snapshot rows, three dialects, opt-out after snapshot, viewers with no profile."""
import unittest

from helpers import MiniPack, default_titles, ev, ist, live, new_viewer, profile, series_viewer, trig


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.pk = MiniPack()

    def tearDown(self):
        self.pk.cleanup()

    def test_two_snapshot_rows_resolved_by_billing_history(self):
        series_viewer(self.pk, 'U-00003')
        # second, older row says active; a purchase in the visible history agrees with it
        self.pk.profiles.append(profile('U-00003', status='active', updated='2026-09-14T07:00:00.000Z'))
        self.pk.history.append(ev('U-00003', 'purchase_completed', ist(12, day=-16),
                                  {'plan_id': 'P_MONTH', 'price_inr': 149, 'is_trial': False}))
        self.pk.triggers.append(trig('U-00003', ist(10)))
        d = self.pk.run().for_user('U-00003')[0]
        st = d['facts']['subscription_status']
        self.assertEqual(st['value'], 'active')
        self.assertEqual(st['source'], 'profile+event')
        self.assertEqual(len(st['conflicts']), 2, 'both rows stay visible as a conflict')
        self.assertNotEqual(d['goal']['id'], 'G05', 'a viewer who paid must not get "first paid plan"')

    def test_unresolvable_rows_block_status_goals(self):
        series_viewer(self.pk, 'U-00004')
        self.pk.profiles.append(profile('U-00004', status='lapsed', updated='2026-09-12T07:00:00.000Z'))
        self.pk.triggers.append(trig('U-00004', ist(10)))
        d = self.pk.run().for_user('U-00004')[0]
        st = d['facts']['subscription_status']
        self.assertIsNone(st['value'])
        self.assertEqual(st['source'], 'unresolved')
        considered = {c['goal_id']: c for c in d['goal_selection']['considered']}
        for g in ('G04', 'G05', 'G06', 'G08', 'G09'):
            self.assertFalse(considered[g]['eligible'], g)
        self.assertEqual(d['goal']['id'], 'G07', 'a status-independent goal is still possible')

    def test_three_dialects_app_setting_decides_the_page(self):
        self.pk.titles += [dict(t, title_id=t['title_id'].replace('T', 'S'), content_slug=t['content_slug'] + '-d2')
                           for t in default_titles(dialect='d2')]
        series_viewer(self.pk, 'U-00005', dialect='d2', title_id='S0002')
        self.pk.history[-1]['properties']['episode_id'] = 'slug-t0002-d2-s1e1'   # the d2 series' real episode
        self.pk.profiles[-1]['preferred_dialect'] = 'd1'
        self.pk.triggers.append(trig('U-00005', ist(10), dialect='d2',
                                     props={'deeplink': 'ott://c/cmp9/d3/title/T0009'}))
        res = self.pk.run()
        d = res.for_user('U-00005')[0]
        self.assertEqual(d['dialect']['used'], 'd2')
        self.assertEqual(d['dialect']['source'], 'app_setting')
        self.assertEqual(sorted(d['dialect']['conflicts']), ['campaign:d3', 'profile:d1'])
        self.assertIn('app setting', d['confidence_reason'])
        page = res.actions_for('U-00005', ['home_page'])[0]
        self.assertEqual(page['dialect'], 'd2')
        ids = [page['hero_title_id']] + [t for r in page['rows'] for t in r['title_ids']]
        self.assertTrue(all(t.startswith('S') for t in ids), 'every title must be in the page dialect')

    def test_opt_out_event_overrides_snapshot_consent(self):
        series_viewer(self.pk, 'U-00006', push='true')
        self.pk.history.append(ev('U-00006', 'notification_opt_out', ist(9, day=-1), {'channel': 'push'}))
        self.pk.triggers.append(trig('U-00006', ist(10)))
        res = self.pk.run()
        d = res.for_user('U-00006')[0]
        self.assertFalse(d['facts']['consent_push']['value'])
        self.assertEqual(d['facts']['consent_push']['source'], 'event')
        self.assertEqual(res.actions_for('U-00006', ['push', 'whatsapp']), [])

    def test_opt_out_arriving_after_decision_cancels_queued_message(self):
        series_viewer(self.pk, 'U-00007', push='true')
        self.pk.triggers.append(trig('U-00007', ist(10)))
        self.pk.live.append(live('U-00007', 'notification_opt_out', ist(11), props={'channel': 'push'}))
        res = self.pk.run()
        self.assertEqual(res.actions_for('U-00007', ['push']), [], 'queued push must be cancelled by the opt-out')
        rp = res.for_user('U-00007', kind='replan')
        self.assertEqual(len(rp), 1)
        self.assertEqual(len(rp[0]['cancelled_actions']), 1)

    def test_brand_new_viewer_without_profile_row(self):
        new_viewer(self.pk, 'U-00008', signed_up_days_ago=0.2, with_profile=False)
        self.pk.triggers.append(trig('U-00008', ist(10)))
        res = self.pk.run()
        d = res.for_user('U-00008')[0]
        self.assertEqual(d['facts']['subscription_status']['value'], 'never')
        self.assertEqual(d['facts']['subscription_status']['source'], 'derived')
        self.assertEqual(d['goal']['id'], 'G01')
        self.assertFalse(d['facts']['consent_push']['value'], 'no profile row means no proven consent')
        self.assertEqual(res.actions_for('U-00008', ['push', 'whatsapp']), [])


if __name__ == '__main__':
    unittest.main()
