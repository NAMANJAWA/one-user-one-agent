"""What happens when 1,000 viewers open the app in the same minute?

Default (model in log-only mode): the model call is off the page path, so service time is ~0.1 s and 4 workers
keep up with ~17 arrivals/s without a queue.

Advise mode (model on the page path): 4 workers at ~1 s per decision cannot keep up, a queue builds, and once queue
wait passes the configured threshold the agent sheds the model call (the deterministic planner still decides),
service time drops and the queue drains. Every page must still beat the viewer home in both modes."""
import unittest
from datetime import timedelta

from helpers import MiniPack, ist, new_viewer, trig


def burst(pk, n=1000):
    for i in range(n):
        uid = 'U-L%04d' % i
        new_viewer(pk, uid, signed_up_days_ago=1)
        pk.triggers.append(trig(uid, ist(20) + timedelta(milliseconds=60000 * i // n)))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.pk = MiniPack()

    def tearDown(self):
        self.pk.cleanup()

    def test_thousand_viewers_in_one_minute_log_only(self):
        burst(self.pk)
        res = self.pk.run()
        ds = [d for d in res.decisions if d['kind'] == 'trigger']
        self.assertEqual(len(ds), 1000)
        self.assertTrue(all(d['status'] == 'PUBLISHED' for d in ds))
        self.assertLess(max(d['decided_in_ms'] for d in ds), 1000, 'the model adds no latency to the page')
        models = [d['goal_selection']['model'] for d in ds]
        self.assertTrue(all(m.get('followed') is False for m in models if m.get('called')))
        self.assertTrue(all(d['goal']['id'] == d['goal_selection']['deterministic_choice'] for d in ds))

    def test_thousand_viewers_in_one_minute_advise_mode_sheds_the_model(self):
        burst(self.pk)
        res = self.pk.run({'model.mode': 'advise'})
        ds = [d for d in res.decisions if d['kind'] == 'trigger']
        outcomes = [d['goal_selection']['model']['outcome'] for d in ds]
        shed = outcomes.count('skipped_load_shedding')
        self.assertGreater(shed, 500, 'the model call should be shed for most of the burst')
        self.assertGreater(outcomes.count('accepted'), 0, 'the model is still used while there is headroom')
        self.assertTrue(all(d['status'] == 'PUBLISHED' for d in ds))
        self.assertLess(max(d['decided_in_ms'] for d in ds), 3000, 'every page must be live before the viewer is home')
        self.assertGreater(max(d['latency']['queue_ms'] for d in ds), 0, 'a queue did form')


if __name__ == '__main__':
    unittest.main()
