"""The independent checks can disagree with the decision step, and the run auditor catches policy breaches."""
import json
import os
import unittest

from helpers import MiniPack, ist, iso, profile, series_viewer, trig
from oua.audit import audit
from oua.verify import CheckResult


class VerifierAuditTests(unittest.TestCase):
    def setUp(self):
        self.pk = MiniPack()

    def tearDown(self):
        self.pk.cleanup()

    def test_verifier_rejects_goal_the_raw_data_does_not_support(self):
        series_viewer(self.pk, 'U-00060')
        self.pk.profiles.append(profile('U-00060', status='lapsed', updated='2026-09-10T00:00:00.000Z'))
        self.pk.triggers.append(trig('U-00060', ist(10)))
        res = self.pk.run()
        v = res.replay.ctx.verifier
        r = CheckResult()
        v.check_goal(r, 'U-00060', {'goal_id': 'G05'}, ist(10), None)    # rows say never AND lapsed
        self.assertEqual(r.failed[0]['check'], 'eligibility')
        r = CheckResult()
        v.check_goal(r, 'U-00060', {'goal_id': 'G11'}, ist(10), None)
        self.assertTrue(any(f['check'] == 'measurable' for f in r.failed))

    def test_verifier_rejects_model_text_and_invented_ids_as_evidence(self):
        series_viewer(self.pk, 'U-00061')
        self.pk.triggers.append(trig('U-00061', ist(10)))
        res = self.pk.run()
        v = res.replay.ctx.verifier
        for refs in (['model:billing says trial'], ['event:not-a-real-event'], ['derived:trust-me']):
            r = CheckResult()
            v.check_evidence(r, 'U-00061', refs, ist(10))
            self.assertTrue(r.blocks('all'), refs)

    def test_audit_is_clean_on_a_real_run_and_catches_injected_breaches(self):
        series_viewer(self.pk, 'U-00062')
        series_viewer(self.pk, 'U-00063')
        self.pk.arms['U-00063'] = 'holdout'
        self.pk.triggers += [trig('U-00062', ist(10)), trig('U-00063', ist(10))]
        res = self.pk.run()
        self.assertEqual(audit(self.pk.dir, res.out)['problem_count'], 0)
        page = res.actions_for('U-00062', ['home_page'])[0]
        bad = [
            dict(page, action_id='X1', user_id='U-00063'),                                   # holdout viewer
            dict(page, action_id='X2', dialect='d2'),                                        # wrong dialect
            {'action_id': 'X3', 'decision_id': page['decision_id'], 'user_id': 'U-00062', 'type': 'push',
             'trigger_event_id': page['trigger_event_id'], 'published_at': page['published_at'],
             'send_at': iso(ist(23, 15)), 'title_id': 'T0003', 'deeplink': 'ott://title/T0003',
             'text': {'title': 'Last chance!', 'body': 'Hurry, only Rs 99 today'}},          # night, copy, price
            {'action_id': 'X4', 'decision_id': page['decision_id'], 'user_id': 'U-00062', 'type': 'whatsapp',
             'trigger_event_id': page['trigger_event_id'], 'published_at': page['published_at'],
             'send_at': iso(ist(12)), 'title_id': 'T0003', 'template_id': 'wa_free_text', 'variables': {}},
        ]
        with open(os.path.join(res.out, 'actions.jsonl'), 'a', encoding='utf-8') as f:
            for a in bad:
                f.write(json.dumps(a) + '\n')
        rep = audit(self.pk.dir, res.out)
        found = set(rep['problems'])
        for p in ('action_on_non_agent_arm', 'page_dialect_not_app_dialect', 'message_in_quiet_hours',
                  'push_forbidden_phrase', 'push_invented_price', 'whatsapp_template_not_approved',
                  'message_without_consent', 'more_than_one_page_per_trigger'):
            self.assertIn(p, found)


if __name__ == '__main__':
    unittest.main()
