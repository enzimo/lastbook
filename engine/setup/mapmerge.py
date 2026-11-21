"""Combining state map sets, so a library can hold the detailed maps of several states at once (enzimo fork).

Every published state map set is the base map plus every row inside one state's box, cut from the same whole-US map set
(packaging/build_packs.py: base_set, state_set). Rows keep the ids they have there, and two sets that both hold a row hold the
same row. So two sets combine by adding to one the rows of the other whose key it lacks: the overlap at a border, and the base
both carry, are kept once. Nothing is rebuilt.

The base map alone is not published, but it is a subset of any state set by rule (base_set): roads to zoom 8, water to zoom 9,
relief to zoom 7, national places, and no pins, buildings, crops or resources. derive_base cuts it out of a set, which is how a
library that drops every state, or keeps some and drops others, gets its starting point.

All of it works on a folder laid out like LastbookData that holds only map-set files (install.map_member). Every step can run
again after a stop: adding rows that are there already adds nothing.
"""
import json
import os
import shutil
import sqlite3
from pathlib import Path

from . import install

GEO = 'derived/geo'
ATLAS = 'library-data/geography/resources/atlas.sqlite'
# The files of a map set that base_set leaves out altogether.
NOT_IN_BASE = (f'{GEO}/buildings.sqlite', f'{GEO}/crops.sqlite', ATLAS)
# Tables of no geographic content: kept as the first set has them (the status line is worked out again in finish()).
SKIP = {'meta', 'metadata'}
# What base_set keeps of each file, as statements that remove the rest.
BASE_RULES = {
    f'{GEO}/national.sqlite': ['DELETE FROM feat WHERE minz>8', 'DELETE FROM feat_idx WHERE id NOT IN (SELECT id FROM feat)', 'DELETE FROM poi'],
    f'{GEO}/water.sqlite': ['DELETE FROM feat WHERE minz>9', 'DELETE FROM feat_idx WHERE id NOT IN (SELECT id FROM feat)',
                            'DELETE FROM resource', 'DELETE FROM spatial', 'DELETE FROM water_fts'],
    f'{GEO}/terrain.sqlite': ['DELETE FROM tile WHERE z>7'],
    f'{GEO}/places.sqlite': ["DELETE FROM place WHERE source='OpenStreetMap'", "INSERT INTO place_fts(place_fts) VALUES('rebuild')"],
    f'{GEO}/map.sqlite': ['DELETE FROM feat', 'DELETE FROM feat_idx', 'DELETE FROM poi'],
}


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _sqlite_files(lib):
    """The map set's SQLite files, relative to lib (map-set.json's own list, as on disk)."""
    return sorted(r for r in install.map_files(lib) if r.endswith('.sqlite'))


def _open(path):
    try:
        db = sqlite3.connect(str(path), isolation_level=None)
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return db
    except sqlite3.DatabaseError as e:
        raise install.Refused('unpack', f'The map file {Path(path).name} could not be opened as a database ({e}).') from None


def _tables(db, schema='main'):
    """{table: kind} for the tables that hold content: 'table', 'rtree', 'fts' (its own content) or 'fts-external' (indexes
    another table: rebuilt, not copied). A virtual table's shadow tables (name_node, name_data, ...) are left out."""
    rows = db.execute(f'SELECT name, sql FROM {schema}.sqlite_master WHERE type=\'table\' AND sql IS NOT NULL').fetchall()
    virtual = {n: s for n, s in rows if s.upper().startswith('CREATE VIRTUAL TABLE')}
    out = {}
    for name, sql in rows:
        if name in SKIP or name.startswith('sqlite_') or any(name.startswith(v + '_') for v in virtual):
            continue
        s = sql.upper()
        if name in virtual:
            if 'USING RTREE' in s:
                out[name] = 'rtree'
            elif 'USING FTS5' in s:
                out[name] = 'fts-external' if 'CONTENT=' in s.replace(' ', '') else 'fts'
        else:
            out[name] = 'table'
    return out


def _columns(db, table, schema='main'):
    return [r[1] for r in db.execute(f'PRAGMA {schema}.table_info({_q(table)})')]


def merge_file(target, source):
    """Adds to the database `target` the rows of `source` it does not hold yet, table by table."""
    db = _open(target)
    try:
        db.execute('ATTACH DATABASE ? AS src', (str(source),))
        mine, theirs = _tables(db), _tables(db, 'src')
        db.execute('BEGIN')
        rebuild = []
        for table, kind in theirs.items():
            if mine.get(table) != kind:
                if table in mine or kind == 'fts-external':
                    raise install.Refused('unpack', f'The maps do not fit together: {Path(target).name} has no {table} like the other.')
                continue
            if kind == 'fts-external':
                rebuild.append(table)
                continue
            cols = _columns(db, table)
            if cols != _columns(db, table, 'src'):
                raise install.Refused('unpack', f'The maps do not fit together: {table} in {Path(target).name} has other columns.')
            names = ', '.join(_q(c) for c in cols)
            t = _q(table)
            if kind == 'table':
                db.execute(f'INSERT OR IGNORE INTO main.{t} ({names}) SELECT {names} FROM src.{t}')
            elif kind == 'rtree':      # an R-tree's first column is its id
                key = _q(cols[0])
                db.execute(f'INSERT INTO main.{t} ({names}) SELECT {names} FROM src.{t} WHERE {key} NOT IN (SELECT {key} FROM main.{t})')
            else:                      # an FTS5 table with its own content, keyed by rowid
                db.execute(f'INSERT INTO main.{t} (rowid, {names}) SELECT rowid, {names} FROM src.{t} '
                           f'WHERE rowid NOT IN (SELECT rowid FROM main.{t})')
        for table in rebuild:
            db.execute(f"INSERT INTO main.{_q(table)}({_q(table)}) VALUES('rebuild')")
        db.execute('COMMIT')
        db.execute('DETACH DATABASE src')
    except sqlite3.DatabaseError as e:
        raise install.Refused('unpack', f'The maps could not be combined ({type(e).__name__}: {e}).') from None
    finally:
        db.close()


def merge_into(target, source):
    """Adds the map set in `source` to the one in `target` (both LastbookData folders holding map files). A file only `source`
    has (a state's buildings, when `target` is the base alone) is copied over."""
    target, source = Path(target), Path(source)
    install.check_map(source)
    for rel in _sqlite_files(source):
        if (target / rel).is_file():
            merge_file(target / rel, source / rel)
        else:
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            tmp = target / (rel + '.partial')
            shutil.copyfile(source / rel, tmp)
            os.replace(tmp, target / rel)
    for rel in install.MAP_FIXED - {'map-set.json'}:          # the small JSON files: the first set's stay (finish() redoes status)
        if rel.endswith('.json') and not (target / rel).exists() and (source / rel).is_file():
            shutil.copyfile(source / rel, target / rel)


def derive_base(lib):
    """Cuts the map set in `lib` down to the base map, in place (BASE_RULES), and drops the files the base has none of."""
    lib = Path(lib)
    for rel in NOT_IN_BASE:
        for side in ('',) + install.SQLITE_SIDES:
            try:
                os.unlink(lib / (rel + side))
            except FileNotFoundError:
                pass
    for rel, statements in BASE_RULES.items():
        if not (lib / rel).is_file():
            continue
        db = _open(lib / rel)
        try:
            db.execute('BEGIN')
            for s in statements:
                db.execute(s)
            db.execute('COMMIT')
            db.execute('VACUUM')
        except sqlite3.DatabaseError as e:
            raise install.Refused('unpack', f'The base map could not be made from {Path(rel).name} ({e}).') from None
        finally:
            db.close()


def finish(lib, states):
    """Writes the combined set's map-set.json and its status line. states: the state codes it now holds (none: the base).
    map-set.json keeps the shape setup checks (name, files with their sizes) and adds `states`."""
    lib = Path(lib)
    states = sorted(set(states))
    features = 0
    national = lib / GEO / 'national.sqlite'
    if national.is_file():
        db = _open(national)
        try:
            features = db.execute('SELECT count(*) FROM feat').fetchone()[0]
            status = json.dumps({'state': 'complete', 'stage': 'ready', 'features': features})
            if 'meta' in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('status', ?)", (status,))
        finally:
            db.close()
    (lib / GEO).mkdir(parents=True, exist_ok=True)
    (lib / GEO / 'national-status.json').write_text(json.dumps({'state': 'complete', 'stage': 'ready', 'features': features}), encoding='utf-8')
    if not (lib / GEO / 'socal-cells.json').exists():
        (lib / GEO / 'socal-cells.json').write_text('[]', encoding='utf-8')
    for rel in [r for r in install.map_files(lib) if r.endswith(install.SQLITE_SIDES)]:
        os.unlink(lib / rel)        # every database was closed in DELETE mode: a side file left is stale
    files = {}
    for rel in install.map_files(lib):
        if rel != 'map-set.json':
            files[rel] = (lib / rel).stat().st_size
    name = '+'.join(states) if states else 'base'
    install._write_json(lib / 'map-set.json', {'name': name, 'states': states, 'files': files, 'bytes': sum(files.values())})
    install.check_map(lib)
    return name
