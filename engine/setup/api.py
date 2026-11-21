"""/api/setup/*: the engine side of first-run setup and of Library → Library and local AI. Contract: docs/SETUP-API.md.

Two ways in, one module:
  - setup mode (no library yet): app/main.py makes a Bridge around SetupServer, whose Handler serves web/setup.html at /, the web
    folder at /ui/, and /api/setup/* here. Nothing else.
  - normal mode: server.py hands /api/setup/ to handle(). The app's main.py configures it first (configure(...)); a bare
    server.py gets a manage-mode Setup on first use, with no relaunch (finish then answers restart: true).

Only requests from this computer get an answer (the bridge's, or loopback): a phone the guide is shared with gets 403.
The exception is the web service (server.py with LASTBOOK_REMOTE_SETUP=1, docs/WEB-SERVICE.md): there every request comes through
a reverse proxy, so server.py marks a request that holds the access key (h.remote_setup_ok) after its Host and Origin checks.
Such a request may not name folders or files on the server, and is never offered the local AI.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import custom, download, hardware, install, mapmerge
from .manifest import Manifest

DATA_NAME = install.DATA_NAME
WORK_SUFFIX = '.setup'
SAVE_EVERY = 2.0
RECOMMEND_MARGIN = 5 * 10 ** 9     # beyond what the install needs, so the disk is not left full
RECOMMEND_MARGIN_MAX = 20 * 10 ** 9
MAX_BODY = 1024 * 1024      # a Custom library's choice can list thousands of single records
ACTIVE = ('waiting', 'downloading', 'verifying', 'unpacking', 'activating')


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _gb(n):
    return f'{n / 1e9:.0f} GB' if n >= 10e9 else f'{n / 1e9:.1f} GB'


def _sha256(path, control, progress):
    h, done = hashlib.sha256(), 0
    total = os.path.getsize(path)
    with open(path, 'rb') as f:
        while True:
            control.check()
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
            progress(done, total)
    return h.hexdigest()


def _same_disk(a, b):
    try:
        pa, pb = Path(a), Path(b)
        while not pa.exists():
            pa = pa.parent
        while not pb.exists():
            pb = pb.parent
        return os.stat(pa).st_dev == os.stat(pb).st_dev
    except OSError:
        return False


class _Item:
    """Progress of one item (the pack, or the model), with rate and time left."""

    def __init__(self, d):
        self.d = d
        self.t, self.at, self.rate = time.time(), d.get('bytes_done', 0), 0.0

    def progress(self, done, total, state=None):
        d = self.d
        d.pop('retry_at', None)
        if state and d['state'] != state:
            d['state'] = state
            self.t, self.at, self.rate = time.time(), done, 0.0
        d['bytes_done'], d['bytes_total'] = done, total
        now = time.time()
        if now - self.t >= 1.0:
            inst = max(done - self.at, 0) / (now - self.t)
            self.rate = inst if not self.rate else 0.6 * self.rate + 0.4 * inst
            self.t, self.at = now, done
            d['rate_bps'] = int(self.rate)
            d['eta_s'] = round((total - done) / self.rate) if self.rate > 0 else None

    def waiting(self, seconds):
        """The server is busy: nothing arrives for `seconds`, so there is no rate, and the time of the next try is kept."""
        d = self.d
        d['retry_at'], d['rate_bps'], d['eta_s'] = time.time() + seconds, 0, None
        self.t, self.at, self.rate = time.time(), d.get('bytes_done', 0), 0.0


class Setup:
    def __init__(self, mode, settings_dir, default_parent, current=None, manifest=None, allowed=None, context=None,
                 on_finish=None, pick_folder=None, look_for_libraries=None, home=None, llm_local=None, default_why=None):
        """mode: 'first-run' or 'manage'. current: the open library (manage). on_finish(folder): the app opens that library
        (it relaunches). pick_folder(): a folder the person chose, or None. look_for_libraries(): LastbookData folders found."""
        self.mode, self.current = mode, Path(current) if current else None
        self.settings_dir, self.default_parent = Path(settings_dir), Path(default_parent)
        self.manifest = manifest or Manifest(allowed=allowed, context=context)
        self.allowed, self.context = allowed, context
        self.on_finish, self.pick_folder, self.look_for_libraries = on_finish, pick_folder, look_for_libraries
        self.default_why = default_why     # why the default folder is not the usual one (a long Windows user folder), for the screen
        self._llm = llm_local
        self.lock = threading.RLock()
        self.job, self.control, self.thread = None, None, None
        self.sources = {}          # file name -> a file already on this computer the person chose with `use`
        self.ai_measure, self.measuring = None, False
        self.deleting = set()
        self.rejected = {}         # path -> (size, mtime) of a file already here that failed its SHA-256: not offered or used again
        self.custom = {'state': 'idle'}   # reading the All library's list for a Custom library (engine/setup/custom.py)
        self._index, self._guides = None, None
        self.job = self._load_job()

    # ------------------------------------------------------------------------------------------------- saved state --
    def _settings(self):
        try:
            return json.loads((self.settings_dir / 'setup.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save_settings(self, d):
        try:
            self.settings_dir.mkdir(parents=True, exist_ok=True)
            install._write_json(self.settings_dir / 'setup.json', d)
        except OSError as e:
            print('setup: could not save setup.json:', e, flush=True)

    def _load_job(self):
        """The saved job, from the app's own settings folder only, with its plan rebuilt from the manifest and the work folder
        recomputed. Anything that does not check out is no job: it never stops the app from starting."""
        try:
            job = self._checked_job(json.loads(_job_file(self.settings_dir).read_text(encoding='utf-8')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            if not isinstance(e, FileNotFoundError):
                print('setup: the saved job was not used:', type(e).__name__, flush=True)
            return None
        if not job:
            return None
        if job.get('state') in ACTIVE:      # this program stopped while it ran: carry on only when the person says so
            job['state'] = 'paused'
            for it in job['items']:
                if it['state'] in ACTIVE:
                    was = it['state']
                    it['state'] = 'paused'
                    if was != 'waiting':
                        it['paused_from'] = was       # what it was doing when this program stopped; dropped on resume
                    it['bytes_done'], it['bytes_total'] = self._done_after_restart(job['plan'], it, was)
                it['rate_bps'], it['eta_s'] = 0, None
                it.pop('retry_at', None)
        return job

    def _checked_job(self, job):
        """A job as this program would have written it, or None. Nothing from disk is used as a path or a file name unless it is
        what the plan's ids and the parent folder give: pack, map and model come from the manifest; work and target are worked
        out again; no `verified` list is kept (files are hashed again); a source must be a file of exactly the expected name."""
        plan, items = job['plan'], job['items']
        if not isinstance(plan, dict) or not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            return None
        if job.get('state') not in ACTIVE + ('paused', 'done', 'failed'):
            return None
        if plan.get('use_library'):
            lib = Path(plan['use_library'])
            if not install.is_library(lib):
                return None
            job['plan'] = {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}
            return job
        parent, target = Path(plan['parent']), Path(plan['target'])
        if target.parent != parent or (target.name != DATA_NAME and target != self.current):
            return None          # a library setup installs is called LastbookData; only the open one may have another name
        work = _work_for(target)
        ids = lambda key: (plan.get(key) or {}).get('id')
        pack = self.manifest.pack(ids('pack')) if plan.get('pack') else None
        model = self.manifest.model(ids('model')) if plan.get('model') else None
        if (plan.get('pack') and not pack) or (plan.get('model') and not model):
            return None
        maps = _entries(plan, 'maps', 'map', self.manifest.map)
        addresses = _entries(plan, 'addresses', None, self.manifest.address_index)
        if maps is None or addresses is None:
            return None
        mapset = _checked_mapset(plan.get('mapset'), maps)
        if plan.get('mapset') and not mapset:
            return None
        cust = None
        if plan.get('custom') is not None:
            cust = self._checked_custom(plan['custom'])
            if not cust:
                return None
        wanted = ({('pack', pack['id']) if pack else None, ('custom', 'custom') if cust else None, ('model', model['id']) if model else None,
                   ('mapset', 'combine') if mapset else None}
                  | {('map', m['id']) for m in maps} | {('addresses', a['id']) for a in addresses}) - {None}
        if {(i.get('kind'), i.get('id')) for i in items} != wanted or len(items) != len(wanted):
            return None
        names = (([f['name'] for f in pack['files']] if pack else []) + [f['name'] for m in maps for f in m['files']]
                 + [f['name'] for a in addresses for f in a['files']] + ([model['file']] if model else []))
        sources = {n: str(v) for n, v in (plan.get('sources') or {}).items()
                   if n in names and isinstance(v, str) and Path(v).name == n and Path(v).is_file()}
        switch = bool(plan.get('switch')) and (pack is not None or cust is not None)
        drop = [x for x in (plan.get('drop_addresses') or []) if isinstance(x, str) and re.fullmatch(r'[A-Z]{2}', x)]
        clean = {'pack': pack, 'custom': cust, 'model': model, 'maps': maps, 'mapset': mapset, 'addresses': addresses, 'drop_addresses': drop,
                 'parent': str(parent), 'target': str(target), 'work': str(work),
                 'sources': sources, 'explicit': bool(plan.get('explicit')), 'retried': {}, 'switch': switch, 'repair': bool(plan.get('repair'))}
        if isinstance(plan.get('damaged'), list):
            clean['damaged'] = [str(x) for x in plan['damaged']]
        if plan.get('map_new'):
            clean['map_new'] = str(work / 'unpack-map' / DATA_NAME)
        if mapset and plan.get('mapset_ready'):
            clean['mapset_ready'] = True
            clean['mapset_merged'] = [x for x in (plan.get('mapset_merged') or []) if x in {m['id'] for m in maps}]
        if plan.get('model_file') and model:
            clean['model_file'] = str((target if switch else Path(self.current or target)) / 'models' / model['file'])
            if plan.get('model_sha256'):
                clean['model_sha256'] = model['sha256']
        job['plan'] = clean
        return job

    def _done_after_restart(self, plan, it, was):
        """(bytes done, bytes total) for an item found mid-run at startup, from what is on disk for the step it was in."""
        entry = _entry_for(plan, it)
        if was == 'unpacking' and it['kind'] == 'map' and entry:
            total = entry['installed_bytes']
            return min(_tree_size(_map_unpack_dir(plan, entry)), total), total
        if was == 'unpacking' and it['kind'] == 'addresses' and entry:
            total = entry['installed_bytes']
            return min(_tree_size(Path(plan['work']) / 'unpack-addresses'), total), total
        if it['kind'] in ('mapset', 'custom'):
            return it.get('bytes_done', 0), it.get('bytes_total', 0)
        if was == 'unpacking' and plan.get('pack'):
            total = plan['pack']['installed_bytes']
            return min(_tree_size(Path(plan['work']) / 'unpack'), total), total
        if it.get('source') == 'found':
            return it['bytes_total'], it['bytes_total']     # nothing to download: the file is already on this computer
        return self._on_disk(plan, it), it['bytes_total']

    @staticmethod
    def _on_disk(plan, it):
        """Bytes of this item already downloaded, from the files themselves."""
        entry = _entry_for(plan, it)
        if it['kind'] == 'pack' and plan.get('pack'):
            plan_files = plan['pack']['files']
        elif it['kind'] in ('map', 'addresses'):
            plan_files = entry['files'] if entry else []
        elif it['kind'] == 'model':
            plan_files = [{'name': plan['model']['file']}] if plan.get('model') else []
        else:
            plan_files = []
        n = 0
        for f in plan_files:
            for p in (Path(plan['work']) / 'downloads' / f['name'], Path(plan['work']) / 'downloads' / (f['name'] + '.part')):
                try:
                    n += p.stat().st_size
                except OSError:
                    pass
        return n

    def _save_job(self):
        """To the app's own settings folder. A save that fails stops the job (raises OSError)."""
        job = self.job
        if not job:
            return
        f = _job_file(self.settings_dir)
        f.parent.mkdir(parents=True, exist_ok=True)
        install._write_json(f, job)

    # --------------------------------------------------------------------------------------------------- the state --
    def llm(self):
        if self._llm is None:
            try:
                import llm_local
                self._llm = llm_local
            except Exception as e:
                print('setup: no llm_local:', type(e).__name__, e, flush=True)
                self._llm = False
        return self._llm or None

    def current_info(self):
        if not self.current:
            return None
        try:
            metadata = json.loads((self.current / 'pack.json').read_text(encoding='utf-8'))
            pack = metadata.get('name')
            revision = metadata.get('content_revision', '')
            if not isinstance(revision, str):
                revision = ''
        except (OSError, ValueError, AttributeError):
            pack, revision = None, ''
        m = self.manifest.model()
        model = None
        if m:
            p = self.current / 'models' / m['file']
            if p.is_file() and p.stat().st_size == m['size']:
                model = m['id']
        return {'folder': str(self.current), 'pack': pack, 'content_revision': revision, 'map_bytes': install.map_bytes(self.current), 'model': model, 'map': install.map_name(self.current), 'maps': install.map_states(self.current), 'address_indexes': self.address_indexes(), 'old': self._settings().get('old', [])}

    def address_indexes(self):
        if not self.current:
            return []
        out = []
        for entry in self.manifest.current.get('address_indexes', []):
            path = self.current / 'derived' / 'geo' / 'addresses' / (entry['id'] + '.sqlite')
            try:
                receipt = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
                expected = {file['name']: file['sha256'] for file in entry['files']}
                if path.is_file() and path.stat().st_size == entry['installed_bytes'] and receipt.get('files') == expected:
                    out.append(entry['id'])
            except (OSError, ValueError, AttributeError):
                pass
        return out

    def found(self):
        """Files in app-owned storage, the selected library, or explicitly chosen by the person. A file is offered when its size matches; its
        SHA-256 is checked when it is used."""
        out, seen = [], set()
        if self.look_for_libraries:
            try:
                for lib in self.look_for_libraries():
                    lib = Path(lib)
                    if str(lib) not in seen and lib != self.current and install.is_library(lib):
                        seen.add(str(lib))
                        out.append({'kind': 'library', 'path': str(lib), 'name': lib.name})
            except Exception:
                traceback.print_exc()
        files = {}
        for p in self.manifest.current['packs']:
            for f in p['files']:
                files[f['name']] = ('pack', p['id'], f['size'])
        m = self.manifest.model()
        if m:
            files[m['file']] = ('model', m['id'], m['size'])
        for kind, key in (('map', 'maps'), ('addresses', 'address_indexes')):
            for x in self.manifest.current.get(key, []):
                for f in x['files']:
                    files[f['name']] = (kind, x['id'], f['size'])
        places = [self.default_parent]
        if self.current:
            places += [self.current / 'models', self.current]
        candidates = [Path(p) for p in self.sources.values()]
        for place in places:
            try:
                candidates.extend(c for c in place.iterdir() if c.name in files or (c.name.startswith('Lastbook-') and c.name.endswith('.zip')))
            except OSError:
                continue
        for c in candidates:
            key = str(c.resolve())
            if key in seen or not c.is_file():
                continue
            seen.add(key)
            kind, pid, size = files.get(c.name, ('pack', None, None))
            st = c.stat()
            rejected = self.rejected.get(str(c)) == (st.st_size, st.st_mtime_ns)
            out.append({'kind': kind, 'path': str(c), 'name': c.name, 'id': pid, 'size': st.st_size,
                        'size_ok': size == st.st_size and not rejected, 'rejected': rejected})
        return out

    def _model_path_here(self):
        m = self.manifest.model()
        if not m:
            return None
        for f in self.found():
            if f['kind'] == 'model' and f.get('size_ok'):
                return f['path']
        return None

    def ai_fit(self, hw):
        m = self.manifest.model()
        if not m:
            return {'fits': False, 'needs_bytes': None, 'estimate': True, 'why': 'No local AI is offered for this version.'}
        if self.ai_measure:
            return self.ai_measure
        path = self._model_path_here()
        if path and not self.measuring and self.llm():
            self.measuring = True
            threading.Thread(target=self._measure, args=(path,), daemon=True, name='setup-measure').start()
        return hardware.estimate_ai(hw['ram_bytes'], hw['gpus'], hw['unified_memory'], m['memory_bytes'])

    def _measure(self, path):
        try:
            r = hardware.measure_ai(self.llm(), path)
            if r:
                self.ai_measure = r
        except Exception:
            traceback.print_exc()

    def hardware(self):
        extra = [self.default_parent] + ([self.current.parent] if self.current else [])
        hw = hardware.detect(extra)
        hw['ai'] = self.ai_fit(hw)
        return hw

    def recommend(self, hw):
        available = [p for p in self.manifest.current['packs'] if p['available']]
        packs = [p for p in available if p['id'] == 'small']
        parent = self.current.parent if self.current else self.default_parent
        free, total = hardware.disk_of(parent)
        model = self.manifest.model()
        need = {p['id']: install.need_bytes(p, None) for p in available}
        map_need = {x['id']: _map_need(x, ()) for x in self.manifest.current.get('maps', []) if x['available']}
        if not packs:
            return {'pack': None, 'ai': False, 'folder': str(parent), 'need_bytes': need, 'why': 'No library is ready to download yet.'}
        margin = _margin(total)
        choice, folder, elsewhere = None, parent, None
        for p in packs:        # the disk that holds the home folder (or the open library) whenever anything fits there
            if free >= need[p['id']] + margin:
                choice = p
                break
        if not choice:         # only then another disk for the default Small library
            for p in packs:
                for d in hw['disks']:
                    if not _same_disk(d['path'], parent) and d['free_bytes'] >= need[p['id']] + _margin(d['total_bytes']):
                        choice, folder, elsewhere = p, Path(d['path']), d
                        break
                if choice:
                    break
        if not choice:
            small = packs[-1]
            return {'pack': None, 'ai': False, 'folder': str(parent), 'need_bytes': need,
                    'why': f'The smallest library, {small["title"]}, needs {_gb(need[small["id"]])} free, and this computer has {_gb(free)} free.'}
        if elsewhere:
            home_free = free
            free, margin = elsewhere['free_bytes'], _margin(elsewhere['total_bytes'])
        left = free - need[choice['id']]
        ai = bool(model and hw['ai']['fits'] and left >= model['size'] + margin)
        if elsewhere:
            name = elsewhere.get('name') or Path(elsewhere['path']).name or elsewhere['path']
            why = (f'This computer’s own disk has {_gb(home_free)} free, not enough for any library, so {choice["title"]} goes on {name}. '
                   'Keep that disk connected: Lastbook reads the library from it.')
        else:
            why = 'Small is selected to get started. You can choose a larger library.'
        if self.default_why and not elsewhere and not self.current:
            why = self.default_why + ' ' + why
        if model and hw['ai']['fits'] and not ai:
            why += ' The local AI fits this computer’s memory, but not on the disk as well.'
        elif model and not hw['ai']['fits']:
            why += ' ' + hw['ai']['why']
        return {'pack': choice['id'], 'ai': ai, 'folder': str(folder), 'need_bytes': need, 'map_need_bytes': map_need, 'address_need_bytes': {x['id']: _map_need(x, ()) for x in self.manifest.current.get('address_indexes', []) if x['available']}, 'why': why}

    def state(self):
        self.manifest.fetch_once()
        hw = self.hardware()
        return {'mode': self.mode, 'hardware': hw, 'manifest': self.manifest.public(), 'current': self.current_info(),
                'found': self.found(), 'recommend': self.recommend(hw), 'job': self.public_job()}

    def public_job(self):
        with self.lock:
            if not self.job:
                return None
            job = {k: v for k, v in json.loads(json.dumps(self.job)).items() if k != 'plan'}
        now = time.time()
        for it in job['items']:      # retry_at becomes seconds from now, shown only while that wait is still ahead
            at = it.pop('retry_at', None)
            if at and it['state'] == 'downloading' and at > now:
                it['retry_in_s'] = round(at - now)
        return job

    # -------------------------------------------------------------------------------------------------- the actions --
    def _parent_for(self, folder):
        if folder:
            p = Path(folder).expanduser()
            return p.parent if p.name == DATA_NAME else p
        return self.current.parent if self.current else self.default_parent

    def start(self, pack_id=None, ai=False, folder=None, sources=None, explicit=False, repair=False, map_id=None, address_id=None,
              map_ids=None, custom_selection=None):
        """map_ids (a list): the states whose detailed map the library should hold, all of them, as the checklist shows them.
        Their address indexes follow: added for states ticked, removed for states unticked. Without it, map_id and address_id
        work as before (one state, which replaces the map there)."""
        if repair:
            return self._start_repair(ai)
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            pack = self.manifest.pack(pack_id) if pack_id else None
            if pack_id and (not pack or not pack['available']):
                return {'error': 'That library is not available yet.', 'code': 'unavailable'}
            cur = self.current_info() or {}
            cust, custom_files = None, None
            if custom_selection is not None:
                if pack_id:
                    return {'error': 'Choose a pack or a Custom library, not both.', 'code': 'invalid'}
                planned = self._plan_custom(custom_selection, cur, map_ids)
                if 'error' in planned:
                    return planned
                cust, custom_files = planned['custom'], planned['fetch_bytes']
            if folder and self.current and install._real(Path(folder).expanduser()) == install._real(self.current):
                folder = None
            same_revision = pack and (not pack.get('revision') or pack['revision'] == cur.get('content_revision', ''))
            if pack and self.mode == 'manage' and cur.get('pack') == pack['id'] and same_revision and not folder:
                pack = None        # the same library again: only the AI is wanted
            model = self.manifest.model() if ai else None
            if ai and not model:
                return {'error': 'No local AI is offered for this version.', 'code': 'unavailable'}
            if map_ids is None:
                mp = self.manifest.map(map_id) if map_id else None
                if map_id and (not mp or not mp['available']):
                    return {'error': 'That state’s map is not available yet.', 'code': 'unavailable'}
                requested_address = address_id
                address_id = address_id or map_id or (pack or {}).get('map') or cur.get('map')
                if address_id in ('base', 'us'):
                    address_id = next(iter(cur.get('address_indexes', [])), None)
                addresses = self.manifest.address_index(address_id) if address_id else None
                if not addresses or not addresses['available']:
                    if requested_address:
                        return {'error': 'That state’s address search is not available yet.', 'code': 'unavailable'}
                    addresses = None
            if folder and self.current and install._real(Path(folder).expanduser()) == install._real(self.current):
                folder = None      # the window sends the shown folder, which in manage mode is the open library itself
            parent = self._parent_for(folder)
            target = self.current if self.current and not folder else parent / DATA_NAME   # the open library as it is, whatever its name
            parent = target.parent
            if model and cur.get('model') and target == self.current:
                model = None       # already here, and an upgrade in place carries it over
            mapset, drop, copy_bytes = None, [], 0
            if map_ids is None:
                if addresses and addresses['id'] in cur.get('address_indexes', []) and target == self.current:
                    addresses = None
                if mp and not pack and cur.get('map') == mp['id']:
                    mp = None          # this state's map is the one here already
                maps, addresses = [mp] if mp else [], [addresses] if addresses else []
            else:
                planned = self._plan_maps(pack, cur, target, map_ids)
                if 'error' in planned:
                    return planned
                maps, mapset, addresses, drop, copy_bytes = planned['maps'], planned['mapset'], planned['addresses'], planned['drop'], planned['copy_bytes']
            if cust:
                if not map_ids and cur.get('map') != 'us' and not (cur.get('maps') and target == self.current):
                    return {'error': 'Tick at least one state: a Custom library takes its map from the states you choose.', 'code': 'invalid'}
                cust['carry'] = self._custom_carry(cur, target, mapset, maps)
            if self.mode == 'first-run' and not pack and not cust and (model or maps or addresses):
                return {'error': 'Choose a library first.', 'code': 'nothing'}
            if not pack and not cust and not model and not maps and not mapset and not addresses:
                if self.mode == 'first-run':
                    return {'error': 'Choose a library first.', 'code': 'nothing'}
                if drop and target == self.current:      # only address indexes to remove: done now, nothing to download
                    install.drop_address_indexes(self.current, drop)
                    return {'removed': drop}
                return {'error': 'Nothing to add: this library and the local AI are already here.', 'code': 'nothing'}
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {'error': f'Lastbook cannot make a folder there ({type(e).__name__}).', 'code': 'folder'}
            if not os.access(parent, os.W_OK):
                return {'error': 'Lastbook cannot save in that folder. Choose another.', 'code': 'folder'}
            if path_too_deep(target):
                room = WINDOWS_MAX_PATH - LONGEST_INSIDE - len('\\') - len(DATA_NAME) - 1
                return {'error': f'That folder is too deep for Windows: the library’s longest file name would not fit. '
                                 f'Choose a folder whose path is at most {room} characters long, such as Documents.', 'code': 'folder'}
            if self.mode == 'first-run' and target.exists() and not install.is_library(target):
                if any(target.iterdir()):
                    return {'error': f'There is already a folder called {DATA_NAME} there that is not a library. Choose another place.', 'code': 'folder'}
            work = _work_for(target)
            srcs = dict(self.sources)
            srcs.update(sources or {})
            for f in self.found():
                if f.get('size_ok') and f['name'] not in srcs and f['kind'] in ('pack', 'model', 'map', 'addresses'):
                    srcs[f['name']] = f['path']
            names = (([f['name'] for f in pack['files']] if pack else []) + ([model['file']] if model else [])
                     + [f['name'] for m in maps for f in m['files']] + [f['name'] for a in addresses for f in a['files']])
            have = set(srcs)
            need = install.need_bytes(pack, model, have) + sum(_map_need(m, have) for m in maps) + sum(_map_need(a, have) for a in addresses) + copy_bytes
            if cust:
                need += custom_files + custom.RUN_MAX * custom.WORKERS + (cur.get('map_bytes') or 0 if cust['carry'] == 'copy' else 0)
            preserve_map = bool(pack and cur.get('pack') == pack['id'] and target == self.current and not maps and not mapset and cur.get('map') not in (None, 'base'))
            if preserve_map:
                need += cur['map_bytes']
            for name in names:     # what an earlier run already downloaded is on the disk already
                if name in have:
                    continue
                for done in (work / 'downloads' / (name + '.part'), work / 'downloads' / name):
                    try:
                        need -= os.path.getsize(done)
                    except OSError:
                        pass
            free, _ = hardware.disk_of(parent)
            if free < need:
                return {'error': f'This needs {_gb(need)} free on that disk, and it has {_gb(free)}. Free some space or choose another disk.',
                        'code': 'no_space', 'need_bytes': need, 'free_bytes': free}
            items = []
            if pack:
                items.append({'id': pack['id'], 'kind': 'pack', 'title': pack['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': pack['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            if cust:
                items.append({'id': 'custom', 'kind': 'custom', 'title': 'Custom library', 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': custom_files, 'rate_bps': 0, 'eta_s': None, 'error': None})
            for mp in maps:        # after the pack: with a pack, the map replaces the pack's own map set in the staged library
                items.append({'id': mp['id'], 'kind': 'map', 'title': mp['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': mp['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            if mapset:             # then the states are put together into one map set (engine/setup/mapmerge.py)
                items.append({'id': 'combine', 'kind': 'mapset', 'title': _mapset_title(mapset['states']), 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': sum(m['installed_bytes'] for m in maps) + copy_bytes, 'rate_bps': 0, 'eta_s': None, 'error': None})
            for entry in addresses:
                items.append({'id': entry['id'], 'kind': 'addresses', 'title': entry['title'] + ' address search', 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': entry['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            if model:
                items.append({'id': model['id'], 'kind': 'model', 'title': model['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': model['size'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'items': items, 'error': None, 'started': _now(),
                        'folder': str(target),
                        'plan': {'pack': pack, 'custom': cust, 'model': model, 'maps': maps, 'mapset': mapset, 'addresses': addresses, 'drop_addresses': drop,
                                 'parent': str(parent), 'target': str(target), 'work': str(work),
                                 'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': explicit,
                                 'retried': {}, 'switch': bool(pack or cust)}}
            s = self._settings()
            s['work'] = str(work)
            self._save_settings(s)
            _tag_sources(self.job)
            self._save_job()
            self._run()
            return self.public_job()

    def _plan_maps(self, pack, cur, target, map_ids):
        """What a checklist of states asks for. The map the library starts from is the pack's own when a pack comes with the
        job (Small: the base, Medium: California, All: the whole US), else the open library's. Returns {maps: entries to
        download, mapset: how to put them together or None, addresses: indexes to download, drop: indexes to remove,
        copy_bytes: room for a copy of the map there now}, or an error answer.

        - The same states as there now: no map work.
        - Only states added: those are downloaded and added to a copy of the map there now ('keep').
        - No state left: the base is cut out of the map there now ('derive').
        - Otherwise every ticked state is downloaded and the map is built from them alone ('first'): a combined map cannot be
          taken apart again. One state so is just that state's map set, as before this checklist existed."""
        if not isinstance(map_ids, list) or not all(isinstance(x, str) and re.fullmatch(r'[A-Z]{2}', x) for x in map_ids):
            return {'error': 'That list of states is not one Lastbook can read.', 'code': 'invalid'}
        wanted = sorted(set(map_ids))
        for st in wanted:
            if not self.manifest.map(st) and not self.manifest.address_index(st):
                return {'error': f'Lastbook has no map for {st}.', 'code': 'unavailable'}
        in_place = target == self.current
        if pack:
            own = pack.get('map') or PACK_MAPS.get(pack['id'], 'base')
            start = 'us' if own == 'us' else ([] if own == 'base' else [own])
        else:
            start = 'us' if cur.get('map') == 'us' else list(cur.get('maps') or [])
        maps, mapset, copy_bytes = [], None, 0
        if start != 'us' and set(wanted) != set(start):
            if start and set(wanted) > set(start):
                base, fetch = 'keep', [x for x in wanted if x not in start]
            elif not wanted:
                base, fetch = 'derive', []
            else:
                base, fetch = 'first', wanted
            for st in fetch:
                mp = self.manifest.map(st)
                if not mp or not mp['available']:
                    return {'error': f'The map of {st} is not available yet.', 'code': 'unavailable'}
                maps.append(mp)
            if not (base == 'first' and len(maps) == 1):
                mapset = {'base': base, 'states': wanted}
                if base != 'first' and not pack:
                    copy_bytes = cur.get('map_bytes') or 0        # the map there now is copied, then added to or cut down
        have = set(cur.get('address_indexes') or []) if in_place else set()
        addresses = []
        for st in wanted:
            entry = self.manifest.address_index(st)
            if st not in have and entry and entry['available']:
                addresses.append(entry)
        drop = sorted(have - set(wanted)) if in_place else []
        return {'maps': maps, 'mapset': mapset, 'addresses': addresses, 'drop': drop, 'copy_bytes': copy_bytes}

    # ---------------------------------------------------------------------------------------- a Custom library --
    def _index_dir(self):
        return self.settings_dir / 'setup' / 'all-index'

    def _all_pack(self):
        p = self.manifest.pack('all')
        return p if p and p.get('available') and len(p.get('files') or []) == 1 else None

    def index(self):
        """The All library's list, read before and still for the All zip the manifest offers, or None."""
        allp = self._all_pack()
        if not allp:
            return None
        if self._index is None or not self._index.matches(allp):
            self._index = custom.load_index(self._index_dir(), allp)
        return self._index

    def guides(self):
        if self._guides is None:
            base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
            self._guides = custom.guide_ids(base / 'content' / 'topics')
        return self._guides

    def custom_state(self):
        """For the screen: whether the All library's list is here, being read or failed, and once here, the groups to choose
        from, the bytes every Custom library has, and the open library's own selection when it is a Custom one."""
        self.manifest.fetch_once()
        with self.lock:
            st = dict(self.custom)
        if not self._all_pack():
            return {'state': 'unavailable', 'error': 'The All library is not offered, so there is nothing to choose from.'}
        idx = self.index() if st['state'] != 'loading' else None
        if not idx:
            return st if st['state'] != 'ready' else {'state': 'idle'}
        core = custom.files_for(idx, custom.resolve(idx, {'groups': [], 'add': [], 'remove': []}, self.guides()))
        selection = None
        if self.current:
            try:
                d = json.loads((self.current / 'pack.json').read_text(encoding='utf-8'))
                selection = custom.clean_selection(d.get('selection')) if d.get('name') == 'custom' else None
            except (OSError, ValueError, AttributeError):
                pass
        return {'state': 'ready', 'groups': custom.groups(idx, self.guides()), 'core_bytes': sum(r[0] for r in core.values()),
                'read': idx.meta.get('read'), 'selection': selection}

    def custom_load(self):
        """Reads the All library's list (about 30 MB) from the download server: only when the person opens Custom."""
        allp = self._all_pack()
        if not allp:
            return {'error': 'The All library is not offered, so there is nothing to choose from.', 'code': 'unavailable'}
        with self.lock:
            if self.custom['state'] == 'loading':
                return dict(self.custom)
            if self.index():
                return self.custom_state()
            self.custom = {'state': 'loading', 'done': 0, 'total': 0}

        def run():
            def progress(done, total):
                with self.lock:
                    self.custom.update(done=done, total=total)
            try:
                idx = custom.read_index(allp, self._index_dir(), download.Control(), progress, allowed=self.allowed, context=self.context)
                with self.lock:
                    self._index, self.custom = idx, {'state': 'ready'}
            except (download.Failed, download.Stopped, install.Refused, OSError, ValueError) as e:
                traceback.print_exc()
                msg = getattr(e, 'message', None) or f'Lastbook could not read the All library’s list ({type(e).__name__}).'
                with self.lock:
                    self.custom = {'state': 'failed', 'error': msg}
        threading.Thread(target=run, daemon=True, name='setup-custom-index').start()
        return dict(self.custom)

    def custom_items(self, group):
        idx = self.index()
        if not idx:
            return {'error': 'Read the All library’s list first.', 'code': 'custom_index'}
        if not isinstance(group, str) or len(group) > 200:
            return {'error': 'bad request', 'code': 'invalid'}
        return {'group': group, 'items': custom.items(idx, group, self.guides())}

    def _plan_custom(self, selection, cur, map_ids):
        idx = self.index()
        if not idx:
            return {'error': 'The All library’s list is not read yet. Open Custom again.', 'code': 'custom_index'}
        sel = custom.clean_selection(selection)
        if sel is None:
            return {'error': 'That choice is not one Lastbook can read.', 'code': 'invalid'}
        kept = custom.resolve(idx, sel, self.guides())
        if not any(custom._choosable(idx.by_id[k]) for k in kept):
            return {'error': 'Nothing is chosen yet.', 'code': 'nothing'}
        files = custom.files_for(idx, kept)
        same_disk = self.current and _same_disk(self.current, self.current.parent)
        reuse = custom.reusable(self.current, files) if same_disk else set()
        fetch = sum(row[0] for rel, row in files.items() if rel not in reuse)
        return {'custom': {'selection': sel, 'etag': idx.etag, 'key': custom.selection_key(sel, idx.etag)}, 'fetch_bytes': fetch}

    def _custom_carry(self, cur, target, mapset, maps):
        """What the new Custom library takes of the open library's map: 'link' it as it is (no change to the states), 'copy' it to
        be added to or cut down (a mapset that starts from it), or nothing (the map is built from the states downloaded)."""
        if not self.current or target != self.current or not install.map_files(self.current):
            return None
        if mapset and mapset['base'] in ('keep', 'derive'):
            return 'copy'
        if not maps and not mapset:
            return 'link'
        return None

    def _checked_custom(self, c):
        if not isinstance(c, dict):
            return None
        idx = self.index()
        sel = custom.clean_selection(c.get('selection'))
        if not idx or sel is None or c.get('etag') != idx.etag:
            return None
        carry = c.get('carry') if c.get('carry') in ('link', 'copy') else None
        return {'selection': sel, 'etag': idx.etag, 'key': custom.selection_key(sel, idx.etag), 'carry': carry}

    def _do_custom(self, it, plan, work, control):
        """Builds the Custom library in unpack/LastbookData: files linked from the open library where they are the same, the
        rest downloaded from the All zip by range (custom.fetch), then its own catalog, search index and lists. What is in place
        is noted in the settings folder, so a stop carries on where it was."""
        c = plan['custom']
        idx = self.index()
        if not idx or idx.etag != c['etag']:
            raise download.Failed('changed', 'The All library on the download server changed since you chose. Go back and choose again.')
        kept = custom.resolve(idx, c['selection'], self.guides())
        files = custom.files_for(idx, kept)
        staged = work / 'unpack'
        _fresh_unpack(staged, {'custom': c['key']})
        lib = staged / DATA_NAME
        lib.mkdir(parents=True, exist_ok=True)
        note = self.settings_dir / 'setup' / 'custom-done.txt'
        try:
            lines = note.read_text(encoding='utf-8').splitlines()
            done = set(lines[1:]) if lines and lines[0] == c['key'] else set()
        except OSError:
            done = set()
        done = {r for r in done if r in files and (lib / r).is_file() and (lib / r).stat().st_size == files[r][0]}
        note.parent.mkdir(parents=True, exist_ok=True)
        with open(note, 'w', encoding='utf-8') as f:
            f.write(c['key'] + '\n' + ''.join(r + '\n' for r in sorted(done)))
        mark_lock = threading.Lock()

        def mark(rel):
            with mark_lock, open(note, 'a', encoding='utf-8') as f:
                f.write(rel + '\n')
        total = sum(row[0] for row in files.values()) or 1
        tracker = _Item(it)
        self._set(it, 'downloading', bytes_done=0, bytes_total=total)
        got = sum(files[r][0] for r in done)
        reuse = custom.reusable(self.current, files) if self.current else set()
        for rel in sorted(reuse - done):
            control.check()
            custom.link_or_copy(self.current / rel, lib / rel)
            mark(rel)
            done.add(rel)
            got += files[rel][0]
            tracker.progress(got, total, 'downloading')
        todo = [r for r in files if r not in done]
        base = got
        custom.fetch(idx, todo, lib, work / 'downloads' / 'custom', control, lambda d, t: tracker.progress(base + d, total, 'downloading'),
                     mark, allowed=self.allowed, context=self.context, waiting=tracker.waiting)
        self._set(it, 'activating')
        custom.finish_library(idx, lib, kept, c['selection'], files)
        if c.get('carry') and self.current:
            for rel in install.map_files(self.current):
                control.check()
                if c['carry'] == 'link':
                    custom.link_or_copy(self.current / rel, lib / rel)
                else:
                    (lib / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.current / rel, lib / rel)
        try:
            note.unlink()
        except OSError:
            pass

    def _start_repair(self, ai):
        """Re-check the open library's files (pack-manifest.tsv) and the model's SHA-256. What is wrong is fetched again: the pack is
        downloaded and switched to as in an upgrade, the model replaced in place. Nothing is downloaded if all is well."""
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            cur = self.current_info()
            if self.mode != 'manage' or not cur:
                return {'error': 'There is no library to repair yet.', 'code': 'nothing'}
            pack = self.manifest.pack(cur['pack']) if cur['pack'] else None
            if not pack or not pack['available']:
                return {'error': 'Lastbook cannot tell which library this is, so it cannot repair it. Download a library instead.', 'code': 'unavailable'}
            model = self.manifest.model() if (ai or cur['model']) else None
            parent, target = self.current.parent, self.current
            work = _work_for(target)
            srcs = {f['name']: f['path'] for f in self.found() if f.get('size_ok') and f['kind'] in ('pack', 'model', 'map')}
            items = [{'id': pack['id'], 'kind': 'pack', 'title': pack['title'], 'state': 'waiting', 'bytes_done': 0,
                      'bytes_total': pack['installed_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None}]
            if model:
                items.append({'id': model['id'], 'kind': 'model', 'title': model['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': model['size'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            maps, mapset = [], None
            if _separate_map(target):
                try:                 # a state map's files come from its own download, not the pack's: the pack check skips them
                    install.check_map(target)
                except install.Refused as e:
                    print('setup: repair:', e.message, flush=True)
                    states = install.map_states(target)
                    maps = [self.manifest.map(st) for st in states]
                    if not states or not all(m and m['available'] for m in maps):
                        return {'error': f'The {cur["map"]} map in this library is damaged, and this version does not offer it to download again. '
                                         'Choose a map again.', 'code': 'unavailable'}
                    if len(maps) > 1:        # a combined map: every state again, put together as before
                        mapset = {'base': 'first', 'states': states}
                    for i, mp in enumerate(maps):
                        items.insert(1 + i, {'id': mp['id'], 'kind': 'map', 'title': mp['title'], 'state': 'waiting', 'bytes_done': 0,
                                             'bytes_total': mp['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
                    if mapset:
                        items.insert(1 + len(maps), {'id': 'combine', 'kind': 'mapset', 'title': _mapset_title(states), 'state': 'waiting', 'bytes_done': 0,
                                                     'bytes_total': sum(m['installed_bytes'] for m in maps), 'rate_bps': 0, 'eta_s': None, 'error': None})
            names = [f['name'] for f in pack['files']] + ([model['file']] if model else []) + [f['name'] for m in maps for f in m['files']]
            self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'items': items, 'error': None, 'started': _now(), 'folder': str(target),
                        'repair': True,
                        'plan': {'pack': pack, 'model': model, 'maps': maps, 'mapset': mapset, 'addresses': [], 'drop_addresses': [],
                                 'parent': str(parent), 'target': str(target), 'work': str(work),
                                 'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': False, 'retried': {},
                                 'switch': True, 'repair': True}}
            s = self._settings()
            s['work'] = str(work)
            self._save_settings(s)
            _tag_sources(self.job)
            self._save_job()
            self._run()
            return self.public_job()

    def _check_library(self, it, lib, control):
        """Files of the open library that are missing or differ from its pack-manifest.tsv (path, bytes, sha256)."""
        try:
            rows = [l.split('\t') for l in (lib / 'pack-manifest.tsv').read_text(encoding='utf-8').splitlines()[1:] if l.strip()]
            rows = [(r[0], int(r[1]), r[2]) for r in rows]
        except (OSError, ValueError, IndexError):
            return ['pack-manifest.tsv']
        if _separate_map(lib):        # a state map replaced the pack's own: _start_repair checks it against map-set.json instead
            rows = [r for r in rows if not install.map_member(r[0])]
        total = sum(r[1] for r in rows) or 1
        tracker, done, bad = _Item(it), 0, []
        self._set(it, 'verifying', bytes_done=0, bytes_total=total)
        for rel, size, digest in rows:
            p = lib / rel
            if '..' in Path(rel).parts or Path(rel).is_absolute() or not p.is_file() or p.stat().st_size != size:
                bad.append(rel)
                done += size
                tracker.progress(done, total, 'verifying')
                continue
            base = done
            if _sha256(p, control, lambda d, t: tracker.progress(base + d, total, 'verifying')) != digest:
                bad.append(rel)
            done = base + size
        return bad

    def _run(self):
        self.control = download.Control()
        self.thread = threading.Thread(target=self._work, args=(self.control,), daemon=True, name='setup-job')
        self.thread.start()

    def pause(self):
        with self.lock:
            if self.job and self.job['state'] in ACTIVE and self.control:
                self.control.stop('paused')
        self._join()
        return self.public_job()

    def resume(self):
        with self.lock:
            if self.job and self.job['state'] == 'paused':
                self.job['state'] = 'waiting'
                for it in self.job['items']:
                    it.pop('paused_from', None)
                self._save_job()
                self._run()
        return self.public_job()

    def retry(self):
        with self.lock:
            if self.job and self.job['state'] == 'failed':
                self.job['state'], self.job['error'] = 'waiting', None
                self.job['plan']['retried'] = {}
                for it in self.job['items']:
                    if it['state'] != 'done':
                        it['state'], it['error'] = 'waiting', None
                self._save_job()
                self._run()
        return self.public_job()

    def cancel(self):
        with self.lock:
            job = self.job
            if not job:
                return None
            if job['state'] in ACTIVE and self.control:
                self.control.stop('cancelled')
        self._join()
        with self.lock:
            if job['state'] != 'done' or job['plan'].get('switch'):     # a staged library not yet switched to can still be dropped
                self._clean(job)
                try:
                    _pending_file(self.settings_dir).unlink()
                except OSError:
                    pass
                job['state'] = 'failed'
                job['error'] = {'code': 'cancelled', 'message': 'Cancelled. Nothing was changed.'}
                for it in job['items']:
                    if it['state'] != 'done':
                        it['state'] = 'failed'
                self._save_job()
        return self.public_job()

    def _join(self):
        t = self.thread
        if t and t is not threading.current_thread():
            t.join(30)

    def _clean(self, job):
        """What the job made in its work folder goes; the person's own files and any library stay."""
        work = Path(job['plan']['work'])
        for sub in ('downloads', 'unpack', 'unpack-map', 'unpack-maps', 'unpack-addresses'):
            shutil.rmtree(work / sub, ignore_errors=True)

    def use(self, path):
        p = Path(path or '').expanduser()
        if not str(path or '').strip() or not p.exists():
            return {'error': 'Lastbook cannot find that file or folder.', 'code': 'missing'}
        if p.is_dir():
            lib = p if install.is_library(p) else p / DATA_NAME
            if not install.is_library(lib):
                known = {f['name'] for pk in self.manifest.current['packs'] for f in pk['files']}
                model = self.manifest.model()
                if model:
                    known.add(model['file'])
                inside = sorted(c for c in p.iterdir() if c.name in known and c.is_file()) if p.is_dir() else []
                if inside:          # the person explicitly chose this folder
                    return self.use(str(inside[0]))
                return {'error': f'“{p.name}” is not a Lastbook library. Choose the folder called {DATA_NAME}.', 'code': 'not_library'}
            if not os.access(lib, os.W_OK):
                return {'error': f'Lastbook cannot save in “{lib.name}”. Move it somewhere you can save to, then choose it there.', 'code': 'folder'}
            with self.lock:
                if self.job and self.job['state'] in ACTIVE:
                    return {'error': 'A download is under way. Pause or cancel it first.', 'code': 'busy'}
                self.job = {'id': uuid.uuid4().hex[:12], 'state': 'done', 'error': None, 'started': _now(), 'folder': str(lib),
                            'items': [{'id': 'library', 'kind': 'pack', 'title': lib.name, 'state': 'done', 'bytes_done': 0, 'bytes_total': 0,
                                       'rate_bps': 0, 'eta_s': None, 'error': None}],
                            'plan': {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}}
                return self.public_job()
        name = p.name
        for pk in self.manifest.current['packs']:
            for f in pk['files']:
                if f['name'] == name:
                    if p.stat().st_size != f['size']:
                        return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(f["size"])} of the published {name}. The download may not have finished.', 'code': 'size'}
                    if not pk['available']:
                        return {'error': 'That library is not available in this version.', 'code': 'unavailable'}
                    self.sources[name] = str(p)
                    return self.start(pk['id'], False, None, {name: str(p)}, explicit=True)
        for x in self.manifest.current.get('maps', []):
            for f in x['files']:
                if f['name'] == name:
                    if p.stat().st_size != f['size']:
                        return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(f["size"])} of the published {name}. The download may not have finished.', 'code': 'size'}
                    self.sources[name] = str(p)
                    if self.mode == 'manage':
                        return self.start(None, False, None, {name: str(p)}, explicit=True, map_id=x['id'])
                    return self.public_job() or {'job': None, 'noted': True}
        for entry in self.manifest.current.get('address_indexes', []):
            for file in entry['files']:
                if file['name'] != name:
                    continue
                if p.stat().st_size != file['size']:
                    return {'error': 'The address index download is incomplete.', 'code': 'size'}
                self.sources[name] = str(p)
                if self.mode == 'manage':
                    return self.start(sources={name: str(p)}, explicit=True, address_id=entry['id'])
                return self.public_job() or {'job': None, 'noted': True}
        m = self.manifest.model()
        if m and name == m['file']:
            if p.stat().st_size != m['size']:
                return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(m["size"])} of the model. The download may not have finished.', 'code': 'size'}
            self.sources[name] = str(p)
            if self.mode == 'manage':
                return self.start(None, True, None, {name: str(p)}, explicit=True)
            return self.public_job() or {'job': None, 'noted': True}
        return {'error': 'That is not a file Lastbook knows. Choose a Lastbook library zip or the model file.', 'code': 'unknown'}

    def finish(self):
        with self.lock:
            job = self.job
            if not job or job['state'] != 'done':
                return {'error': 'Setup has not finished yet.', 'code': 'not_done'}
            plan = job['plan']
            folder = plan.get('use_library') or plan['target']
        if not self.on_finish:
            return {'ok': True, 'restart': True, 'folder': folder}
        threading.Timer(0.4, self.on_finish, [folder]).start()     # after this answer has reached the window
        return {'ok': True, 'folder': folder}

    def pick(self, files=False):
        """{path, free_bytes, disk}, or {path: null, cancelled: true}."""
        path = None
        if self.pick_folder:
            try:
                path = (self.pick_folder(files=True) if files else self.pick_folder()) or None
            except Exception:
                traceback.print_exc()
        if not path:
            return {'path': None, 'cancelled': True}
        try:
            free, _ = hardware.disk_of(path)
        except OSError:
            free = None
        return {'path': path, 'free_bytes': free, 'disk': hardware.disk_name(path)}

    def remove_ai(self):
        """Manage mode: the model file goes from the open library. The library itself is not touched."""
        m = self.manifest.model()
        if self.mode != 'manage' or not self.current or not m:
            return {'error': 'There is no local AI in this library to remove.', 'code': 'nothing'}
        with self.lock:
            if self.job and self.job['state'] in ACTIVE:
                return {'error': 'A download is under way. Pause or cancel it first.', 'code': 'busy'}
        p = self.current / 'models' / m['file']
        if not p.is_file():
            return {'error': 'There is no local AI in this library to remove.', 'code': 'nothing'}
        llm = self.llm()
        local = getattr(llm, 'LOCAL', None) if llm else None
        try:
            if local is not None and hasattr(local, 'stop'):
                local.stop('removed')
        except Exception:
            traceback.print_exc()
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError as e:
            return {'error': f'Lastbook could not remove the model file ({type(e).__name__}). Quit anything using it, then try again.', 'code': 'folder'}
        if local is not None and getattr(local, '_started', False):
            threading.Thread(target=local.discover, daemon=True).start()
        return {'ok': True, 'freed_bytes': size}

    def delete_old(self, path):
        s = self._settings()
        olds = s.get('old', [])
        if path not in olds:
            return {'error': 'Lastbook only deletes an old library it moved aside itself.', 'code': 'unknown'}
        p = Path(path)
        if '.old-' not in p.name:
            return {'error': 'That is not an old library.', 'code': 'unknown'}

        def gone():
            shutil.rmtree(p, ignore_errors=True)
            s2 = self._settings()
            s2['old'] = [o for o in s2.get('old', []) if o != path or Path(o).exists()]
            self._save_settings(s2)
            self.deleting.discard(path)
        self.deleting.add(path)
        threading.Thread(target=gone, daemon=True, name='setup-delete-old').start()
        return {'ok': True, 'deleting': path}

    # ------------------------------------------------------------------------------------------------------ the work --
    def _set(self, it, state, **kw):
        with self.lock:
            if it is not None:
                it['state'] = state
                it.update(kw)
            self.job['state'] = state
            self._save_job()

    def _fail(self, it, code, message, **extra):
        with self.lock:
            err = {'code': code, 'message': message, **extra}
            if it is not None:
                it['state'], it['error'], it['rate_bps'], it['eta_s'] = 'failed', err, 0, None
            self.job['state'], self.job['error'] = 'failed', err
            self._save_quietly()
        print(f'setup: failed ({code}): {message}', flush=True)

    def _save_quietly(self):
        try:
            self._save_job()
        except OSError as e:
            print('setup: could not save the job:', e, flush=True)

    def _work(self, control):
        job = self.job
        plan = job['plan']
        it = None
        try:
            work = Path(plan['work'])
            (work / 'downloads').mkdir(parents=True, exist_ok=True)
            for it in job['items']:
                if it['state'] == 'done':
                    continue
                if it['kind'] == 'pack':
                    self._do_pack(it, plan, work, control)
                elif it['kind'] == 'custom':
                    self._do_custom(it, plan, work, control)
                elif it['kind'] == 'map':
                    self._do_map(it, plan, work, control)
                elif it['kind'] == 'mapset':
                    self._do_mapset(it, plan, work, control)
                elif it['kind'] == 'addresses':
                    self._do_addresses(it, plan, work, control)
                else:
                    self._do_model(it, plan, work, control)
                with self.lock:
                    it['state'], it['rate_bps'], it['eta_s'] = 'done', 0, None
                    self._save_job()
            it = None
            self._finish_job(plan, work)
        except download.Stopped as s:
            with self.lock:
                if s.why == 'paused':
                    if it is not None:
                        it['state'], it['rate_bps'], it['eta_s'] = 'paused', 0, None
                    job['state'] = 'paused'
                self._save_quietly()
        except download.Failed as e:
            self._fail(it, e.code, e.message, **e.extra)
        except install.Refused as e:
            self._fail(it, e.code, e.message)
        except OSError as e:
            if download.disk_full(e):
                self._fail(it, 'no_space', 'The disk filled up. Free some space, then try again.')
            else:
                traceback.print_exc()
                self._fail(it, 'unpack' if it and job['state'] == 'unpacking' else 'network', f'Lastbook could not write a file ({type(e).__name__}: {e.strerror or e}).')
        except Exception as e:
            traceback.print_exc()
            self._fail(it, 'activate', f'Setup stopped on an internal error ({type(e).__name__}).')

    def _get_file(self, it, f, plan, work, control, offset):
        """The file, checked against its SHA-256: one already on this computer, or a download. Returns its path."""
        tracker = _Item(it)
        name, src = f['name'], plan['sources'].get(f['name'])
        if src and os.path.isfile(src) and name in plan.setdefault('verified', []):
            return src, False
        explicit_use = bool(plan.get('explicit'))
        if src and os.path.isfile(src):
            self._set(it, 'verifying')
            digest = _sha256(src, control, lambda d, t: tracker.progress(offset + d, it['bytes_total'], 'verifying'))
            if digest == f['sha256']:
                plan['verified'].append(name)
                return src, False
            st = os.stat(src)
            self.rejected[src] = (st.st_size, st.st_mtime_ns)
            plan['sources'].pop(name, None)       # a retry downloads it instead; the file itself is left where it is, untouched
            it['source'] = 'download'
            plan['explicit'] = False
            if explicit_use:
                folder = Path(src).parent.name or str(Path(src).parent)
                raise download.Failed('found_mismatch', f'The {name} in {folder} doesn’t match the published file, so it was not used. '
                                                        'It was left as it is. Download it instead?', file=src)
            print(f'setup: {src} does not match; downloading instead', flush=True)
        dest = work / 'downloads' / name
        if not dest.exists():       # room for what is still to come (the space check at start counted a file already here as had)
            try:
                still = f['size'] - os.path.getsize(str(dest) + '.part')
            except OSError:
                still = f['size']
            free, _ = hardware.disk_of(work)
            if free < still + 512 * 2 ** 20:
                raise download.Failed('no_space', f'Downloading {name} needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.',
                                      need_bytes=still, free_bytes=free)
        while True:
            self._set(it, 'downloading')
            d = download.Download(f['url'], dest, f['size'], f['sha256'], control,
                                  lambda done, total, phase: tracker.progress(offset + done, it['bytes_total'], phase),
                                  allowed=self.allowed, context=self.context, waiting=tracker.waiting)
            try:
                return d.run(), True
            except download.BadChecksum:
                tries = plan['retried'].get(name, 0)
                if tries >= 1:
                    raise download.Failed('checksum', f'{name} did not match its published checksum twice. The file on the server may be damaged; try again later.')
                plan['retried'][name] = tries + 1
                print(f'setup: {name} failed its checksum; downloading it once more', flush=True)

    def _do_pack(self, it, plan, work, control):
        pack = plan['pack']
        if plan.get('repair') and 'damaged' not in plan:
            bad = self._check_library(it, Path(plan['target']), control)
            plan['damaged'] = bad
            print(f'setup: repair found {len(bad)} damaged or missing files', flush=True)
            if not bad:
                plan['switch'] = False
                return
            need = install.need_bytes(pack, None, set(plan['sources']))
            free, _ = hardware.disk_of(work.parent)
            if free < need:
                raise download.Failed('no_space', f'{len(bad)} files need fetching again. That needs {_gb(need)} free, and the disk has {_gb(free)}.')
            with self.lock:
                it['bytes_total'], it['bytes_done'] = pack['download_bytes'], 0
        staged = work / 'unpack'
        _fresh_unpack(staged, {'pack': pack['id'], 'revision': pack.get('revision'), 'files': [f['sha256'] for f in pack['files']]})
        got, offset = [], 0
        for f in pack['files']:
            path, ours = self._get_file(it, f, plan, work, control, offset)
            got.append((path, ours))
            offset += f['size']
        tracker = _Item(it)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=pack['installed_bytes'])
        total = sum(install.check_zip(path, pack['installed_bytes'])[1] for path, _ in got)   # every member checked before any is written
        still = max(total - _tree_size(staged), 0)
        free, _ = hardware.disk_of(work)
        if free < still + 512 * 2 ** 20:
            raise download.Failed('no_space', f'Unpacking needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.')
        for path, _ in got:
            lib = install.unpack(path, staged, pack['installed_bytes'], control, lambda d, t: tracker.progress(d, t, 'unpacking'))
        if pack.get('revision'):
            metadata = json.loads((lib / 'pack.json').read_text(encoding='utf-8'))
            if metadata.get('content_revision') != pack['revision']:
                raise install.Refused('unpack', 'The library content version does not match this download.')
        current = self.current_info() or {}
        if (not plan.get('maps') and not plan.get('mapset') and Path(plan['target']) == self.current and current.get('pack') == pack['id']
                and _separate_map(self.current)):          # kept only when Repair found it whole, or it would have its own item
            install.copy_map(self.current, lib)
        install._write_json(lib / 'setup-pack.json', {'pack': pack['id'], 'installed': _now(), 'files': [f['name'] for f in pack['files']]})
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _do_map(self, it, plan, work, control):
        """A state map: download, check, unpack (map files only). On its own (no mapset) it is the new map: with a pack, it
        replaces the staged library's own map set at once; else it is staged and switched in when the app restarts
        (install._apply_map). With a mapset it waits in unpack-maps/<state>/ for _do_mapset to put the states together."""
        mp = _entry_for(plan, it)
        got, offset = [], 0
        for f in mp['files']:
            path, ours = self._get_file(it, f, plan, work, control, offset)
            got.append((path, ours))
            offset += f['size']
        tracker = _Item(it)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=mp['installed_bytes'])
        staged = _map_unpack_dir(plan, mp)
        for path, _ in got:
            install.check_zip(path, mp['installed_bytes'], install.map_member)
        for path, _ in got:
            lib = install.unpack(path, staged, mp['installed_bytes'], control, lambda d, t: tracker.progress(d, t, 'unpacking'), allowed=install.map_member)
        if install.map_name(lib) != mp['id']:
            raise install.Refused('unpack', f'The map file says it is {install.map_name(lib)}, not {mp["id"]}.')
        if plan.get('mapset'):
            pass
        elif plan['switch']:
            install.replace_map_in(work / 'unpack' / DATA_NAME, lib)
            shutil.rmtree(staged, ignore_errors=True)
        else:
            plan['map_new'] = str(lib)
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _do_mapset(self, it, plan, work, control):
        """Puts the states together (engine/setup/mapmerge.py) into one map set: inside the staged library when a pack comes with
        the job, else in unpack-map/, which is switched in when the app restarts like a single state map. The starting point:
        'keep' the map there now (a copy of it, unless the staged pack's own), 'derive' the base from it, or the 'first' state.
        Each state merged is noted in the plan, so a stop carries on with the next."""
        ms = plan['mapset']
        target = work / 'unpack' / DATA_NAME if plan['switch'] else work / 'unpack-map' / DATA_NAME
        fetched = [m['id'] for m in plan['maps']]
        done = plan.setdefault('mapset_merged', [])
        total = it.get('bytes_total') or 1
        tracker = _Item(it)
        self._set(it, 'activating', bytes_done=it.get('bytes_done', 0))
        if not plan.get('mapset_ready'):
            if not plan['switch']:
                shutil.rmtree(work / 'unpack-map', ignore_errors=True)
                target.mkdir(parents=True)
            if ms['base'] == 'first':
                first = fetched[0]
                source = _map_unpack_dir(plan, {'id': first}) / DATA_NAME
                for rel in install.map_files(target):
                    os.unlink(target / rel)
                for rel in install.map_files(source):
                    (target / rel).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source / rel, target / rel)
                done.append(first)
            elif not plan['switch']:
                for rel in install.map_files(self.current):
                    control.check()
                    (target / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.current / rel, target / rel)
            if ms['base'] == 'derive':
                mapmerge.derive_base(target)
            plan['mapset_ready'] = True
            self._save_job()
        merged = sum(m['installed_bytes'] for m in plan['maps'] if m['id'] in done)
        tracker.progress(min(merged, total), total, 'activating')
        for mp in plan['maps']:
            if mp['id'] in done:
                continue
            control.check()
            source = _map_unpack_dir(plan, mp) / DATA_NAME
            mapmerge.merge_into(target, source)
            done.append(mp['id'])
            self._save_job()
            shutil.rmtree(_map_unpack_dir(plan, mp), ignore_errors=True)
            merged += mp['installed_bytes']
            tracker.progress(min(merged, total), total, 'activating')
        mapmerge.finish(target, ms['states'])
        shutil.rmtree(work / 'unpack-maps', ignore_errors=True)
        if not plan['switch']:
            plan['map_new'] = str(target)

    def _do_addresses(self, it, plan, work, control):
        entry = _entry_for(plan, it)
        relative = 'derived/geo/addresses/' + entry['id'] + '.sqlite'
        directories = {'', 'derived', 'derived/geo', 'derived/geo/addresses'}

        def allowed(name, is_dir=False):
            return name in directories if is_dir else name == relative

        def verify(lib):
            path = lib / relative
            if not path.is_file() or path.stat().st_size != entry['installed_bytes']:
                raise install.Refused('unpack', 'The address index is missing or the wrong size.')

        got = []
        offset = 0
        for file in entry['files']:
            path, ours = self._get_file(it, file, plan, work, control, offset)
            got.append((path, ours))
            offset += file['size']
        for path, _ in got:
            install.check_zip(path, entry['installed_bytes'], allowed)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=entry['installed_bytes'])
        tracker = _Item(it)
        for path, _ in got:
            lib = install.unpack(path, work / 'unpack-addresses', entry['installed_bytes'], control,
                                 lambda done, total: tracker.progress(done, total, 'unpacking'), allowed=allowed, verify=verify)
        target = work / 'unpack' / DATA_NAME if plan['switch'] else Path(plan['target'])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._set(it, 'activating')
        os.replace(lib / relative, destination)
        install._write_json(destination.with_suffix('.json'), {'files': {file['name']: file['sha256'] for file in entry['files']}})
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _do_model(self, it, plan, work, control):
        m = plan['model']
        path, ours = self._get_file(it, {'name': m['file'], 'url': m['url'], 'size': m['size'], 'sha256': m['sha256']}, plan, work, control, 0)
        lib = (work / 'unpack' / DATA_NAME) if plan['switch'] else Path(self.current or plan['target'])
        dest = lib / 'models' / m['file']
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._set(it, 'activating')
        if ours or _same_disk(path, dest):
            os.replace(path, dest)
        else:
            tmp = dest.with_name(dest.name + '.partial')
            shutil.copyfile(path, tmp)
            os.replace(tmp, dest)
        plan['model_file'] = str(Path(plan['target']) / 'models' / m['file']) if plan['switch'] else str(dest)
        plan['model_sha256'] = m['sha256']           # what _get_file just matched: the streamed or re-read hash equalled this
        if not plan['switch'] and self.current:      # the library open now: the AI can use it at once
            self._mark_checked(dest, m['sha256'])

    def _mark_checked(self, path, sha256):
        llm = self.llm()
        if not llm:
            return
        try:
            llm.mark_checked(path, sha256)
            if getattr(llm, 'LOCAL', None) and getattr(llm.LOCAL, '_started', False):
                threading.Thread(target=llm.LOCAL.discover, daemon=True).start()
        except Exception as e:
            print('setup: mark_checked:', type(e).__name__, e, flush=True)

    def _finish_job(self, plan, work):
        with self.lock:
            if plan['switch']:
                lib = work / 'unpack' / DATA_NAME
                if not install.is_library(lib):
                    raise install.Refused('activate', 'The new library is not complete.')
                install.stage_switch(_pending_file(self.settings_dir), lib, plan['target'], plan.get('model_file'), model_sha256=plan.get('model_sha256'),
                                     drop_addresses=plan.get('drop_addresses'))
            elif plan.get('map_new'):
                install.stage_switch(_pending_file(self.settings_dir), plan['map_new'], plan['target'], kind='map', drop_addresses=plan.get('drop_addresses'))
            elif plan.get('drop_addresses'):
                install.drop_address_indexes(plan['target'], plan['drop_addresses'])
            self.job['state'] = 'done'
            for it in self.job['items']:
                it['rate_bps'], it['eta_s'] = 0, None
            self._save_job()


WINDOWS_MAX_PATH = 259      # characters in a path Windows programs can open with long paths off (the default): MAX_PATH less the end
LONGEST_INSIDE = 190        # the longest file path inside LastbookData in any pack (Medium and All, measured 12 Nov 2026; Small 166)


def path_too_deep(target, windows=None):
    """On Windows the library reads its files by ordinary paths, so <target>\\<longest file> must fit in WINDOWS_MAX_PATH."""
    windows = os.name == 'nt' if windows is None else windows
    return windows and len(str(target)) + 1 + LONGEST_INSIDE > WINDOWS_MAX_PATH


def _tag_sources(job):
    """Each item's source: 'found' when all its files are already on this computer (plan.sources), else 'download'."""
    plan = job['plan']
    for it in job['items']:
        entry = _entry_for(plan, it)
        if it['kind'] == 'pack' and plan.get('pack'):
            names = [f['name'] for f in plan['pack']['files']]
        elif it['kind'] in ('map', 'addresses') and entry:
            names = [f['name'] for f in entry['files']]
        elif it['kind'] == 'model' and plan.get('model'):
            names = [plan['model']['file']]
        else:
            names = []
        it['source'] = 'found' if names and all(n in plan['sources'] for n in names) else 'download'


def _map_need(mp, have):
    """Room a state map needs: what is still to download plus its unpacked size."""
    if not mp:
        return 0
    return sum(f['size'] for f in mp['files'] if f['name'] not in have) + (mp.get('installed_bytes') or 0)


PACK_MAPS = {'medium': 'CA', 'all': 'us'}    # the map set each pack carries, where the manifest's entry does not say (web/setup.js has the same)


def _entries(plan, key, legacy, lookup):
    """The manifest's entries for a saved plan's list under `key` (or its older single entry under `legacy`), by id. [] for
    none, None when one is no longer offered: the job is then not used."""
    raw = plan.get(key)
    if raw is None and legacy:
        raw = [plan[legacy]] if plan.get(legacy) else []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        entry = lookup(r.get('id')) if isinstance(r, dict) else None
        if not entry or entry['id'] in {e['id'] for e in out}:
            return None
        out.append(entry)
    return out


def _checked_mapset(ms, maps):
    """A saved mapset as start() writes it, or None."""
    if not isinstance(ms, dict) or ms.get('base') not in ('keep', 'derive', 'first'):
        return None
    states = ms.get('states')
    if not isinstance(states, list) or not all(isinstance(x, str) and re.fullmatch(r'[A-Z]{2}', x) for x in states):
        return None
    if ms['base'] == 'first' and not maps:
        return None
    return {'base': ms['base'], 'states': sorted(set(states))}


def _entry_for(plan, it):
    """The manifest entry of a map or address item, from the plan's lists."""
    key = {'map': 'maps', 'addresses': 'addresses'}.get(it.get('kind'))
    return next((e for e in plan.get(key) or [] if e['id'] == it.get('id')), None) if key else None


def _map_unpack_dir(plan, mp):
    """Where a state map unpacks: unpack-map/ when it is the new map on its own (install._pending_ok expects it there),
    unpack-maps/<state>/ while it waits to be put together with others."""
    work = Path(plan['work'])
    return work / 'unpack-maps' / mp['id'] if plan.get('mapset') else work / 'unpack-map'


def _mapset_title(states):
    if not states:
        return 'Base map'
    return 'Map of ' + (', '.join(states[:-1]) + ' and ' + states[-1] if len(states) > 1 else states[0])


def _margin(total):
    """Room left free beyond what an install needs: 5% of the disk, at least 5 GB and at most 20 GB (5% of an 8 TB disk would
    rule out every library)."""
    return max(RECOMMEND_MARGIN, min(int((total or 0) * 0.05), RECOMMEND_MARGIN_MAX))


def _fresh_unpack(staged, stamp):
    """The unpack folder keeps whole files from an earlier run, so a resumed job does not unpack them again. That is only right
    for the same pack: files left by another pack (or another revision) would be taken as whole when their size matches. So the
    folder carries a note of what it was unpacked from, and is emptied when that is not what comes next. The new note is written
    only once the folder is empty: a file that cannot be removed (still open on Windows) stops setup instead of being reused."""
    note = Path(staged) / 'unpacked-from.json'
    try:
        same = json.loads(note.read_text(encoding='utf-8')) == stamp
    except (OSError, ValueError):
        same = False
    if not same and Path(staged).exists():
        try:
            note.unlink(missing_ok=True)
            shutil.rmtree(staged)
        except OSError as e:
            raise install.Refused('unpack', f'Lastbook could not clear the files of the previous download ({type(e).__name__}). '
                                            'Close anything that may be using them, then try again.')
    Path(staged).mkdir(parents=True, exist_ok=True)
    install._write_json(note, stamp)


def _tree_size(p):
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def _separate_map(lib):
    """Does the library hold a map set installed apart from its pack (a state map), rather than the pack's own (All's whole US)?
    The pack's own set is named by pack.json's `map`, and its map-set.json is listed in pack-manifest.tsv with its other files,
    so they are checked and fetched with the pack. Ownership never depends on map-set.json's contents being whole: a damaged one
    of the pack's own then shows up as a damaged pack file."""
    lib = Path(lib)
    d = install.map_set(lib)
    if not d:
        return False
    try:
        own = json.loads((lib / 'pack.json').read_text(encoding='utf-8')).get('map')
        listed = any(line.split('\t', 1)[0] == 'map-set.json'
                     for line in (lib / 'pack-manifest.tsv').read_text(encoding='utf-8').splitlines()[1:])
    except (OSError, ValueError, AttributeError):
        return True
    return not (listed and d.get('name') == own)


# ------------------------------------------------------------------------------------------------ startup and the app --
def _work_for(target):
    """Setup's work folder for a library: <its name>.setup beside it (LastbookData.setup for every library setup installs)."""
    return Path(target).with_name(Path(target).name + WORK_SUFFIX)


def _job_file(settings_dir):
    """The job and the pending switch live in the app's own settings folder, never in the work folder beside a library:
    a LastbookData.setup can arrive with a folder or a drive, and what is in it is not obeyed (audit E3)."""
    return Path(settings_dir) / 'setup' / 'job.json'


def _pending_file(settings_dir):
    return Path(settings_dir) / 'setup' / 'pending.json'


def startup(settings_dir, only=None):
    """app/main.py calls this first: a staged switch is made now, before the engine opens anything. Returns the library to open
    (or None to look as usual) and a note: {target, old, model_file, error}."""
    settings_dir = Path(settings_dir)
    try:
        s = json.loads((settings_dir / 'setup.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, None
    work = s.get('work')
    if not work or not isinstance(work, str):
        return None, None
    if only is not None and install._real(work) != install._real(_work_for(only)):
        return None, None        # opened with --data: a switch staged for another library waits for a normal start
    pend = _pending_file(settings_dir)
    try:
        return install.apply_pending(pend, work)
    except Exception as e:        # whatever is wrong with it, the app still starts, on the library as it is
        print('setup: the pending switch was not carried out:', type(e).__name__, e, flush=True)
        try:
            pend.unlink()
        except OSError:
            pass
        return None, None


def opened(settings_dir, data, note):
    """The library opened. Record the old one for deleting later, mark the model checked, and clear the work folder."""
    settings_dir = Path(settings_dir)
    try:
        s = json.loads((settings_dir / 'setup.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        s = {}
    work = Path(s['work']) if s.get('work') else None
    if note and note.get('model_file') and note.get('model_sha256') and os.path.isfile(note['model_file']):
        try:
            import llm_local      # imported here, after LASTBOOK_DATA names the library: its state file lives there
            llm_local.mark_checked(note['model_file'], note['model_sha256'])
        except Exception as e:
            print('setup: mark_checked:', type(e).__name__, e, flush=True)
    if note and note.get('old') and note['old'] not in s.get('old', []):
        s.setdefault('old', []).append(note['old'])
    if work:
        install.confirm(_pending_file(settings_dir))
        try:
            job = json.loads(_job_file(settings_dir).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            job = None
        if note or (isinstance(job, dict) and job.get('state') == 'done'):
            shutil.rmtree(work, ignore_errors=True)
            s.pop('work', None)
            try:
                _job_file(settings_dir).unlink()
            except OSError:
                pass
    s['old'] = [o for o in s.get('old', []) if Path(o).exists()]
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        install._write_json(settings_dir / 'setup.json', s)
    except OSError:
        pass


SETUP = None


def configure(**kw):
    global SETUP
    SETUP = Setup(**kw)
    return SETUP


def default_settings():
    """The app's own settings folder, where setup keeps its job and a staged switch. LASTBOOK_SETTINGS names another."""
    if os.environ.get('LASTBOOK_SETTINGS'):
        return Path(os.environ['LASTBOOK_SETTINGS'])
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'Lastbook'
    if sys.platform.startswith('linux'):
        return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'Lastbook'
    return Path.home() / 'Library' / 'Application Support' / 'Lastbook'


def _default():
    global SETUP
    if SETUP is None:
        from engine import paths
        SETUP = Setup('manage', default_settings(), paths.DATA, current=paths.DATA)
    return SETUP


ROUTES = {
    ('GET', 'state'): lambda s, b: s.state(),
    ('GET', 'job'): lambda s, b: s.public_job(),
    ('POST', 'start'): lambda s, b: s.start(b.get('pack'), bool(b.get('ai')), b.get('folder'), repair=bool(b.get('repair')), map_id=b.get('map') or None,
                                            address_id=b.get('address_id') or None, map_ids=b.get('maps') if 'maps' in b else None,
                                            custom_selection=b.get('custom') if 'custom' in b else None),
    ('GET', 'custom'): lambda s, b: s.custom_state(),
    ('POST', 'custom-load'): lambda s, b: s.custom_load(),
    ('POST', 'custom-items'): lambda s, b: s.custom_items(b.get('group')),
    ('POST', 'pause'): lambda s, b: s.pause(),
    ('POST', 'resume'): lambda s, b: s.resume(),
    ('POST', 'cancel'): lambda s, b: s.cancel(),
    ('POST', 'retry'): lambda s, b: s.retry(),
    ('POST', 'use'): lambda s, b: s.use(b.get('path')),
    ('POST', 'finish'): lambda s, b: s.finish(),
    ('POST', 'pick'): lambda s, b: s.pick(files=b.get('files') is True),
    ('POST', 'delete-old'): lambda s, b: s.delete_old(b.get('path')),
    ('POST', 'remove-ai'): lambda s, b: s.remove_ai(),
}


def _send_json(h, obj, status=200):
    data = json.dumps(obj, separators=(',', ':')).encode()
    h.send_response(status)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Content-Length', str(len(data)))
    h.send_header('Cache-Control', 'no-store')
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(data)


REMOTE_REFUSED = {'use', 'pick', 'remove-ai'}    # they name or open files and folders on the server, or are about the local AI


def handle(h, method, path):
    """Answers one /api/setup/ request on a BaseHTTPRequestHandler h."""
    local = h.client_address[0] in ('127.0.0.1', '::1', '::ffff:127.0.0.1')
    if not local and getattr(h, 'remote_setup_ok', False) is not True:
        return _send_json(h, {'error': 'Setup is only open on this computer.'}, 403)
    name = path[len('/api/setup/'):].strip('/')
    fn = ROUTES.get(('GET' if method == 'HEAD' else method, name))
    if not fn:
        return _send_json(h, {'error': 'not found'}, 404)
    if not local and name in REMOTE_REFUSED:
        return _send_json(h, {'error': 'This is done on the server itself, not from a browser.', 'code': 'remote'}, 403)
    body = {}
    if method == 'POST':
        try:
            n = int(h.headers.get('Content-Length') or 0)
            if n < 0 or n > MAX_BODY:
                return _send_json(h, {'error': 'request too large'}, 413)
            body = json.loads(h.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _send_json(h, {'error': 'bad request'}, 400)
    if not local:
        body.pop('folder', None)     # always the server's own data folder
        body['ai'] = False
    try:
        out = fn(SETUP or _default(), body)
    except Exception as e:
        traceback.print_exc()
        return _send_json(h, {'error': f'Setup stopped on an internal error ({type(e).__name__}).'}, 500)
    status = 200
    if isinstance(out, dict) and out.get('error') and 'code' in out:
        status = 409 if out['code'] in ('busy', 'not_done') else 400 if out['code'] != 'no_space' else 507
    return _send_json(h, out, status)


# ------------------------------------------------------------------------------------------------ setup mode's Handler --
def web_dir():
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / 'web'
    return Path(__file__).resolve().parents[2] / 'web'


TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml',
         '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.woff2': 'font/woff2'}


class SetupHandler(BaseHTTPRequestHandler):
    """All there is in setup mode: the setup page, the app's web files, and /api/setup/*."""
    server_version = 'Lastbook'
    sys_version = ''
    NO_CODE_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; worker-src 'none'; object-src 'none'; manifest-src 'none'; "
                   "base-uri 'none'; form-action 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:")
    SAVED_PAGE_CSP = 'sandbox; ' + NO_CODE_CSP

    def log_message(self, *a):
        pass

    def _path(self):
        import urllib.parse
        return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

    def _static(self, p):
        try:
            data = p.read_bytes()
        except OSError:
            return _send_json(self, {'error': 'not found'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', TYPES.get(p.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_GET(self):
        path = self._path()
        if '\0' in path or '/..' in path or '\\' in path:
            return _send_json(self, {'error': 'not found'}, 404)
        web = web_dir()
        if path in ('/', '/index.html', '/ui/setup.html'):
            return self._static(web / 'setup.html')
        if path.startswith('/ui/'):
            p = (web / path[4:]).resolve()
            if web.resolve() in p.parents and p.is_file():
                return self._static(p)
            return _send_json(self, {'error': 'not found'}, 404)
        if path.startswith('/api/setup/'):
            return handle(self, self.command, path)
        return _send_json(self, {'error': 'not found'}, 404)

    do_HEAD = do_GET

    def do_POST(self):
        path = self._path()
        if path.startswith('/api/setup/'):
            return handle(self, 'POST', path)
        return _send_json(self, {'error': 'not found'}, 404)


class SetupServer:
    """What app/bridge.py needs of an engine module: its Handler."""
    Handler = SetupHandler
