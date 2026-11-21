#!/usr/bin/env python3
"""A Custom library (enzimo fork): engine/setup/custom.py and Setup.start(custom_selection=...), against the local HTTPS test
server of test_setup.py serving a small All pack shaped like the real one (a stored zip with pack-manifest.tsv, catalog.json, an
FTS5 search index, a whole-US map set, course assets and the sources page). No real internet. Never packaged.

  python -B app/tests/test_custom_library.py [-v]
"""
import hashlib, io, json, os, shutil, sqlite3, sys, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(ROOT)]
from engine.setup import api, custom, download, install, manifest  # noqa: E402
from app.tests.test_setup import ALLOWED, fast_backoff, sha  # noqa: E402
from app.tests.test_map_checklist import StateMaps  # noqa: E402

KiB = 1024


def all_pack():
    """{rel: bytes} of a small All pack, and its catalog. Videos come with a transcript and a metadata record each."""
    files, cat = {}, []

    def add(rec, size, fields=('path',)):
        for f in fields:
            rel = 'library-data/' + rec[f]
            files[rel] = hashlib.sha256((rec['id'] + f).encode()).digest() * (size // 32 + 1)
            files[rel] = files[rel][:size]
        cat.append(rec)
    for i in range(6):
        cat_name = 'medical' if i < 4 else 'fishing'
        v = {'id': f'video-v{i}', 'kind': 'video', 'title': f'Video {i}', 'category': cat_name, 'collection': 'Practical video demonstrations',
             'path': f'videos/{cat_name}/v{i}.mp4', 'text_path': f'text/video-v{i}.txt', 'bytes': 1}
        add(v, 300 * KiB if i == 0 else 40 * KiB, ('path', 'text_path'))
        add({'id': f'metadata-video-v{i}', 'kind': 'metadata', 'video_id': f'video-v{i}', 'video_path': v['path'], 'category': cat_name,
             'path': f'metadata/videos/v{i}.jpg', 'thumbnail_path': f'metadata/videos/v{i}.jpg', 'title': 't'}, 3 * KiB)
        add({'id': f'transcript-v{i}', 'kind': 'transcript', 'video_path': v['path'], 'category': cat_name, 'title': 't',
             'path': f'transcripts/v{i}.en.txt', 'vtt_path': f'transcripts/v{i}.en.vtt'}, 2 * KiB, ('path', 'vtt_path'))
    for i in range(8):
        cat_name = ['engineering', 'medical'][i % 2]
        add({'id': f'doc-{i}', 'kind': 'pdf', 'title': f'Manual {i}', 'category': cat_name, 'collection': 'Manuals', 'pages': '10',
             'path': f'documents/{cat_name}/m{i}.pdf', 'text_path': f'text/doc-{i}.txt', 'bytes': 1}, 20 * KiB, ('path', 'text_path'))
    add({'id': 'zim-1', 'kind': 'zim', 'title': 'MDWiki: medical encyclopedia', 'category': 'encyclopedias', 'path': 'collections/mdwiki.zim'}, 250 * KiB)
    add({'id': 'zim-2', 'kind': 'zim', 'title': 'Wikipedia: full English text', 'category': 'encyclopedias', 'path': 'collections/wikipedia.zim'}, 260 * KiB)
    add({'id': 'lastbook-sources', 'kind': 'html', 'title': 'Sources and permissions', 'category': 'about', 'collection': 'About this library',
         'path': 'documents/lastbook/sources-and-permissions.html'}, 2 * KiB)
    for i in range(30):                      # course assets no record names
        files[f'library-data/courses/c1/img/{i}.png'] = os.urandom(1500)
    files['library-data/reader.css'] = b'body{}'
    for name in ('national.sqlite', 'terrain.sqlite', 'map.sqlite'):
        files['derived/geo/' + name] = os.urandom(150 * KiB)          # All's whole-US map: never taken
    files['library-data/geography/resources/atlas.sqlite'] = os.urandom(20 * KiB)
    files['library-data/geography/resources/hazards.json'] = b'{"x": 1}'
    files['map-set.json'] = b'{"name": "us", "files": {}}'
    files['pack.json'] = json.dumps({'name': 'all', 'map': 'us'}).encode()
    files['library-data/catalog.json'] = json.dumps(cat).encode()
    files['library-data/summary.json'] = b'{}'
    return files, cat


def search_db(cat, path):
    db = sqlite3.connect(path)
    db.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
    for r in cat:
        if r['kind'] != 'metadata':
            db.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', (r['id'], r['title'], r.get('collection', ''), r.get('category', ''), r['path'], 1, 'words ' + r['id']))
    db.commit()
    db.close()


def make_all_zip(path, files):
    """Stored, under LastbookData/, with pack-manifest.tsv listing every file but itself, as the real All."""
    rows = ['path\tbytes\tsha256\thow\tchanges'] + [f'{k}\t{len(v)}\t{sha(v)}\tclone\t' for k, v in sorted(files.items())]
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('LastbookData/', b'')
        z.writestr('LastbookData/pack-manifest.tsv', ('\n'.join(rows) + '\n').encode())
        for k, v in files.items():
            z.writestr('LastbookData/' + k, v)


class Custom(StateMaps):
    def setUp(self):
        super().setUp()
        self.patch = mock.patch.multiple(custom, BIG=200 * KiB, RUN_MAX=96 * KiB, GAP_MAX=8 * KiB, TAIL=64 * KiB, HEADER_PROBE=16)
        self.patch.start()
        self.all_files, self.cat = all_pack()
        search_db(self.cat, self.tmp / 'search.sqlite')
        self.all_files['library-data/search.sqlite'] = (self.tmp / 'search.sqlite').read_bytes()
        make_all_zip(self.tmp / 'Lastbook-All.zip', self.all_files)
        self.zip_all = (self.tmp / 'Lastbook-All.zip').read_bytes()
        self.files.data['Lastbook-All.zip'] = self.zip_all
        m = self.mf.current
        m['packs'] = [p for p in m['packs'] if p['id'] != 'all'] + [{
            'id': 'all', 'title': 'All', 'summary': 'a', 'records': len(self.cat), 'installed_bytes': len(self.zip_all), 'available': True,
            'download_bytes': len(self.zip_all),
            'files': [{'name': 'Lastbook-All.zip', 'url': f'{self.base}/Lastbook-All.zip', 'size': len(self.zip_all), 'sha256': sha(self.zip_all), 'kind': 'zip'}]}]
        self.topics = self.tmp / 'topics'
        self.topics.mkdir()
        (self.topics / 'water.json').write_text(json.dumps({'read': [{'doc': 'doc-1'}, {'doc': 'video-v4'}]}))

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def setup(self, mode='first-run', current=None, **kw):
        s = super().setup(mode, current, **kw)
        s._guides = custom.guide_ids(self.topics)
        return s

    def ready(self, s):
        r = s.custom_load()
        self.assertIn(r['state'], ('loading', 'ready'))
        for _ in range(500):
            st = s.custom_state()
            if st['state'] != 'loading':
                break
            import time; time.sleep(0.02)
        self.assertEqual(st['state'], 'ready', st)
        return st

    def build(self, s, selection, map_ids, kinds=None):
        j = s.start(custom_selection=selection, map_ids=map_ids)
        self.assertNotIn('code', j, j)
        if kinds:
            self.assertEqual([(i['kind'], i['id']) for i in j['items']], kinds)
        self.assertIsNotNone(s._checked_job(json.loads(json.dumps(s.job))), 'the saved job must load again')
        self.assertEqual(self.wait(s, timeout=120)['state'], 'done', s.public_job())
        lib, note = api.startup(self.settings)
        self.assertIsNone((note or {}).get('error'))
        api.opened(self.settings, lib, note)
        return lib, note

    # ------------------------------------------------------------------------------------------------------------ tests --
    def test_the_index(self):
        s = self.setup(manifest=self.mf)
        st = self.ready(s)
        ids = {g['id']: g for g in st['groups']}
        self.assertEqual(list(ids)[0], 'guides')
        self.assertEqual(ids['guides']['count'], 2)
        self.assertEqual((ids['video:medical']['count'], ids['video:fishing']['count']), (4, 2))
        self.assertEqual((ids['doc:engineering']['count'], ids['doc:medical']['count']), (4, 4))
        self.assertTrue(ids['zim:zim-1']['default'])
        self.assertFalse(ids['zim:zim-2']['default'], 'Wikipedia is big: not ticked unless chosen')
        self.assertNotIn('doc:about', ids)
        v0 = len(self.all_files['library-data/videos/medical/v0.mp4']) + len(self.all_files['library-data/text/video-v0.txt'])
        deps = sum(len(self.all_files['library-data/' + p]) for p in ('metadata/videos/v0.jpg', 'transcripts/v0.en.txt', 'transcripts/v0.en.vtt'))
        item = next(i for i in s.custom_items('video:medical')['items'] if i['id'] == 'video-v0')
        self.assertEqual(item['bytes'], v0 + deps)
        again = self.setup(manifest=self.mf)            # read once: a later setup finds it in the settings folder
        self.assertEqual(again.custom_state()['state'], 'ready')

    def test_a_new_custom_library(self):
        s = self.setup(manifest=self.mf)
        self.ready(s)
        sel = {'groups': ['guides', 'video:medical', 'zim:zim-1'], 'add': ['doc-2'], 'remove': ['video-v2']}
        lib, _ = self.build(s, sel, ['AA'], [('custom', 'custom'), ('map', 'AA'), ('addresses', 'AA')])
        cat = json.loads((lib / 'library-data/catalog.json').read_text())
        ids = {r['id'] for r in cat}
        self.assertEqual(ids, {'doc-1', 'video-v4', 'video-v0', 'video-v1', 'video-v3', 'zim-1', 'doc-2', 'lastbook-sources',
                               'metadata-video-v0', 'metadata-video-v1', 'metadata-video-v3', 'metadata-video-v4',
                               'transcript-v0', 'transcript-v1', 'transcript-v3', 'transcript-v4'})
        for r in cat:
            for rel in custom.record_files(r):
                self.assertEqual((lib / rel).read_bytes(), self.all_files[rel], rel)
        self.assertFalse((lib / 'library-data/videos/medical/v2.mp4').exists())
        self.assertFalse((lib / 'collections').exists() and (lib / 'library-data/collections/wikipedia.zim').exists())
        self.assertEqual(len(list((lib / 'library-data/courses/c1/img').iterdir())), 30)
        self.assertTrue((lib / 'library-data/geography/resources/hazards.json').is_file())
        self.assertEqual(install.map_states(lib), ['AA'])             # the state's map, not All's
        self.assertEqual((lib / 'derived/geo/national.sqlite').read_bytes(), (self.src['AA'] / 'derived/geo/national.sqlite').read_bytes())
        db = sqlite3.connect(lib / 'library-data/search.sqlite')
        self.assertEqual({r[0] for r in db.execute('SELECT DISTINCT doc_id FROM pages')}, ids - {i for i in ids if i.startswith('metadata-')})
        self.assertEqual(db.execute("SELECT count(*) FROM pages WHERE pages MATCH 'doc'").fetchone()[0], 2)
        pack = json.loads((lib / 'pack.json').read_text())
        self.assertEqual((pack['name'], pack['selection']['remove']), ('custom', ['video-v2']))
        for rel, size, digest in install.read_pack_manifest(lib / 'pack-manifest.tsv'):
            self.assertEqual(hashlib.sha256((lib / rel).read_bytes()).hexdigest(), digest, rel)
        self.assertFalse(list(lib.rglob('*.part*')) + list(lib.rglob('*.partial')))
        info = self.setup('manage', current=lib, manifest=self.mf).current_info()
        self.assertEqual((info['pack'], info['maps']), ('custom', ['AA']))
        asked = [r for n, r, _ in self.files.log if n == 'Lastbook-All.zip']
        self.assertTrue(asked and all(r and r.startswith('bytes=') and r.split('-')[1] for r in asked), 'only closed ranges of All')

    def test_changing_a_custom_library_links_what_stays(self):
        s = self.setup(manifest=self.mf)
        self.ready(s)
        lib, _ = self.build(s, {'groups': ['video:medical', 'zim:zim-1'], 'add': [], 'remove': []}, ['AA'])
        before = {rel: os.stat(lib / rel).st_ino for rel in ('library-data/videos/medical/v0.mp4', 'library-data/collections/mdwiki.zim')}
        self.files.log.clear()
        s = self.setup('manage', current=lib, manifest=self.mf)
        st = s.custom_state()
        self.assertEqual(st['selection']['groups'], ['video:medical', 'zim:zim-1'])
        new, note = self.build(s, {'groups': ['video:medical', 'zim:zim-1', 'video:fishing'], 'add': [], 'remove': []}, ['AA'],
                               [('custom', 'custom')])
        self.assertTrue(Path(note['old']).name.startswith('LastbookData.old-'))
        for rel, ino in before.items():
            self.assertEqual(os.stat(new / rel).st_ino, ino, f'{rel} is linked, not downloaded again')
        self.assertTrue((new / 'library-data/videos/fishing/v4.mp4').is_file())
        self.assertEqual(install.map_states(new), ['AA'], 'the map stays as it was')
        self.assertTrue((new / 'derived/geo/addresses/AA.sqlite').is_file())
        fetched = sum(int(r.split('=')[1].split('-')[1]) - int(r.split('=')[1].split('-')[0]) + 1 for n, r, _ in self.files.log if n == 'Lastbook-All.zip' and r)
        self.assertLess(fetched, sum(len(self.all_files[f'library-data/videos/medical/v{i}.mp4']) for i in range(4)), 'the medical videos came from the old library')

    def test_a_stop_carries_on(self):
        s = self.setup(manifest=self.mf)
        self.ready(s)
        real = custom.split_member
        n = [0]

        def pause_after_three(*a):
            real(*a)
            n[0] += 1
            if n[0] == 3:
                s.control.stop('paused')
        with mock.patch.object(custom, 'split_member', pause_after_three):
            s.start(custom_selection={'groups': ['video:medical', 'doc:engineering', 'zim:zim-1'], 'add': [], 'remove': []}, map_ids=['AA'])
            self.assertEqual(self.wait(s)['state'], 'paused')
        again = self.setup(manifest=self.mf)
        self.assertEqual(again.public_job()['state'], 'paused')
        self.files.log.clear()
        again.resume()
        self.assertEqual(self.wait(again, timeout=120)['state'], 'done', again.public_job())
        lib, _ = api.startup(self.settings)
        for rel, size, digest in install.read_pack_manifest(lib / 'pack-manifest.tsv'):
            self.assertEqual(hashlib.sha256((lib / rel).read_bytes()).hexdigest(), digest, rel)

    def test_a_damaged_answer_is_fetched_again(self):
        s = self.setup(manifest=self.mf)
        self.ready(s)
        self.files.corrupt['Lastbook-All.zip'] = 1         # the next answer has one flipped byte
        lib, _ = self.build(s, {'groups': ['doc:engineering'], 'add': [], 'remove': []}, ['AA'])
        for r in json.loads((lib / 'library-data/catalog.json').read_text()):
            for rel in custom.record_files(r):
                self.assertEqual((lib / rel).read_bytes(), self.all_files[rel])

    def test_the_zip_on_the_server_changed(self):
        s = self.setup(manifest=self.mf)
        self.ready(s)
        self.files.etag['Lastbook-All.zip'] = '"e2"'
        with fast_backoff():
            s.start(custom_selection={'groups': ['doc:engineering'], 'add': [], 'remove': []}, map_ids=['AA'])
            j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'changed'))

    def test_what_is_refused(self):
        s = self.setup(manifest=self.mf)
        self.assertEqual(s.start(custom_selection={'groups': ['guides']}, map_ids=['AA'])['code'], 'custom_index')
        self.ready(s)
        self.assertEqual(s.start(custom_selection={'groups': 'guides'}, map_ids=['AA'])['code'], 'invalid')
        self.assertEqual(s.start(custom_selection={'groups': []}, map_ids=['AA'])['code'], 'nothing')
        self.assertEqual(s.start(custom_selection={'groups': ['guides']}, map_ids=[])['code'], 'invalid')
        self.assertEqual(s.start('small', custom_selection={'groups': ['guides']}, map_ids=['AA'])['code'], 'invalid')

    def test_a_bad_directory_is_refused(self):
        bad = self.tmp / 'bad.zip'
        with zipfile.ZipFile(bad, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('LastbookData/pack-manifest.tsv', 'path\tbytes\tsha256\n')
            z.writestr('LastbookData/library-data/catalog.json', '[]' * 1000)
        data = bad.read_bytes()
        self.files.data['Lastbook-All.zip'] = data
        p = {'files': [{'url': f'{self.base}/Lastbook-All.zip', 'size': len(data), 'sha256': sha(data)}]}
        with self.assertRaises(install.Refused):
            custom.read_index(p, self.tmp / 'idx', download.Control(), allowed=ALLOWED, context=self.ctx)
        self.assertFalse((self.tmp / 'idx').exists())

    def test_plan_tasks(self):
        s = self.setup(manifest=self.mf)
        self.ready(s)
        idx = s.index()
        rels = [r for r in idx.entries if r.startswith('library-data/courses/')] + ['library-data/videos/medical/v0.mp4']
        tasks = custom.plan_tasks(idx, rels)
        self.assertEqual([t for t in tasks if t[0] == 'big'], [('big', 'library-data/videos/medical/v0.mp4')])
        runs = [t for t in tasks if t[0] == 'run']
        self.assertEqual(sorted(r for t in runs for r in t[1]), sorted(rels[:-1]))
        self.assertTrue(all(t[3] - t[2] <= custom.RUN_MAX + idx.entries[t[1][-1]][1] + 200 for t in runs))


if __name__ == '__main__':
    unittest.main()
