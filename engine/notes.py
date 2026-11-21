"""Things I couldn't find (#116): notes the person chooses to keep when Lastbook had nothing for them, so they can later tell
the curators what was missing, if they want to. Nothing is kept unless the person asks for each note, nothing is sent anywhere,
and a note holds only the words they typed, the guide they came from (if any) and the date.

The notes live in the app's settings folder (setup's settings_dir: notes-not-found.json), not in the library, so a library update
or repair never touches them, and they never go into a pack, a copy for someone else or a scan. They are never printed to the log.

Routes, this computer only (like /api/setup/): GET /api/not-found; POST /api/not-found {words, guide};
POST /api/not-found/delete {id}; POST /api/not-found/delete-all.
"""
import json
import threading
import time
import uuid
from pathlib import Path

FILE_NAME = 'notes-not-found.json'
MAX_WORDS = 200        # characters: what someone types in a search box
MAX_NOTES = 500        # the oldest go first beyond this
MAX_BODY = 4096
_lock = threading.Lock()


def folder():
    """The settings folder setup uses: the app's (app/main.py app_folders), or the library server's default."""
    from engine.setup import api
    return Path((api.SETUP or api._default()).settings_dir)


def _file():
    return folder() / FILE_NAME


def load():
    try:
        notes = json.loads(_file().read_text(encoding='utf-8')).get('notes')
    except (OSError, ValueError, AttributeError):
        return []
    return [n for n in notes if isinstance(n, dict) and isinstance(n.get('id'), str) and isinstance(n.get('words'), str)] if isinstance(notes, list) else []


def _save(notes):
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps({'version': 1, 'notes': notes}, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(f)


def add(words, guide=None, guides=()):
    """Keeps one note. words: what was typed (text, trimmed, at most MAX_WORDS). guide: a guide id from guides, else none.
    Both come from a request body, so their types are checked before anything else: words that are not text are refused, and a
    guide that is not text (a list or an object, which could not even be looked up) counts as no guide, like an unknown one."""
    if words is not None and not isinstance(words, str):
        raise ValueError('There is nothing to keep: the words are not text.')
    words = ' '.join((words or '').split())[:MAX_WORDS]
    if not words:
        raise ValueError('There is nothing to keep: no words.')
    known = isinstance(guide, str) and guide in set(guides)
    note = {'id': uuid.uuid4().hex[:12], 'words': words, 'guide': guide if known else None, 'at': time.strftime('%Y-%m-%d')}
    with _lock:
        notes = load() + [note]
        _save(notes[-MAX_NOTES:])
    return note


def delete(note_id):
    with _lock:
        notes = load()
        kept = [n for n in notes if n['id'] != note_id]
        if len(kept) != len(notes):
            _save(kept)
        return len(notes) - len(kept)


def delete_all():
    with _lock:
        n = len(load())
        _save([])
        return n


def handle(h, method, path, guides=()):
    """Answers /api/not-found... on a BaseHTTPRequestHandler h. guides: the guide ids a note may name."""
    from engine.setup.api import _send_json
    if h.client_address[0] not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return _send_json(h, {'error': 'These notes are only open on this computer.'}, 403)
    if method in ('GET', 'HEAD') and path == '/api/not-found':
        return _send_json(h, {'notes': load()})
    if method != 'POST':
        return _send_json(h, {'error': 'not found'}, 404)
    try:
        n = int(h.headers.get('Content-Length') or 0)
        body = json.loads(h.rfile.read(n) if 0 < n <= MAX_BODY else b'{}')
    except (ValueError, TypeError):
        body = None
    if not isinstance(body, dict):
        return _send_json(h, {'error': 'bad request'}, 400)
    if path == '/api/not-found':
        try:
            return _send_json(h, {'note': add(body.get('words'), body.get('guide'), guides)})
        except ValueError as e:
            return _send_json(h, {'error': str(e)}, 400)
    if path == '/api/not-found/delete':
        return _send_json(h, {'deleted': delete(str(body.get('id') or ''))})
    if path == '/api/not-found/delete-all':
        return _send_json(h, {'deleted': delete_all()})
    return _send_json(h, {'error': 'not found'}, 404)
