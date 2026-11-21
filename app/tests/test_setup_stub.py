"""The setup stub (app/tests/setup_stub.py) drives the setup screens for previews. Its scenarios must run to their end
without the stub itself failing. No server is started: the Stub class is driven directly on a moved clock. Never packaged."""
import sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_stub  # noqa: E402


class BusyScenario(unittest.TestCase):
    def test_busy_waits_with_a_countdown_then_finishes(self):
        """busy: past 40% the item shows retry_in_s with no rate for 2 minutes, then the download goes on and finishes."""
        now = [1000.0]
        with mock.patch.object(setup_stub.time, 'time', lambda: now[0]):
            stub = setup_stub.Stub('busy')
            stub.start({'pack': 'small', 'ai': False})
            seen = []
            for _ in range(400):
                now[0] += 1
                stub.tick()                       # the crossing of 40% used to raise KeyError('busy') here
                it = stub.public()['items'][0]
                if 'retry_in_s' in it:
                    seen.append((it['retry_in_s'], it['rate_bps'], it['eta_s']))
                if stub.public()['state'] == 'done':
                    break
        self.assertTrue(seen, 'no countdown shown')
        self.assertTrue(all(rate == 0 and eta is None for _, rate, eta in seen), seen[:3])
        self.assertLessEqual(seen[0][0], 120)
        self.assertEqual(stub.public()['state'], 'done')
        self.assertIsNone(stub.public()['error'])

    def test_server_scenario_still_fails_once(self):
        """server: the generic failure path is unchanged for the other downloading faults."""
        now = [1000.0]
        with mock.patch.object(setup_stub.time, 'time', lambda: now[0]):
            stub = setup_stub.Stub('server')
            stub.start({'pack': 'small', 'ai': False})
            for _ in range(20):
                now[0] += 1
                stub.tick()
        self.assertEqual(stub.public()['state'], 'failed')
        self.assertEqual(stub.public()['error']['code'], 'server')


class RepairScenario(unittest.TestCase):
    def test_repair_counts_the_bytes_it_checks(self):
        """manage-ai, Repair: the job says repair; the library, then the AI file already here, are checked with growing bytes and a time left."""
        now = [1000.0]
        with mock.patch.object(setup_stub.time, 'time', lambda: now[0]):
            stub = setup_stub.Stub('manage-ai')
            stub.start({'repair': True})
            j = stub.public()
            self.assertTrue(j['repair'])
            self.assertEqual([i['state'] for i in j['items']], ['waiting', 'waiting'])   # as the engine answers
            self.assertEqual([(i['kind'], i.get('source')) for i in j['items']], [('pack', 'download'), ('model', 'found')])
            seen = {'pack': [], 'model': []}
            for _ in range(60):
                now[0] += 1
                stub.tick()
                for it in stub.public()['items']:
                    if it['state'] == 'verifying':
                        seen[it['kind']].append((it['bytes_done'], it['eta_s']))
                    self.assertNotEqual(it['state'], 'downloading')
                if stub.public()['state'] == 'done':
                    break
        self.assertEqual(stub.public()['state'], 'done')
        for kind, s in seen.items():
            done = [d for d, _ in s]
            self.assertEqual(done, sorted(done), kind)
            self.assertGreater(len(set(done)), 3, kind)
            self.assertTrue(all(e is not None for _, e in s[1:]), kind)


if __name__ == '__main__':
    unittest.main()
