#!/usr/bin/env python3
"""Several state maps in one library (enzimo fork): engine/setup/mapmerge.py, and setup's checklist of states
(Setup.start(map_ids=[...])) against the local HTTPS test server of test_setup.py. No real internet. Never packaged.

The state maps here are small but real SQLite map sets, cut from one made-up "whole US" the way packaging/build_packs.py cuts
the published ones (base rows plus every row in a state's box), with the published schemas.

  python -B app/tests/test_map_checklist.py [-v]
"""
import io, json, os, shutil, sqlite3, sys, unittest, zipfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(ROOT)]
from engine.setup import api, install, manifest, mapmerge  # noqa: E402
from app.tests import shorttemp  # noqa: E402
from app.tests.test_setup import ALLOWED, Base, make_pack, sha  # noqa: E402

SCHEMA = {   # as in the published map sets (shadow tables come with the virtual ones)
    'derived/geo/national.sqlite': [
        'CREATE TABLE feat(id INTEGER PRIMARY KEY,layer TEXT,cls TEXT,name TEXT,minz INTEGER,geom BLOB)',
        'CREATE VIRTUAL TABLE feat_idx USING rtree(id,minx,maxx,miny,maxy,minz,maxz)',
        'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)',
        'CREATE TABLE poi(id INTEGER PRIMARY KEY,cls TEXT,name TEXT,lon REAL,lat REAL)'],
    'derived/geo/water.sqlite': [
        'CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB, origin TEXT)',
        'CREATE VIRTUAL TABLE feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz)',
        'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)',
        'CREATE TABLE resource(id INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, name TEXT, lat REAL, lon REAL, detail TEXT, source TEXT, origin TEXT)',
        'CREATE VIRTUAL TABLE spatial USING rtree(id, minx, maxx, miny, maxy)',
        'CREATE VIRTUAL TABLE water_fts USING fts5(name, other, tokenize="unicode61")'],
    'derived/geo/terrain.sqlite': [
        'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)',
        'CREATE TABLE tile(z INT, x INT, y INT, png BLOB, PRIMARY KEY(z,x,y)) WITHOUT ROWID'],
    'derived/geo/buildings.sqlite': [
        'CREATE TABLE bldg(x INT, y INT, part INT, n INT, data BLOB, PRIMARY KEY(x,y,part)) WITHOUT ROWID',
        'CREATE TABLE bldg_osm(x INT, y INT, n INT, data BLOB, PRIMARY KEY(x,y)) WITHOUT ROWID',
        'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)'],
    'derived/geo/crops.sqlite': [
        'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)',
        'CREATE TABLE resource(id INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, name TEXT, lat REAL, lon REAL, detail TEXT, source TEXT)',
        'CREATE VIRTUAL TABLE spatial USING rtree(id, minx, maxx, miny, maxy)',
        'CREATE TABLE tile(z INT, x INT, y INT, png BLOB, PRIMARY KEY(z,x,y)) WITHOUT ROWID'],
    'derived/geo/places.sqlite': [
        'CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT)',
        "CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61')"],
    'derived/geo/map.sqlite': [
        'CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB)',
        'CREATE VIRTUAL TABLE feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz)',
        'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)',
        'CREATE TABLE poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL)'],
    'library-data/geography/resources/atlas.sqlite': [
        'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)',
        'CREATE TABLE resource(id INTEGER PRIMARY KEY,source_id TEXT,kind TEXT,name TEXT,lat REAL,lon REAL,detail TEXT,source TEXT)',
        'CREATE VIRTUAL TABLE spatial USING rtree(id,minx,maxx,miny,maxy)'],
}
# Made-up states side by side along the equator, each box 10 degrees wide; AA and BB overlap by one degree.
BOXES = {'AA': (0, 0, 10, 10), 'BB': (9, 0, 19, 10), 'CC': (40, 0, 50, 10)}


def us_rows():
    """The made-up whole US: base rows (low zoom, national places) everywhere, detail rows spread over 0..50 degrees."""
    rows = {k: [] for k in ('national.feat', 'national.poi', 'water.feat', 'water.resource', 'terrain.tile', 'bldg', 'bldg_osm',
                            'crops.resource', 'crops.tile', 'place', 'atlas.resource')}
    for i in range(1, 121):
        x = (i * 7) % 50 + 0.5
        rows['national.feat'].append((i, 'road', 'primary', f'road {i}', 4 + i % 10, bytes([i % 256]) * 20, x))    # minz 4..13
        rows['water.feat'].append((i, 'water', 'river', f'river {i}', 5 + i % 9, bytes([i % 7]) * 10, 'nhd', x))   # minz 5..13
        rows['place'].append((i, f'Town {i}', 'town', 'detail', 5.0, x, 1, 'OpenStreetMap' if i % 3 else 'USGS'))
    for i in range(1, 61):
        x = (i * 11) % 50 + 0.25
        rows['national.poi'].append((i, 'hospital', f'Hospital {i}', x, 5.0))
        rows['water.resource'].append((i, f's{i}', 'well', f'Well {i}', 5.0, x, '{}', 'usgs', 'x'))
        rows['crops.resource'].append((i, f'c{i}', 'farm', f'Farm {i}', 5.0, x, '{}', 'usda'))
        rows['atlas.resource'].append((i, f'a{i}', 'shelter', f'Shelter {i}', 5.0, x, '{}', 'fema'))
    for z in range(4, 13):
        for x in range(0, 2 ** min(z, 6), 3):
            rows['terrain.tile'].append((z, x, 1, bytes([z, x % 256]) * 8))
    for x in range(0, 50, 2):
        rows['bldg'].append((x, 1, 0, 3, bytes([x]) * 12))
        rows['bldg_osm'].append((x, 1, 2, bytes([x + 1]) * 12))
        rows['crops.tile'].append((12, x, 1, bytes([x + 2]) * 6))
    return rows


def write_set(lib, state, rows):
    """A map set: the base everywhere, plus each row inside `state`'s box (none: the base alone, which has no buildings,
    crops or atlas, as base_set makes it)."""
    lib = Path(lib)
    box = BOXES.get(state)
    inbox = (lambda x: box[0] <= x <= box[2]) if box else (lambda x: False)
    tile_x = (lambda z, x: box[0] <= x * 360 / 2 ** z - 180 + 180 <= box[2] * 2 ** z) if box else (lambda z, x: False)
    data = {
        'derived/geo/national.sqlite': {'feat': [r[:6] for r in rows['national.feat'] if r[4] <= 8 or inbox(r[6])],
                                        'feat_idx': [(r[0], r[6], r[6] + 0.1, 5, 5.1, r[4], 20) for r in rows['national.feat'] if r[4] <= 8 or inbox(r[6])],
                                        'poi': [r for r in rows['national.poi'] if inbox(r[3])],
                                        'meta': [('status', '{}')]},
        'derived/geo/water.sqlite': {'feat': [r[:7] for r in rows['water.feat'] if r[4] <= 9 or inbox(r[7])],
                                     'feat_idx': [(r[0], r[7], r[7] + 0.1, 5, 5.1, r[4], 20) for r in rows['water.feat'] if r[4] <= 9 or inbox(r[7])],
                                     'resource': [r for r in rows['water.resource'] if inbox(r[5])],
                                     'spatial': [(r[0], r[5], r[5], 5, 5) for r in rows['water.resource'] if inbox(r[5])],
                                     'meta': [('source', 'nhd'), ('built', '1')]},
        'derived/geo/terrain.sqlite': {'tile': [r for r in rows['terrain.tile'] if r[0] <= 7 or (box and box[0] <= r[1] <= box[2])],
                                       'meta': [('source', 'USGS')]},
        'derived/geo/places.sqlite': {'place': [r for r in rows['place'] if r[7] != 'OpenStreetMap' or inbox(r[5])]},
        'derived/geo/map.sqlite': {'meta': []},
    }
    if box:
        data['derived/geo/buildings.sqlite'] = {'bldg': [r for r in rows['bldg'] if inbox(r[0])], 'bldg_osm': [r for r in rows['bldg_osm'] if inbox(r[0])],
                                                'meta': [('count', '1')]}
        data['derived/geo/crops.sqlite'] = {'resource': [r for r in rows['crops.resource'] if inbox(r[5])],
                                            'spatial': [(r[0], r[5], r[5], 5, 5) for r in rows['crops.resource'] if inbox(r[5])],
                                            'tile': [r for r in rows['crops.tile'] if inbox(r[1])], 'meta': [('built', '{}')]}
        data['library-data/geography/resources/atlas.sqlite'] = {'resource': [r for r in rows['atlas.resource'] if inbox(r[5])],
                                                                 'spatial': [(r[0], r[5], r[5], 5, 5) for r in rows['atlas.resource'] if inbox(r[5])],
                                                                 'metadata': [('source', 'fema')]}
    for rel, tables in data.items():
        p = lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(p)
        for sql in SCHEMA[rel]:
            db.execute(sql)
        for t, rs in tables.items():
            for r in rs:
                db.execute(f'INSERT INTO {t} VALUES ({",".join("?" * len(r))})', r)
        if rel.endswith('water.sqlite'):
            db.execute('INSERT INTO water_fts(rowid, name, other) SELECT id, name, kind FROM resource')
        if rel.endswith('places.sqlite'):
            db.execute("INSERT INTO place_fts(place_fts) VALUES('rebuild')")
        db.commit()
        db.close()
    (lib / 'derived/geo/national-status.json').write_text('{"state": "complete"}')
    (lib / 'derived/geo/socal-cells.json').write_text('[]')
    files = {r: (lib / r).stat().st_size for r in install.map_files(lib) if r != 'map-set.json'}
    (lib / 'map-set.json').write_text(json.dumps({'name': state or 'base', 'files': files}))
    return lib


def rows_of(lib, rel, table):
    db = sqlite3.connect(f'file:{Path(lib) / rel}?mode=ro', uri=True)
    try:
        return sorted(db.execute(f'SELECT * FROM {table}').fetchall())
    finally:
        db.close()


TABLES = [('derived/geo/national.sqlite', 'feat'), ('derived/geo/national.sqlite', 'poi'), ('derived/geo/water.sqlite', 'feat'),
          ('derived/geo/water.sqlite', 'resource'), ('derived/geo/terrain.sqlite', 'tile'), ('derived/geo/places.sqlite', 'place')]
DETAIL = [('derived/geo/buildings.sqlite', 'bldg'), ('derived/geo/crops.sqlite', 'resource'), ('library-data/geography/resources/atlas.sqlite', 'resource')]


class Merging(unittest.TestCase):
    def setUp(self):
        self.tmp = shorttemp.mkdtemp('lastbook-mapmerge-')
        self.rows = us_rows()
        self.sets = {st: write_set(self.tmp / st / 'LastbookData', st, self.rows) for st in BOXES}
        self.base = write_set(self.tmp / 'base' / 'LastbookData', None, self.rows)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy(self, st):
        dest = self.tmp / ('copy-' + st) / 'LastbookData'
        shutil.copytree(self.sets[st], dest)
        return dest

    def test_two_states_hold_every_row_of_each_once(self):
        lib = self.copy('AA')
        mapmerge.merge_into(lib, self.sets['BB'])
        self.assertEqual(mapmerge.finish(lib, ['BB', 'AA']), 'AA+BB')
        for rel, t in TABLES + DETAIL:
            want = sorted(set(rows_of(self.sets['AA'], rel, t)) | set(rows_of(self.sets['BB'], rel, t)))
            self.assertEqual(rows_of(lib, rel, t), want, f'{rel} {t}')
        overlap = set(rows_of(self.sets['AA'], 'derived/geo/national.sqlite', 'poi')) & set(rows_of(self.sets['BB'], 'derived/geo/national.sqlite', 'poi'))
        self.assertTrue(overlap, 'the made-up boxes are meant to share rows at the border')

    def test_indexes_follow(self):
        lib = self.copy('AA')
        mapmerge.merge_into(lib, self.sets['CC'])
        mapmerge.finish(lib, ['AA', 'CC'])
        db = sqlite3.connect(lib / 'derived/geo/national.sqlite')
        self.assertEqual(db.execute('SELECT count(*) FROM feat_idx').fetchone(), db.execute('SELECT count(*) FROM feat').fetchone())
        self.assertEqual(db.execute("SELECT rtreecheck('feat_idx')").fetchone(), ('ok',))
        w = sqlite3.connect(lib / 'derived/geo/water.sqlite')
        self.assertEqual(w.execute('SELECT count(*) FROM water_fts').fetchone(), w.execute('SELECT count(*) FROM resource').fetchone())
        w.execute("INSERT INTO water_fts(water_fts) VALUES('integrity-check')")
        p = sqlite3.connect(lib / 'derived/geo/places.sqlite')
        cc_town = next(r[1] for r in rows_of(self.sets['CC'], 'derived/geo/places.sqlite', 'place') if r[7] == 'OpenStreetMap')
        self.assertEqual(p.execute('SELECT count(*) FROM place_fts WHERE place_fts MATCH ?', (f'"{cc_town}"',)).fetchone()[0], 1)

    def test_merging_twice_adds_nothing(self):
        lib = self.copy('AA')
        mapmerge.merge_into(lib, self.sets['BB'])
        once = {t: rows_of(lib, rel, t) for rel, t in TABLES}
        mapmerge.merge_into(lib, self.sets['BB'])
        self.assertEqual({t: rows_of(lib, rel, t) for rel, t in TABLES}, once)

    def test_base_cut_out_of_any_set_is_the_base(self):
        lib = self.copy('AA')
        mapmerge.merge_into(lib, self.sets['CC'])
        mapmerge.derive_base(lib)
        self.assertEqual(mapmerge.finish(lib, []), 'base')
        for rel, t in TABLES:
            self.assertEqual(rows_of(lib, rel, t), rows_of(self.base, rel, t), f'{rel} {t}')
        for rel, _ in DETAIL:
            self.assertFalse((lib / rel).exists(), rel)
        self.assertEqual(install.map_states(lib), [])

    def test_a_state_added_to_the_base_brings_its_own_files(self):
        lib = self.tmp / 'b2' / 'LastbookData'
        shutil.copytree(self.base, lib)
        mapmerge.merge_into(lib, self.sets['BB'])
        mapmerge.finish(lib, ['BB'])
        self.assertEqual(rows_of(lib, 'derived/geo/buildings.sqlite', 'bldg'), rows_of(self.sets['BB'], 'derived/geo/buildings.sqlite', 'bldg'))

    def test_map_set_json(self):
        lib = self.copy('AA')
        mapmerge.merge_into(lib, self.sets['BB'])
        mapmerge.finish(lib, ['AA', 'BB'])
        d = json.loads((lib / 'map-set.json').read_text())
        self.assertEqual((d['name'], d['states']), ('AA+BB', ['AA', 'BB']))
        install.check_map(lib)
        self.assertEqual(install.map_states(lib), ['AA', 'BB'])
        self.assertFalse([r for r in install.map_files(lib) if r.endswith(install.SQLITE_SIDES)])
        status = json.loads((lib / 'derived/geo/national-status.json').read_text())
        self.assertEqual(status['features'], len(rows_of(lib, 'derived/geo/national.sqlite', 'feat')))

    def test_a_set_that_does_not_fit_is_refused(self):
        lib = self.copy('AA')
        db = sqlite3.connect(self.sets['BB'] / 'derived/geo/terrain.sqlite')
        db.execute('ALTER TABLE tile ADD COLUMN extra TEXT')
        db.commit()
        db.close()
        with self.assertRaises(install.Refused):
            mapmerge.merge_into(lib, self.sets['BB'])


class StateMaps(Base):
    """Three made-up states served as published state maps, with an address index each. No tests of its own."""

    def setUp(self):
        super().setUp()
        self.rows = us_rows()
        maps, addresses = [], []
        for st in BOXES:
            lib = write_set(self.tmp / 'src' / st / 'LastbookData', st, self.rows)
            name = f'Lastbook-Map-{st}.zip'
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z:
                for root, _, fs in os.walk(lib):
                    for f in fs:
                        z.write(Path(root, f), Path(root, f).relative_to(lib).as_posix())   # files at the top, as the published zips
            data = buf.getvalue()
            self.files.data[name] = data
            installed = sum((lib / r).stat().st_size for r in install.map_files(lib))
            maps.append({'id': st, 'title': f'State {st}', 'kind': 'state', 'includes': 'x', 'installed_bytes': installed, 'download_bytes': len(data),
                         'available': True, 'files': [{'name': name, 'url': f'{self.base}/{name}', 'size': len(data), 'sha256': sha(data), 'kind': 'zip'}]})
            aname, payload = f'Lastbook-Addresses-{st}.zip', f'addresses of {st}'.encode() * 100
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr(f'derived/geo/addresses/{st}.sqlite', payload)
            adata = buf.getvalue()
            self.files.data[aname] = adata
            addresses.append({'id': st, 'title': f'State {st}', 'available': True, 'installed_bytes': len(payload), 'download_bytes': len(adata),
                              'files': [{'name': aname, 'url': f'{self.base}/{aname}', 'size': len(adata), 'sha256': sha(adata), 'kind': 'zip'}]})
        m = self.manifest().current
        m['maps'], m['address_indexes'] = maps, addresses
        self.mf = manifest.Manifest(bundled=m, remote=None, allowed=ALLOWED, context=self.ctx)
        self.src = {st: self.tmp / 'src' / st / 'LastbookData' for st in BOXES}

    def library(self, states=()):
        """An installed Small library (its own base map), with the given states' map and address search put in."""
        files = {'library-data/catalog.json': b'{"records": []}', 'pack.json': b'{"name": "small", "map": "base"}'}
        lib = self.parent / 'LastbookData'
        for k, v in files.items():
            (lib / k).parent.mkdir(parents=True, exist_ok=True)
            (lib / k).write_bytes(v)
        shutil.copytree(write_set(self.tmp / 'own-base' / 'LastbookData', None, self.rows), lib, dirs_exist_ok=True)
        if states:
            shutil.copytree(self.src[states[0]], lib, dirs_exist_ok=True)
            for st in states[1:]:
                mapmerge.merge_into(lib, self.src[st])
            if len(states) > 1:
                mapmerge.finish(lib, list(states))
            for st in states:
                s = self.setup('manage', current=lib, manifest=self.mf)
                s.start(address_id=st)
                self.assertEqual(self.wait(s)['state'], 'done')
        return lib

    def go(self, s, map_ids, kinds, **kw):
        j = s.start(map_ids=map_ids, **kw)
        self.assertNotIn('code', j, j)
        self.assertEqual([(i['kind'], i['id']) for i in j['items']], kinds)
        self.assertIsNotNone(s._checked_job(json.loads(json.dumps(s.job))), 'the saved job must load again')
        self.assertEqual(self.wait(s, timeout=120)['state'], 'done', s.public_job())
        lib, note = api.startup(self.settings)
        self.assertIsNone((note or {}).get('error'))
        api.opened(self.settings, lib, note)
        return lib


class Checklist(StateMaps):
    """Setup with the checklist: start(map_ids=[...])."""

    def test_add_a_second_state(self):
        lib = self.library(['AA'])
        s = self.setup('manage', current=lib, manifest=self.mf)
        self.assertEqual(s.current_info()['maps'], ['AA'])
        lib = self.go(s, ['AA', 'BB'], [('map', 'BB'), ('mapset', 'combine'), ('addresses', 'BB')])
        self.assertEqual(install.map_states(lib), ['AA', 'BB'])
        for rel, t in TABLES + DETAIL:
            want = sorted(set(rows_of(self.src['AA'], rel, t)) | set(rows_of(self.src['BB'], rel, t)))
            self.assertEqual(rows_of(lib, rel, t), want, f'{rel} {t}')
        info = self.setup('manage', current=lib, manifest=self.mf).current_info()
        self.assertEqual((info['maps'], info['address_indexes'], info['map']), (['AA', 'BB'], ['AA', 'BB'], 'AA+BB'))
        self.assertFalse((self.parent / 'LastbookData.setup').exists())

    def test_the_same_states_again_is_nothing(self):
        lib = self.library(['AA'])
        self.assertEqual(self.setup('manage', current=lib, manifest=self.mf).start(map_ids=['AA'])['code'], 'nothing')

    def test_dropping_a_state_rebuilds_from_the_rest(self):
        lib = self.library(['AA', 'BB', 'CC'])
        s = self.setup('manage', current=lib, manifest=self.mf)
        lib = self.go(s, ['AA', 'CC'], [('map', 'AA'), ('map', 'CC'), ('mapset', 'combine')])
        self.assertEqual(install.map_states(lib), ['AA', 'CC'])
        bb_only = set(rows_of(self.src['BB'], 'derived/geo/national.sqlite', 'poi')) - set(rows_of(self.src['AA'], 'derived/geo/national.sqlite', 'poi'))
        self.assertFalse(bb_only & set(rows_of(lib, 'derived/geo/national.sqlite', 'poi')))
        self.assertFalse((lib / 'derived/geo/addresses/BB.sqlite').exists())
        self.assertTrue((lib / 'derived/geo/addresses/AA.sqlite').exists())

    def test_dropping_to_one_state_is_that_states_own_map(self):
        lib = self.library(['AA', 'BB'])
        s = self.setup('manage', current=lib, manifest=self.mf)
        lib = self.go(s, ['BB'], [('map', 'BB')])
        self.assertEqual((install.map_name(lib), install.map_states(lib)), ('BB', ['BB']))
        for rel, t in TABLES:
            self.assertEqual(rows_of(lib, rel, t), rows_of(self.src['BB'], rel, t))
        self.assertEqual(sorted(p.name for p in (lib / 'derived/geo/addresses').iterdir()), ['BB.json', 'BB.sqlite'])

    def test_dropping_every_state_leaves_the_base(self):
        lib = self.library(['AA', 'BB'])
        s = self.setup('manage', current=lib, manifest=self.mf)
        lib = self.go(s, [], [('mapset', 'combine')])
        self.assertEqual((install.map_name(lib), install.map_states(lib)), ('base', []))
        base = write_set(self.tmp / 'want-base' / 'LastbookData', None, self.rows)
        for rel, t in TABLES:
            self.assertEqual(rows_of(lib, rel, t), rows_of(base, rel, t), f'{rel} {t}')
        self.assertFalse((lib / 'derived/geo/addresses').exists() and any((lib / 'derived/geo/addresses').iterdir()))

    def test_first_run_with_two_states(self):
        s = self.setup(manifest=self.mf)
        lib = self.go(s, ['CC', 'AA'], [('pack', 'small'), ('map', 'AA'), ('map', 'CC'), ('mapset', 'combine'), ('addresses', 'AA'), ('addresses', 'CC')], pack_id='small')
        self.assertEqual(install.map_states(lib), ['AA', 'CC'])
        self.assertTrue((lib / 'library-data/catalog.json').is_file())
        self.assertEqual(sorted(self.setup('manage', current=lib, manifest=self.mf).address_indexes()), ['AA', 'CC'])

    def test_first_run_with_one_state_is_as_before(self):
        s = self.setup(manifest=self.mf)
        lib = self.go(s, ['BB'], [('pack', 'small'), ('map', 'BB'), ('addresses', 'BB')], pack_id='small')
        self.assertEqual(json.loads((lib / 'map-set.json').read_text())['name'], 'BB')
        self.assertEqual((lib / 'derived/geo/national.sqlite').read_bytes(), (self.src['BB'] / 'derived/geo/national.sqlite').read_bytes())

    def test_a_paused_combine_carries_on(self):
        lib = self.library(['AA'])
        s = self.setup('manage', current=lib, manifest=self.mf)
        real = mapmerge.merge_into
        calls = []

        def stop_after_first(target, source):
            calls.append(source)
            if len(calls) == 2:                # the first state is merged and noted: pause before the second
                s.control.stop('paused')
                s.control.check()
            real(target, source)
        mapmerge.merge_into = stop_after_first
        try:
            j = s.start(map_ids=['AA', 'BB', 'CC'])
            self.assertEqual([i['kind'] for i in j['items']], ['map', 'map', 'mapset', 'addresses', 'addresses'])
            self.assertEqual(self.wait(s)['state'], 'paused')
        finally:
            mapmerge.merge_into = real
        again = self.setup('manage', current=lib, manifest=self.mf)        # as after closing the app
        self.assertEqual(again.public_job()['state'], 'paused')
        self.assertEqual(len(again.job['plan']['mapset_merged']), 1)
        again.resume()
        self.assertEqual(self.wait(again, timeout=120)['state'], 'done', again.public_job())
        lib, note = api.startup(self.settings)
        self.assertEqual(install.map_states(lib), ['AA', 'BB', 'CC'])
        for rel, t in TABLES:
            want = sorted(set().union(*(set(rows_of(self.src[x], rel, t)) for x in ('AA', 'BB', 'CC'))))
            self.assertEqual(rows_of(lib, rel, t), want, f'{rel} {t}')

    def test_repair_of_a_combined_map(self):
        lib = self.library(['AA', 'BB'])
        rows = ['path\tbytes\tsha256\thow\tchanges'] + [f'{k}\t{(lib / k).stat().st_size}\t{sha((lib / k).read_bytes())}\tclone\t'
                                                        for k in ('library-data/catalog.json', 'pack.json')]
        (lib / 'pack-manifest.tsv').write_text('\n'.join(rows) + '\n')
        (lib / 'derived/geo/terrain.sqlite').unlink()
        s = self.setup('manage', current=lib, manifest=self.mf)
        j = s.start(repair=True)
        self.assertEqual([(i['kind'], i['id']) for i in j['items']], [('pack', 'small'), ('map', 'AA'), ('map', 'BB'), ('mapset', 'combine')])
        self.assertEqual(self.wait(s, timeout=120)['state'], 'done', s.public_job())
        lib, note = api.startup(self.settings)
        self.assertIsNone(note.get('error'))
        self.assertEqual(install.map_states(lib), ['AA', 'BB'])
        install.check_map(lib)

    def test_all_only_takes_address_search(self):
        lib = self.library()
        (lib / 'pack.json').write_text('{"name": "all", "map": "us"}')
        (lib / 'map-set.json').unlink()
        s = self.setup('manage', current=lib, manifest=self.mf)
        j = s.start(map_ids=['AA', 'CC'])
        self.assertEqual([(i['kind'], i['id']) for i in j['items']], [('addresses', 'AA'), ('addresses', 'CC')])
        self.assertEqual(self.wait(s)['state'], 'done')
        r = self.setup('manage', current=lib, manifest=self.mf).start(map_ids=['CC'])
        self.assertEqual(r, {'removed': ['AA']})
        self.assertFalse((lib / 'derived/geo/addresses/AA.sqlite').exists())

    def test_a_bad_list_is_refused(self):
        lib = self.library(['AA'])
        s = self.setup('manage', current=lib, manifest=self.mf)
        self.assertEqual(s.start(map_ids=['aa'])['code'], 'invalid')
        self.assertEqual(s.start(map_ids='AA')['code'], 'invalid')
        self.assertEqual(s.start(map_ids=['ZZ'])['code'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
