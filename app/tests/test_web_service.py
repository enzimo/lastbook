#!/usr/bin/env python3
"""The web service settings (docs/WEB-SERVICE.md): allowed host names, the HTTPS-only key cookie, setup for key holders, and an AI
host elsewhere. No network: handlers get fake requests, and the AI client is checked before it would connect. Never packaged.

  python -B app/tests/test_web_service.py [-v]
"""
import io, json, os, sys, tempfile, unittest, urllib.error
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(ROOT)]
TMP = tempfile.TemporaryDirectory()
os.environ.update(LASTBOOK_DATA=str(Path(TMP.name) / 'LastbookData'), LASTBOOK_ALLOWED_HOSTS='Lastbook.Example.org, other.example.org',
                  LASTBOOK_COOKIE_SECURE='1', LASTBOOK_REMOTE_SETUP='1',
                  LASTBOOK_AI_BASE_URL='https://ai.example.org/v1/', LASTBOOK_AI_MODEL='m1', LASTBOOK_AI_API_KEY='k-123')
import server  # noqa: E402
import assistant  # noqa: E402
from engine.setup import api, manifest  # noqa: E402

# server and assistant read these settings once, at import. When another test module imported them first (the suite runs
# test_bridge earlier), the environment above came too late: set the values here, and put the old ones back afterwards.
_AI_BASE = assistant.remote_base(os.environ['LASTBOOK_AI_BASE_URL'])
_PATCHES = [
    mock.patch.multiple(server, ALLOWED_HOSTS=frozenset({'lastbook.example.org', 'other.example.org'}), COOKIE_SECURE=True,
                        REMOTE_SETUP=True),
    mock.patch.multiple(assistant, REMOTE=True, BASE=_AI_BASE, MODEL_URL=_AI_BASE + '/v1/chat/completions',
                        MODELS_URL=_AI_BASE + '/v1/models', MODEL='m1', API_KEY='k-123', WHO='The AI host'),
]


def setUpModule():
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


KEY = 'k' * 24


class FakeRequest:
    """Just enough of a BaseHTTPRequestHandler for the checks and for api.handle."""

    def __init__(self, path='/', ip='10.0.0.5', host='lastbook.example.org', cookie=None, origin=None, body=None, command='GET'):
        self.path, self.client_address, self.command = path, (ip, 40000), command
        self.headers = {'Host': host}
        if cookie:
            self.headers['Cookie'] = cookie
        if origin:
            self.headers['Origin'] = origin
        data = json.dumps(body).encode() if body is not None else b''
        if data:
            self.headers['Content-Length'] = str(len(data))
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()
        self.status, self.sent = None, []

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass

    def answer(self):
        return json.loads(self.wfile.getvalue() or b'null')


def handler(**kw):
    """A server.Handler's methods on a fake request."""
    r = FakeRequest(**kw)
    for name in ('host_ok', 'origin_ok', 'lan_ok'):
        setattr(r, name, getattr(server.Handler, name).__get__(r))
    return r


class Hosts(unittest.TestCase):
    def test_allowed_names_and_the_old_rules(self):
        for host in ('lastbook.example.org', 'LASTBOOK.example.org:443', 'other.example.org.', 'localhost:8890', '192.168.1.4:8890', '[::1]:8890'):
            self.assertTrue(handler(host=host).host_ok(), host)
        for host in ('evil.example.org', 'lastbook.example.org.evil.net', 'example.org'):
            self.assertFalse(handler(host=host).host_ok(), host)

    def test_origin_behind_tls_proxy(self):
        self.assertTrue(handler(origin='https://lastbook.example.org').origin_ok())
        self.assertFalse(handler(origin='https://evil.example.org').origin_ok())
        self.assertFalse(handler(origin='null').origin_ok())


class KeyCookie(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(server, 'LAN_KEY', KEY)
        p.start()
        self.addCleanup(p.stop)

    def test_key_link_sets_a_secure_cookie(self):
        r = handler(path='/?key=' + KEY)
        self.assertIsNone(r.lan_ok())
        self.assertEqual(r.status, 302)
        cookie = dict(r.sent)['Set-Cookie']
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)

    def test_cookie_marks_setup_allowed_and_nothing_else_does(self):
        r = handler(cookie='sg_key=' + KEY)
        self.assertTrue(r.lan_ok())
        self.assertIs(r.remote_setup_ok, True)
        r = handler(cookie='sg_key=wrong')
        self.assertFalse(r.lan_ok())
        self.assertIs(r.remote_setup_ok, False)
        r = handler(ip='127.0.0.1')      # loopback is let in as before, through the loopback rule in api.handle, not this flag
        self.assertTrue(r.lan_ok())
        self.assertIs(r.remote_setup_ok, False)

    def test_without_remote_setup_a_key_is_not_enough(self):
        with mock.patch.object(server, 'REMOTE_SETUP', False):
            r = handler(cookie='sg_key=' + KEY)
            self.assertTrue(r.lan_ok())
            self.assertIs(r.remote_setup_ok, False)


class SetupGate(unittest.TestCase):
    def setUp(self):
        self.setup = mock.Mock()
        self.setup.public_job.return_value = {'state': 'waiting'}
        self.setup.start.return_value = {'state': 'waiting'}
        p = mock.patch.object(api, 'SETUP', self.setup)
        p.start()
        self.addCleanup(p.stop)

    def call(self, method, name, ok, ip='10.0.0.5', body=None):
        r = FakeRequest(path='/api/setup/' + name, ip=ip, body=body, command=method)
        r.remote_setup_ok = ok
        api.handle(r, method, '/api/setup/' + name)
        return r.status, r.answer()

    def test_remote_needs_the_flag(self):
        self.assertEqual(self.call('GET', 'job', False)[0], 403)
        self.assertEqual(self.call('GET', 'job', 'yes')[0], 403)      # only True itself
        self.assertEqual(self.call('GET', 'job', True), (200, {'state': 'waiting'}))
        self.assertEqual(self.call('GET', 'job', False, ip='127.0.0.1')[0], 200)

    def test_remote_never_names_a_folder_or_adds_the_local_ai(self):
        status, _ = self.call('POST', 'start', True, body={'pack': 'small', 'folder': '/etc', 'ai': True})
        self.assertEqual(status, 200)
        args = self.setup.start.call_args
        self.assertEqual(args.args[:3], ('small', False, None))
        status, _ = self.call('POST', 'start', False, ip='127.0.0.1', body={'pack': 'small', 'folder': '/srv', 'ai': True})
        self.assertEqual(self.setup.start.call_args.args[:3], ('small', True, '/srv'))     # this computer: as before

    def test_remote_cannot_open_files_or_pickers(self):
        for name in ('use', 'pick', 'remove-ai'):
            status, out = self.call('POST', name, True, body={'path': '/etc/passwd'})
            self.assertEqual((status, out.get('code')), (403, 'remote'), name)
        self.setup.use.assert_not_called()


class NoLocalAI(unittest.TestCase):
    def test_models_dropped_also_after_a_fetch(self):
        m = manifest.Manifest(bundled={'version': 1, 'published': '', 'packs': [], 'maps': [],
                                       'models': [{'id': 'x', 'available': True}]}, remote=None, models=False)
        self.assertIsNone(m.model())
        self.assertEqual(m.public()['models'], [])
        m.current = m._offered(manifest.merge(m.current, {'version': 1, 'published': '', 'packs': [], 'maps': [],
                                                           'models': [{'id': 'y', 'available': True}]}))
        self.assertIsNone(m.model())


class AIHost(unittest.TestCase):
    def test_base_urls(self):
        self.assertEqual(assistant.BASE, 'https://ai.example.org')
        self.assertEqual(assistant.MODEL_URL, 'https://ai.example.org/v1/chat/completions')
        self.assertEqual(assistant.MODEL, 'm1')
        ok = {'http://bifrost:8080': 'http://bifrost:8080', 'https://h.example/openai/v1': 'https://h.example/openai',
              'https://h.example:8443/': 'https://h.example:8443'}
        for given, want in ok.items():
            self.assertEqual(assistant.remote_base(given), want)
        for bad in ('ftp://h', 'https://', 'https://u:p@h', 'https://h/v1?x=1', 'h.example', 'https://h:99999'):
            with self.assertRaises(ValueError, msg=bad):
                assistant.remote_base(bad)

    def test_only_the_model_server_with_the_key(self):
        sent = []

        def opened(req, timeout):
            sent.append((req.full_url, req.unredirected_hdrs.get('Authorization')))
            raise urllib.error.URLError('test: no network')
        with mock.patch.object(assistant._opener, 'open', opened):
            for url in (assistant.MODELS_URL, assistant.MODEL_URL):
                with self.assertRaises(urllib.error.URLError):
                    assistant.model_open(url, 1)
            for url in ('https://ai.example.org/v1/files', 'https://evil.example.org/v1/models', 'http://127.0.0.1:19443/v1/models'):
                with self.assertRaisesRegex(urllib.error.URLError, 'not the model server'):
                    assistant.model_open(url, 1)
        self.assertEqual(sent, [(assistant.MODELS_URL, 'Bearer k-123'), (assistant.MODEL_URL, 'Bearer k-123')])

    def test_key_from_a_file(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('from-file\n')
        try:
            with mock.patch.dict(os.environ, {'LASTBOOK_AI_API_KEY_FILE': f.name}):
                self.assertEqual(assistant.secret('LASTBOOK_AI_API_KEY'), 'from-file')
        finally:
            os.unlink(f.name)


if __name__ == '__main__':
    unittest.main()
