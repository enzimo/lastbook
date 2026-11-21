"""A Custom library: chosen record by record from the All pack, and downloaded file by file from its zip (enzimo fork).

The All pack (Lastbook-All.zip, 388 GB) is a stored zip on a server that answers byte ranges. Its central directory says where
each file starts; its pack-manifest.tsv gives each file's size and SHA-256; its catalog.json describes every record. Setup
reads those three (about 30 MB) when the person opens Custom, offers the records by group, and then fetches only the files of
what was chosen:

  - a file of BIG or more on its own, straight into the library, over several connections, checked against its SHA-256;
  - smaller files together, in runs of neighbouring zip entries (up to RUN_MAX), split here and each checked on its own.

Every request asks for a closed range of the one zip and must carry the ETag seen when the list was read, so every byte comes
from the same copy of the pack (download.Download: span, expect_etag).

What a record brings: its own files (path, text_path, and a video's captions and thumbnail), and for a video its transcript
and metadata records. What every Custom library has: the files no record names (course and page assets, reader.css), the
sources page, and its own catalog.json, search.sqlite (All's, cut down to the records kept), summary.json, pack.json and
pack-manifest.tsv. Not the map: All's whole-US map set is left out, and the map comes from the states ticked (mapmerge).

Trust: the files are checked against All's pack-manifest.tsv, which is read from the same zip and is not itself pinned by the
release manifest (whose SHA-256 is of the whole zip). The bytes come over HTTPS from the allowed host with the pinned roots,
as every download does.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
from collections import Counter
from pathlib import Path, PurePosixPath

from . import download, install

TOP = 'LastbookData/'
BIG = 64 * 2 ** 20          # a file this size or larger downloads on its own
RUN_MAX = 128 * 2 ** 20     # smaller files come together, in runs of at most this many bytes
GAP_MAX = 4 * 2 ** 20       # unwanted bytes a run may carry between two wanted files, rather than make another request
WORKERS = 4                 # runs and big files at once
BIG_CONNECTIONS = 4         # connections for one big file (download.CONNECTIONS is 8: four big files at once would be 32)
TAIL = 2 ** 20              # the end of the zip, read first: the end of central directory record is in it
HEADER_PROBE = 512          # bytes of a local header's extra field read with it; a longer one is read again
FIELDS = ('path', 'video_path', 'vtt_path', 'text_path', 'thumbnail_path')
DOC_KINDS = ('pdf', 'html', 'pptx')
ALWAYS_CATEGORIES = {'about'}       # the sources page
SEARCH = 'library-data/search.sqlite'
REWRITTEN = {'library-data/catalog.json', SEARCH, 'library-data/summary.json', 'pack.json', 'pack-manifest.tsv',
             'pack-contents.tsv', 'map-set.json'}
MAX_IDS = 50000


class Refused(install.Refused):
    pass


# ----------------------------------------------------------------------------------------------------- the zip's index --
def _safe(rel):
    p = PurePosixPath(rel)
    return (bool(rel) and not rel.startswith('/') and '\\' not in rel and '\x00' not in rel and '..' not in p.parts
            and ':' not in p.parts[0])


def parse_directory(tail, tail_at, size, read):
    """[(name, local header offset, size)] of a stored zip, from its tail (bytes read at tail_at) and read(offset, n) for the
    central directory when it lies before the tail. ZIP64 as written by Python's zipfile."""
    i = tail.rfind(b'PK\x05\x06')
    if i < 0:
        raise Refused('custom', 'The All library’s list of files could not be found.')
    count, cd_size, cd_off = struct.unpack('<H', tail[i + 10:i + 12])[0], *struct.unpack('<II', tail[i + 12:i + 20])
    j = tail.rfind(b'PK\x06\x06', 0, i)
    if j >= 0:
        count, cd_size, cd_off = struct.unpack('<QQQ', tail[j + 32:j + 56])
    if cd_off + cd_size > size:
        raise Refused('custom', 'The All library’s list of files does not fit the file.')
    cd = tail[cd_off - tail_at:cd_off - tail_at + cd_size] if cd_off >= tail_at else read(cd_off, cd_size)
    out, p = [], 0
    while p < len(cd) and len(out) < count:
        if cd[p:p + 4] != b'PK\x01\x02':
            raise Refused('custom', 'The All library’s list of files is damaged.')
        method, = struct.unpack('<H', cd[p + 10:p + 12])
        csize, usize = struct.unpack('<II', cd[p + 20:p + 28])
        nl, el, cl = struct.unpack('<HHH', cd[p + 28:p + 34])
        off, = struct.unpack('<I', cd[p + 42:p + 46])
        name = cd[p + 46:p + 46 + nl].decode('utf-8')
        ex, q = cd[p + 46 + nl:p + 46 + nl + el], 0
        while q + 4 <= len(ex):
            hid, hl = struct.unpack('<HH', ex[q:q + 4])
            if hid == 1:
                vals, k = ex[q + 4:q + 4 + hl], 0
                if usize == 0xFFFFFFFF:
                    usize, = struct.unpack('<Q', vals[k:k + 8]); k += 8
                if csize == 0xFFFFFFFF:
                    csize, = struct.unpack('<Q', vals[k:k + 8]); k += 8
                if off == 0xFFFFFFFF:
                    off, = struct.unpack('<Q', vals[k:k + 8]); k += 8
            q += 4 + hl
        if not name.endswith('/'):
            if method != 0 or csize != usize:
                raise Refused('custom', f'The All library holds a compressed file ({name[:80]}); Custom reads stored files only.')
            out.append((name, off, csize))
        p += 46 + nl + el + cl
    if len(out) == 0:
        raise Refused('custom', 'The All library’s list of files is empty.')
    return out, cd_off


class Index:
    """What Custom chooses from: the All zip's files {rel: (local header offset, size)}, its pack-manifest rows {rel: (bytes,
    sha256)} and catalog, kept in `folder` (the app's own settings folder) for the zip they were read from (meta.json)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.meta = json.loads((self.folder / 'meta.json').read_text(encoding='utf-8'))
        d = json.loads((self.folder / 'index.json').read_text(encoding='utf-8'))
        self.entries = {rel: (off, size) for rel, off, size in d['entries']}
        self.ends = d['ends']                 # rel -> where the next entry starts (or the central directory): a run's last byte
        self.rows = {rel: (size, sha) for rel, size, sha in install.read_pack_manifest(self.folder / 'pack-manifest.tsv')}
        self.catalog = json.loads((self.folder / 'catalog.json').read_text(encoding='utf-8'))
        self.by_id = {r['id']: r for r in self.catalog if isinstance(r, dict) and isinstance(r.get('id'), str)}
        self.owner = {}
        for r in self.by_id.values():
            for f in record_files(r):
                self.owner.setdefault(f, set()).add(r['id'])

    @property
    def etag(self):
        return self.meta['etag']

    def matches(self, all_pack):
        f = (all_pack or {}).get('files') or [{}]
        return self.meta.get('url') == f[0].get('url') and self.meta.get('zip_sha256') == f[0].get('sha256') and self.meta.get('size') == f[0].get('size')


def load_index(folder, all_pack):
    """The saved index when it is whole and was read from the All zip the manifest offers now, else None."""
    try:
        idx = Index(folder)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return idx if idx.matches(all_pack) else None


def read_index(all_pack, folder, control, progress=lambda done, total: None, allowed=None, context=None):
    """Reads the All zip's list of files, pack-manifest.tsv and catalog.json by byte range into `folder`, and returns the Index.
    Everything is written beside, then meta.json last: a stop leaves no index that looks whole."""
    f = all_pack['files'][0]
    url, size = f['url'], int(f['size'])
    folder = Path(folder)
    work = folder.with_name(folder.name + '.new')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    got = [0]
    total = [TAIL + 32 * 2 ** 20]

    def fetch(name, span, n, sha=None, etag=None):
        base = got[0]
        d = download.Download(url, work / name, n, sha, control, lambda done, t, phase: progress(base + done, total[0]),
                              allowed=allowed, context=context, span=span, url_size=size, expect_etag=etag, connections=1)
        d.run()
        got[0] += n
        return d

    n = min(TAIL, size)
    d = fetch('tail.bin', size - n, n)
    etag = d.etag
    if not etag:
        raise Refused('custom', 'The download server did not say which copy of the All library it has, so Lastbook cannot read parts of it safely.')
    tail = (work / 'tail.bin').read_bytes()

    def read(off, length):
        total[0] += length
        fetch('directory.bin', off, length, etag=etag)
        return (work / 'directory.bin').read_bytes()
    listed, cd_off = parse_directory(tail, size - n, size, read)
    entries, seen = {}, set()
    for name, off, length in listed:
        if not name.startswith(TOP) or not _safe(name[len(TOP):]):
            raise Refused('custom', f'The All library holds a file Lastbook does not accept ({name[:80]!r}).')
        rel = name[len(TOP):]
        if rel.lower() in seen:
            raise Refused('custom', f'The All library holds the same file twice ({rel[:80]!r}).')
        seen.add(rel.lower())
        entries[rel] = (off, length)
    order = sorted(entries.items(), key=lambda kv: kv[1][0])
    ends = {}
    for (rel, (off, length)), nxt in zip(order, order[1:] + [(None, (cd_off, 0))]):
        ends[rel] = nxt[1][0]
        if off + 30 + len(rel) + len(TOP) + length > ends[rel]:
            raise Refused('custom', f'The All library holds entries that overlap ({rel[:80]!r}).')
    for rel, want in (('pack-manifest.tsv', None), ('library-data/catalog.json', 'row')):
        if rel not in entries:
            raise Refused('custom', f'The All library has no {rel}.')
        off, length = entries[rel]
        total[0] += length
        sha = None
        if want == 'row':
            rows = {r: (b, s) for r, b, s in install.read_pack_manifest(work / 'pack-manifest.tsv')}
            if rows.get(rel, (None,))[0] != length:
                raise Refused('custom', f'The All library’s {rel} is not the size its list says.')
            sha = rows[rel][1]
        start = member_start(url, size, etag, off, rel, ends[rel], work / 'header.bin', control, allowed, context)
        fetch(Path(rel).name, start, length, sha, etag)
    rows = install.read_pack_manifest(work / 'pack-manifest.tsv')
    for rel, length, _ in rows:
        if rel not in entries or entries[rel][1] != length:
            raise Refused('custom', f'The All library’s list and its files disagree about {rel[:80]!r}.')
    for name in ('tail.bin', 'directory.bin', 'header.bin'):
        try:
            os.unlink(work / name)
        except FileNotFoundError:
            pass
    install._write_json(work / 'index.json', {'entries': [[rel, off, length] for rel, (off, length) in sorted(entries.items())], 'ends': ends})
    install._write_json(work / 'meta.json', {'url': url, 'size': size, 'zip_sha256': f['sha256'], 'etag': etag, 'read': time.strftime('%Y-%m-%dT%H:%M:%S')})
    shutil.rmtree(folder, ignore_errors=True)
    os.replace(work, folder)
    return Index(folder)


def member_start(url, size, etag, off, rel, end, scratch, control, allowed=None, context=None):
    """Where the bytes of the zip entry whose local header is at `off` begin: its header is read to learn its length."""
    want = (TOP + rel).encode('utf-8')
    n = min(30 + len(want) + HEADER_PROBE, end - off)
    for _ in range(2):
        download.Download(url, scratch, n, None, control, allowed=allowed, context=context, span=off, url_size=size,
                          expect_etag=etag, connections=1).run()
        h = Path(scratch).read_bytes()
        os.unlink(scratch)
        if h[:4] != b'PK\x03\x04':
            raise Refused('custom', f'The All library’s entry for {rel[:80]!r} is not where its list says.')
        nl, el = struct.unpack('<HH', h[26:30])
        if h[30:30 + nl] != want:
            raise Refused('custom', f'The All library’s entry for {rel[:80]!r} has another name.')
        if 30 + nl + el <= len(h) or n >= end - off:
            return off + 30 + nl + el
        n = min(30 + nl + el, end - off)
    raise Refused('custom', f'The All library’s entry for {rel[:80]!r} could not be read.')


# --------------------------------------------------------------------------------------------------------- choosing --
def record_files(r):
    """The library files a catalog record names, relative to LastbookData."""
    out = []
    for f in FIELDS:
        v = r.get(f)
        if isinstance(v, str) and v and _safe(v):
            out.append('library-data/' + v)
    return out


def _title(slug):
    words = str(slug or 'other').replace('_', '-').split('-')
    if words and words[0] == 'creator':
        words = words[1:]
    return ' '.join(words).strip().capitalize() or 'Other'


def guide_ids(topics_dir):
    """Catalog ids the shipped guides point to (content/topics/*.json: every "doc")."""
    ids = set()
    for p in sorted(Path(topics_dir).glob('*.json')):
        try:
            ids |= set(re.findall(r'"doc"\s*:\s*"([^"]+)"', p.read_text(encoding='utf-8')))
        except OSError:
            pass
    return ids


def _dependents(idx):
    """video id -> the transcript and metadata records that come with it."""
    by_path = {r.get('path'): r['id'] for r in idx.by_id.values() if r.get('kind') == 'video'}
    out = {}
    for r in idx.by_id.values():
        if r.get('kind') == 'metadata' and r.get('video_id') in idx.by_id:
            out.setdefault(r['video_id'], []).append(r['id'])
        elif r.get('kind') == 'transcript' and r.get('video_path') in by_path:
            out.setdefault(by_path[r['video_path']], []).append(r['id'])
    return out


def _choosable(r):
    k = r.get('kind')
    return k in DOC_KINDS + ('video', 'zim') and r.get('category') not in ALWAYS_CATEGORIES


def _group_of(r):
    k = r.get('kind')
    if k == 'zim':
        return 'zim:' + r['id']
    return ('video:' if k == 'video' else 'doc:') + str(r.get('category') or 'other')


def record_bytes(idx, rid, deps):
    files = set(record_files(idx.by_id[rid]))
    for d in deps.get(rid, ()):
        files |= set(record_files(idx.by_id[d]))
    return sum(idx.rows.get(f, (0,))[0] for f in files)


def groups(idx, guides):
    """[{id, kind, title, count, bytes, default}] for the screen: the guides' sources, each encyclopedia, videos by category,
    documents by category. A record's bytes count the files it brings (a video's transcript, captions and thumbnail)."""
    deps = _dependents(idx)
    out = {}
    for r in idx.by_id.values():
        if not _choosable(r):
            continue
        gid = _group_of(r)
        g = out.setdefault(gid, {'id': gid, 'kind': gid.split(':')[0], 'count': 0, 'bytes': 0,
                                 'title': r.get('title') if r.get('kind') == 'zim' else _title(r.get('category'))})
        g['count'] += 1
        g['bytes'] += record_bytes(idx, r['id'], deps)
    gs = [g for g in idx.by_id if g in guides and _choosable(idx.by_id[g])]
    guide = {'id': 'guides', 'kind': 'guides', 'title': 'What the guides point to', 'count': len(gs),
             'bytes': sum(record_bytes(idx, g, deps) for g in gs)}
    order = {'guides': 0, 'zim': 1, 'video': 2, 'doc': 3}
    rest = sorted(out.values(), key=lambda g: (order[g['kind']], -g['bytes'] if g['kind'] == 'zim' else 0, g['title'].lower()))
    for g in [guide] + rest:
        g['default'] = g['id'] == 'guides' or g['id'].startswith('doc:') or (g['kind'] == 'zim' and 'wikipedia' not in g['title'].lower())
    return [guide] + rest


def items(idx, group_id, guides):
    """The records of one group: [{id, title, bytes, kind, duration}]."""
    deps = _dependents(idx)
    rs = [r for r in idx.by_id.values() if _choosable(r) and (r['id'] in guides if group_id == 'guides' else _group_of(r) == group_id)]
    rs.sort(key=lambda r: str(r.get('title') or '').lower())
    return [{'id': r['id'], 'title': r.get('title') or r['id'], 'kind': r.get('kind'), 'bytes': record_bytes(idx, r['id'], deps),
             'duration': r.get('duration')} for r in rs]


def clean_selection(sel):
    """A selection as the screen sends it, or None: {groups: [ids], add: [record ids], remove: [record ids]}."""
    if not isinstance(sel, dict):
        return None
    out = {}
    for key in ('groups', 'add', 'remove'):
        v = sel.get(key) or []
        if not isinstance(v, list) or len(v) > MAX_IDS or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in v):
            return None
        out[key] = sorted(set(v))
    return out


def resolve(idx, sel, guides):
    """The ids of every record the library keeps: the groups ticked, plus single records added, less those taken out; then
    the sources page and each kept video's transcript and metadata records."""
    chosen = set()
    groups_on = set(sel['groups'])
    for r in idx.by_id.values():
        if not _choosable(r):
            continue
        if _group_of(r) in groups_on or ('guides' in groups_on and r['id'] in guides):
            chosen.add(r['id'])
    chosen |= {x for x in sel['add'] if x in idx.by_id and _choosable(idx.by_id[x])}
    chosen -= set(sel['remove'])
    deps = _dependents(idx)
    kept = set(chosen)
    for rid in chosen:
        kept |= set(deps.get(rid, ()))
    kept |= {r['id'] for r in idx.by_id.values() if r.get('category') in ALWAYS_CATEGORIES}
    return kept


def files_for(idx, kept):
    """{rel: (bytes, sha256)} of the zip's files the library needs: the kept records' files, the files no record names, and
    All's search index (cut down later). Not the map set (it comes from the state maps) and not what is written anew."""
    out = {}
    for rid in kept:
        for f in record_files(idx.by_id[rid]):
            if f in idx.rows:
                out[f] = idx.rows[f]
    for rel, row in idx.rows.items():
        if rel not in idx.owner and rel not in REWRITTEN and not install.map_member(rel) and not rel.startswith('derived/'):
            out[rel] = row
    if SEARCH in idx.rows:
        out[SEARCH] = idx.rows[SEARCH]
    return out


def selection_key(sel, etag):
    return hashlib.sha256(json.dumps({'s': sel, 'e': etag}, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------------- downloading --
class _Group:
    """One Control for several workers: the person's pause or cancel stops them all, and so does the first one's failure."""

    def __init__(self, parent):
        self.parent, self.ev = parent, threading.Event()

    def check(self):
        self.parent.check()
        if self.ev.is_set():
            raise download.Stopped('cancelled')

    def sleep(self, s):
        end = time.time() + s
        while True:
            self.check()
            left = end - time.time()
            if left <= 0:
                return
            self.ev.wait(min(0.5, left))


def plan_tasks(idx, rels):
    """[('big', rel)] or [('run', [rels], start, end)]: neighbouring small files together, in zip order."""
    big, small = [], []
    for rel in rels:
        (big if idx.entries[rel][1] >= BIG else small).append(rel)
    small.sort(key=lambda r: idx.entries[r][0])
    tasks = [('big', r) for r in sorted(big, key=lambda r: idx.entries[r][0])]
    run, start = [], None
    for rel in small:
        off = idx.entries[rel][0]
        end = idx.ends[rel]
        if run and (off - idx.ends[run[-1]] > GAP_MAX or end - start > RUN_MAX):
            tasks.append(('run', run, start, idx.ends[run[-1]]))
            run = []
        if not run:
            start = off
        run.append(rel)
    if run:
        tasks.append(('run', run, start, idx.ends[run[-1]]))
    return tasks


def fetch(idx, rels, lib, scratch, control, progress, mark, allowed=None, context=None, waiting=lambda s: None):
    """Downloads these files of the zip into `lib`, each checked against its SHA-256; mark(rel) as each is in place.
    progress(done, total)."""
    lib, scratch = Path(lib), Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    url, size, etag = idx.meta['url'], idx.meta['size'], idx.etag
    tasks = plan_tasks(idx, rels)
    total = sum(idx.rows[r][0] for r in rels) or 1
    group = _Group(control)
    lock = threading.Lock()
    finished, inflight, errors = [0], {}, []

    def report():
        with lock:
            n = finished[0] + sum(inflight.values())
        progress(min(n, total), total)

    def run_big(i, rel):
        off, length = idx.entries[rel]
        start = member_start(url, size, etag, off, rel, idx.ends[rel], scratch / f'h{i}.bin', group, allowed, context)
        dest = lib / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        def prog(done, t, phase):
            with lock:
                inflight[i] = done if phase == 'downloading' else inflight.get(i, 0)
            report()
        download.Download(url, dest, length, idx.rows[rel][1], group, prog, allowed=allowed, context=context, waiting=waiting,
                          connections=BIG_CONNECTIONS, span=start, url_size=size, expect_etag=etag).run()
        with lock:
            inflight.pop(i, None)
            finished[0] += length
        mark(rel)
        report()

    def run_small(i, run, start, end):
        blob = scratch / f'run-{start}.bin'

        def prog(done, t, phase):
            with lock:
                inflight[i] = int(done * share) if phase == 'downloading' else inflight.get(i, 0)
            report()
        share = sum(idx.rows[r][0] for r in run) / max(end - start, 1)
        download.Download(url, blob, end - start, None, group, prog, allowed=allowed, context=context, waiting=waiting,
                          connections=1, span=start, url_size=size, expect_etag=etag).run()
        try:
            with open(blob, 'rb') as f:
                for rel in run:
                    group.check()
                    split_member(f, idx.entries[rel][0] - start, rel, idx.rows[rel], lib)
                    with lock:
                        finished[0] += idx.rows[rel][0]
                    mark(rel)
        finally:
            with lock:
                inflight.pop(i, None)
            os.unlink(blob)
        report()

    queue = list(enumerate(tasks))

    def worker():
        while True:
            with lock:
                if not queue or errors:
                    return
                i, t = queue.pop(0)
            for attempt in range(2):
                try:
                    if t[0] == 'big':
                        run_big(i, t[1])
                    else:
                        run_small(i, *t[1:])
                    break
                except download.BadChecksum:
                    with lock:
                        inflight.pop(i, None)
                    if attempt:
                        with lock:
                            errors.append(download.Failed('checksum', 'A file of the All library did not match its checksum twice. The file on the server may be damaged; try again later.'))
                        group.ev.set()
                        return
                except BaseException as e:      # noqa: B036 -- the coordinator below raises it
                    with lock:
                        errors.append(e)
                    group.ev.set()
                    return

    threads = [threading.Thread(target=worker, daemon=True, name=f'custom-{n}') for n in range(min(WORKERS, len(tasks)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    control.check()          # the person's pause or cancel first
    for e in errors:
        if not (isinstance(e, download.Stopped) and group.ev.is_set() and e.why == 'cancelled'):
            raise e
    if errors:
        raise errors[0]
    progress(total, total)


def split_member(f, at, rel, row, lib):
    """Writes the zip entry whose local header is at `at` in the open file f to lib/rel, checked against row (bytes, sha256)."""
    f.seek(at)
    h = f.read(30)
    if h[:4] != b'PK\x03\x04':
        raise download.BadChecksum(f'The All library’s entry for {rel[:80]} is not where its list says.')
    nl, el = struct.unpack('<HH', h[26:30])
    if f.read(nl) != (TOP + rel).encode('utf-8'):
        raise download.BadChecksum(f'The All library’s entry for {rel[:80]} has another name.')
    f.seek(el, 1)
    length, sha = row
    dest = Path(lib) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.partial')
    hs, left = hashlib.sha256(), length
    with open(part, 'wb') as out:
        while left:
            b = f.read(min(4 * 2 ** 20, left))
            if not b:
                raise download.BadChecksum(f'The All library’s entry for {rel[:80]} is cut short.')
            hs.update(b)
            out.write(b)
            left -= len(b)
    if hs.hexdigest() != sha:
        os.unlink(part)
        raise download.BadChecksum(f'{rel[:80]} did not match its published checksum.')
    os.replace(part, dest)


def reusable(current, files):
    """Files of the open library that are the same as the ones wanted: same path, size and SHA-256 by its own
    pack-manifest.tsv. They can be linked into the new library rather than downloaded again."""
    if not current:
        return set()
    try:
        rows = {rel: (size, sha) for rel, size, sha in install.read_pack_manifest(Path(current) / 'pack-manifest.tsv')}
    except (OSError, ValueError):
        return set()
    out = set()
    for rel, row in files.items():
        p = Path(current) / rel
        if rel != SEARCH and rows.get(rel) == row:
            try:
                if p.is_file() and not p.is_symlink() and p.stat().st_size == row[0]:
                    out.add(rel)
            except OSError:
                pass
    return out


def link_or_copy(src, dest):
    """A hard link (no second copy on the disk), or a copy where links are not possible."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.partial')
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


# ----------------------------------------------------------------------------------------------------- the library --
def finish_library(idx, lib, kept, sel, files):
    """Writes what is the Custom library's own: catalog.json and summary.json for the records kept, All's search index cut
    down to them, pack.json, and pack-manifest.tsv listing every file setup put there."""
    lib = Path(lib)
    recs = [r for r in idx.catalog if isinstance(r, dict) and r.get('id') in kept]
    (lib / 'library-data').mkdir(parents=True, exist_ok=True)
    install._write_json(lib / 'library-data' / 'summary.json', {
        'resources': len(recs), 'pdfs': sum(1 for r in recs if r.get('kind') == 'pdf'),
        'pdf_pages': sum(int(r.get('pages') or 0) for r in recs if r.get('kind') == 'pdf' and str(r.get('pages') or '').isdigit()),
        'web_pages': sum(1 for r in recs if r.get('kind') == 'html'), 'videos': sum(1 for r in recs if r.get('kind') == 'video'),
        'collections': sum(1 for r in recs if r.get('kind') == 'zim'), 'document_bytes': sum(int(r.get('bytes') or 0) for r in recs)})
    search = lib / SEARCH
    if search.is_file():
        db = sqlite3.connect(search, isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('BEGIN')
            db.execute('DELETE FROM pages WHERE doc_id NOT IN (SELECT value FROM json_each(?))', (json.dumps(sorted(kept)),))
            db.execute('COMMIT')
            db.execute("INSERT INTO pages(pages) VALUES('optimize')")
            db.execute('VACUUM')
        finally:
            db.close()
    counts = Counter(r.get('kind') for r in recs)
    install._write_json(lib / 'pack.json', {'name': 'custom', 'size': 'custom', 'map': 'base', 'built': time.strftime('%Y-%m-%d'),
                                            'from': {'pack': 'all', 'zip_sha256': idx.meta.get('zip_sha256'), 'etag': idx.etag},
                                            'counts': dict(sorted(counts.items())), 'selection': sel, 'add_ons': [], 'incomplete': 0})
    tmp = lib / 'library-data' / 'catalog.json.partial'
    tmp.write_text(json.dumps(recs, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, lib / 'library-data' / 'catalog.json')
    rows = []
    for rel in sorted(set(files) | {'library-data/catalog.json', 'library-data/summary.json', 'pack.json'}):
        p = lib / rel
        if not p.is_file():
            continue
        if rel in files and rel != SEARCH:
            size, sha = files[rel]
        else:
            h = hashlib.sha256()
            with open(p, 'rb') as f:
                for b in iter(lambda: f.read(8 * 2 ** 20), b''):
                    h.update(b)
            size, sha = p.stat().st_size, h.hexdigest()
        rows.append(f'{rel}\t{size}\t{sha}\t{"custom"}\t')
    tmp = lib / 'pack-manifest.tsv.partial'
    tmp.write_text('path\tbytes\tsha256\thow\tchanges\n' + '\n'.join(rows) + '\n', encoding='utf-8')
    os.replace(tmp, lib / 'pack-manifest.tsv')
