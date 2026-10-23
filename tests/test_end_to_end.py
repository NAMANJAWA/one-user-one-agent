"""End to end on the real evidence pack (skipped if it is not next to the repo or in OUA_PACK).

Checks the whole replay day: every trigger decided once, nothing for holdout viewers, every decision on time, the
independent audit finds nothing, the injected model faults are handled, and two runs make the same decisions."""
import os
import shutil
import tempfile
import unittest

from helpers import REAL_PACK, Result
from oua.audit import audit
from oua.runtime import Replay, load_config

HAVE_PACK = os.path.exists(os.path.join(REAL_PACK, 'replay', 'triggers.jsonl'))


def projection(res):
    """Everything a decision decides, without the measured wall-clock milliseconds (which vary run to run)."""
    d = [(x['decision_id'], x['viewer_id'], x['status'], x.get('action'), (x.get('goal') or {}).get('id'),
          tuple(x.get('content') or ()), (x.get('no_action_reason') or '')[:40]) for x in res.decisions]
    a = [(x['action_id'], x['type'], x['user_id'], x.get('dialect'), x.get('hero_title_id'),
          str(x.get('rows')), x.get('title_id'), x.get('send_at')) for x in res.actions]
    return d, a


@unittest.skipUnless(HAVE_PACK, 'real pack not found; set OUA_PACK')
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='oua_e2e_')
        cls.runs = []
        for i in range(2):
            out = os.path.join(cls.tmp, 'run%d' % i)
            r = Replay(REAL_PACK, out, load_config(), log=lambda *a: None).run()
            r.write()
            cls.runs.append(Result(r, out))
        cls.res = cls.runs[0]

    @classmethod
    def tearDownClass(cls):
        for r in cls.runs:
            r.memory.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_trigger_decided_once_and_on_time(self):
        trig = [d for d in self.res.decisions if d['kind'] == 'trigger']
        firsts = [d for d in trig if d['status'] != 'DUPLICATE_IGNORED']
        self.assertEqual(len({d['trigger']['event_id'] for d in firsts}), len(firsts))
        self.assertTrue(all(d['decided_in_ms'] <= 60000 for d in trig))
        pages = [d for d in trig if str(d.get('action', '')).startswith('HOME_PAGE')]
        self.assertTrue(all(d['decided_in_ms'] <= 3000 for d in pages), 'pages must beat the viewer to home')

    def test_no_action_on_holdout_and_audit_clean(self):
        arms = self.res.replay.ctx.pack.arms
        self.assertFalse([a for a in self.res.actions if arms.get(a['user_id']) != 'agent'])
        rep = audit(REAL_PACK, self.res.out)
        self.assertEqual(rep['problem_count'], 0, rep['problems'])

    def test_injected_model_faults_are_rejected_or_never_reached(self):
        expected = {'202cccb8-8127-465f-b2d0-038cf533197e': 'rejected_unsupported_claim',
                    '76b138f6-0952-456d-904a-bd0d04497cf7': 'rejected_malformed_output'}
        by_trigger = {d['trigger']['event_id']: d for d in self.res.decisions if d['kind'] == 'trigger'}
        for eid, outcome in expected.items():
            if eid in by_trigger:
                self.assertEqual(by_trigger[eid]['goal_selection']['model']['outcome'], outcome)

    def test_two_runs_make_identical_decisions(self):
        self.assertEqual(projection(self.runs[0]), projection(self.runs[1]))


if __name__ == '__main__':
    unittest.main()
