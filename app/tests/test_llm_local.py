"""Tests for llm_local.py: turning the AI off must stick, even while the model file is being checked.
No model is started and no large file is read: the size, the digest, the measuring and the spawn are all replaced here."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llm_local

TINY = b'0123456789'
FIT = {'ev': 'fit', 'status': 'fits', 'ram_total': 0, 'device_bytes': 1, 'host_bytes': 1, 'memory': []}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.model = self.dir / llm_local.FILE
        self.model.write_bytes(TINY)
        for patch in (mock.patch.object(llm_local, 'STATE_FILE', self.dir / 'llm' / 'state.json'),
                      mock.patch.object(llm_local, 'SIZE', len(TINY)),
                      mock.patch.object(llm_local, 'SHA256', hashlib.sha256(TINY).hexdigest()),
                      mock.patch.object(llm_local, 'places', lambda: [self.dir])):
            patch.start()
            self.addCleanup(patch.stop)
        self.local = llm_local.Local()
        self.local.runner = Path('fake-runner')
        self.spawned = []
        self.local._spawn = lambda: self.spawned.append(1)

    def fake_run(self, during=None):
        """A stand-in for subprocess.run when measuring; `during` runs while the measuring is going on."""
        def run(argv, **kw):
            self.measured = True
            if during:
                during()
            return mock.Mock(stdout=json.dumps(FIT, separators=(',', ':')).encode(), stderr=b'')
        return mock.patch.object(llm_local.subprocess, 'run', run)


class TurnedOff(Base):
    def test_off_during_measuring_stays_off(self):
        with self.fake_run(during=lambda: self.local.set_enabled(False)):
            self.local.discover()
        self.assertEqual(self.local.state, 'off')
        self.assertFalse(self.local.available())

    def test_off_during_checking_stays_off(self):
        real = hashlib.sha256
        with self.fake_run(), mock.patch.object(llm_local.hashlib, 'sha256', lambda: (self.local.set_enabled(False), real())[1]):
            self.local.discover()
        self.assertEqual(self.local.state, 'off')

    def test_check_and_measure_do_nothing_when_off(self):
        self.local.set_enabled(False)
        with self.fake_run():
            self.assertFalse(self.local._check(self.model))
            self.local._measure()
        self.assertEqual(self.local.state, 'off')
        self.assertFalse(getattr(self, 'measured', False))

    def test_turned_off_model_is_not_started_or_offered(self):
        self.local.state, self.local.path, self.local.fit = 'stopped', self.model, {'fits': True}
        self.local.saved['enabled'] = False
        self.assertFalse(self.local.available())
        self.assertTrue(self.local.ensure_running())      # a sentence saying why not
        self.assertEqual(self.spawned, [])


if __name__ == '__main__':
    unittest.main()
