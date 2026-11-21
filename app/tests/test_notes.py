"""Things I couldn't find (engine/notes.py, #116): notes are kept only when asked, hold only the words, a known guide and the
date, live in the settings folder (never the library), can be deleted one by one or all at once, and are answered only to this
computer. No library, no server and no port.

  python3 -B -m unittest app.tests.test_notes
"""
import contextlib, io, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
from engine import notes  # noqa: E402
from engine.setup import api  # noqa: E402

GUIDES = ['water', 'hurt']


class H:
    def __init__(self, ip='127.0.0.1', body=None, command='POST'):
        raw = b'' if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.client_address, self.command = (ip, 1), command
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile, self.wfile, self.status = io.BytesIO(raw), io.BytesIO(), None
    def send_response(self, s): self.status = s
    def send_header(self, *a): pass
    def end_headers(self): pass
    def answer(self): return json.loads(self.wfile.getvalue())


class Notes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = Path(self.dir.name) / 'settings'
        self.p = mock.patch.object(api, 'SETUP', mock.Mock(settings_dir=self.settings))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.dir.cleanup()

    def test_kept_only_what_was_asked_in_the_settings_folder(self):
        self.assertEqual(notes.load(), [])
        self.assertFalse(self.settings.exists())                          # nothing written until a note is kept
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = notes.add('  snake \t  bite\n', 'hurt', GUIDES)
        self.assertEqual(set(n), {'id', 'words', 'guide', 'at'})            # only these: no answer, nothing from the library
        self.assertEqual((n['words'], n['guide']), ('snake bite', 'hurt'))   # spaces, tabs and line breaks folded
        self.assertEqual(notes.load(), [n])
        self.assertEqual(json.loads((self.settings / notes.FILE_NAME).read_text(encoding='utf-8'))['notes'], [n])
        self.assertNotIn('snake', out.getvalue())                          # never in the log

    def test_words_and_guide_are_checked(self):
        self.assertEqual(len(notes.add('x' * 500, None, GUIDES)['words']), notes.MAX_WORDS)
        self.assertIsNone(notes.add('insulin without a fridge', 'not-a-guide', GUIDES)['guide'])
        self.assertIsNone(notes.add('insulin', '../../etc', GUIDES)['guide'])
        for empty in ('', '   ', None):
            with self.assertRaises(ValueError):
                notes.add(empty, None, GUIDES)

    def test_values_of_other_types(self):
        """#177 review: the request body is JSON, so guide and words can be any JSON value. A guide that is not text (a list or an
        object could not even be looked up among the guides, and raised) counts as no guide; words that are not text are refused.
        Through the route, each is a plain answer, never an internal error, and nothing odd is kept."""
        for guide in (['water'], {'id': 'water'}, 7, True, None):
            with self.subTest(guide=guide):
                self.assertIsNone(notes.add('sterilize a needle', guide, GUIDES)['guide'])
                h = H(body={'words': 'sterilize a needle', 'guide': guide})
                notes.handle(h, 'POST', '/api/not-found', GUIDES)
                self.assertEqual((h.status, h.answer()['note']['guide']), (200, None))
        for words in (['a'], {'w': 1}, 12, False):
            with self.subTest(words=words):
                with self.assertRaises(ValueError):
                    notes.add(words, None, GUIDES)
                h = H(body={'words': words, 'guide': 'water'})
                notes.handle(h, 'POST', '/api/not-found', GUIDES)
                self.assertEqual(h.status, 400)
        self.assertTrue(all(isinstance(n['words'], str) and n['guide'] is None for n in notes.load()))

    def test_delete_one_and_all(self):
        a, b = notes.add('one', None, GUIDES), notes.add('two', 'water', GUIDES)
        self.assertEqual(notes.delete(a['id']), 1)
        self.assertEqual(notes.delete(a['id']), 0)
        self.assertEqual([n['id'] for n in notes.load()], [b['id']])
        notes.add('three', None, GUIDES)
        self.assertEqual(notes.delete_all(), 2)
        self.assertEqual(notes.load(), [])

    def test_oldest_go_beyond_the_limit(self):
        with mock.patch.object(notes, 'MAX_NOTES', 3):
            for w in 'abcde':
                notes.add(w, None, GUIDES)
        self.assertEqual([n['words'] for n in notes.load()], ['c', 'd', 'e'])

    def test_a_damaged_file_reads_as_no_notes(self):
        self.settings.mkdir(parents=True)
        for text in ('not json', '[]', '{"notes": "x"}', '{"notes": [1, {"id": 2}]}'):
            (self.settings / notes.FILE_NAME).write_text(text, encoding='utf-8')
            self.assertEqual(notes.load(), [], text)

    def test_routes_this_computer_only(self):
        for method, path, body in (('GET', '/api/not-found', None), ('POST', '/api/not-found', {'words': 'x'}),
                                   ('POST', '/api/not-found/delete-all', {})):
            h = H('192.0.2.20', body, method)
            notes.handle(h, method, path, GUIDES)
            self.assertEqual(h.status, 403, path)
        self.assertFalse(self.settings.exists())

    def test_routes(self):
        h = H(body={'words': 'generator carbon monoxide', 'guide': 'water'})
        notes.handle(h, 'POST', '/api/not-found', GUIDES)
        n = h.answer()['note']
        self.assertEqual((h.status, n['words'], n['guide']), (200, 'generator carbon monoxide', 'water'))
        g = H('::1', command='GET')
        notes.handle(g, 'GET', '/api/not-found', GUIDES)
        self.assertEqual(g.answer(), {'notes': [n]})
        for bad in ({'words': ''}, b'not json', [1]):
            b = H(body=bad)
            notes.handle(b, 'POST', '/api/not-found', GUIDES)
            self.assertEqual(b.status, 400, bad)
        d = H(body={'id': n['id']})
        notes.handle(d, 'POST', '/api/not-found/delete', GUIDES)
        self.assertEqual(d.answer(), {'deleted': 1})
        x = H(body={})
        notes.handle(x, 'POST', '/api/not-found/nope', GUIDES)
        self.assertEqual(x.status, 404)


if __name__ == '__main__':
    unittest.main()
