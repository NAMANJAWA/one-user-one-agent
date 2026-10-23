"""`oua explain`: the time-travel view of one viewer, and its message when the agent memory is missing."""
import os
import unittest

from helpers import MiniPack, ist, series_viewer, trig
from oua.explain import explain


class ExplainTests(unittest.TestCase):
    def test_explain_shows_the_timeline_and_respects_at(self):
        pk = MiniPack()
        try:
            series_viewer(pk, 'U-00300')
            pk.triggers += [trig('U-00300', ist(10)), trig('U-00300', ist(14))]
            res = pk.run()
            res.replay.close()
            pk._replays = []
            full = explain(res.out, 'U-00300')
            self.assertIn('DECISION D-000001', full)
            self.assertIn('GOAL COMMITTED', full)
            early = explain(res.out, 'U-00300', at='2026-10-01T06:00:00.000Z')   # 11:30 IST: before the 2nd visit
            self.assertIn('D-000001', early)
            self.assertNotIn('D-000002', early)
        finally:
            pk.cleanup()

    def test_missing_memory_gives_instructions_not_a_crash(self):
        msg = explain(os.path.join('no', 'such', 'run'), 'U-1')
        self.assertIn('run-all', msg)


if __name__ == '__main__':
    unittest.main()
