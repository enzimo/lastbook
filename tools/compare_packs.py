#!/usr/bin/env python3
"""Compare an installed library's file list with a new release's: which files would be added, changed, removed or kept.

  python3 tools/compare_packs.py OLD/pack-manifest.tsv NEW/pack-manifest.tsv [--list]
  python3 tools/compare_packs.py --data /path/to/LastbookData NEW/pack-manifest.tsv [--list]

With two lists, only they are compared (path, size and SHA-256 of each file). With --data, the installed list is that
library's own pack-manifest.tsv, and the plan also lists what an update would keep as it is: a map installed on its own,
the local AI, address indexes and any other file neither list names (install.plan_update). Then the names and sizes of
the library's files are read, but no file is opened. Nothing is downloaded, written or deleted, and no port is used.
This is a step toward upgrades that fetch only what changed (issue #144); setup does not use it yet.
"""
import argparse, sys
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from engine.setup import install  # noqa: E402
KEPT = install.KEPT_KINDS


def files(n):
    return f'{n} file' if n == 1 else f'{n} files'


def size(n):
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if n < 1000 or unit == 'GB':
            return f'{n} bytes' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1000


def main(args):
    ap = argparse.ArgumentParser(description="Compare an installed library's pack-manifest.tsv with a new release's.")
    ap.add_argument('--data', help='the installed library, the folder that holds pack-manifest.tsv (instead of OLD)')
    ap.add_argument('--list', action='store_true', help='also list each path, under its heading')
    ap.add_argument('manifests', nargs='+', type=Path, help='OLD and NEW pack-manifest.tsv, or only NEW with --data')
    a = ap.parse_args(args)
    if len(a.manifests) != (1 if a.data else 2):
        ap.error('give the installed and the new pack-manifest.tsv, or --data and the new one')
    try:
        if a.data:
            d = install.plan_update(Path(a.data), install.read_pack_manifest(a.manifests[0]))
        else:
            d = install.compare_pack_manifests(*(install.read_pack_manifest(p) for p in a.manifests))
    except OSError as e:
        sys.exit(f'Cannot read {e.filename}: {e.strerror}.')
    except ValueError as e:
        sys.exit(f'Cannot use this list: {e}.')
    for k, what in (('added', 'new in this release'), ('changed', 'differ in size or SHA-256'),
                    ('unchanged', 'already here, the same in the release'), ('removed', 'no longer in the release')):
        print(f'{k:<10} {files(len(d[k])):>12}  {size(d["bytes"][k]):>12}  {what}')
    if d['unsafe']:
        print(f'{"unsafe":<10} {files(len(d["unsafe"])):>12}  {"":>12}  paths outside the library, never used')
    kept = d.get('kept')
    if kept:
        print('\nKept as they are, not part of the pack:')
        for k, what in (('map', f'your own map ({d["map"]})' if d.get('map') else 'your own map'), ('model', 'the local AI'),
                        ('addresses', 'address search'), ('other', 'other files in the library')):
            if kept[k]:
                print(f'{k:<10} {files(len(kept[k])):>12}  {size(d["kept_bytes"][k]):>12}  {what}')
    print(f'\nTo fetch: {size(d["fetch_bytes"])} (added and changed). Already here: {size(d["reuse_bytes"])}.')
    if a.list:
        for k in ('added', 'changed', 'removed', 'unsafe', 'unchanged'):
            if d[k]:
                print(f'\n{k}:')
                for rel in d[k]:
                    print('  ' + rel)
        for k in KEPT if kept else ():
            if kept[k]:
                print(f'\nkept ({k}):')
                for rel, _ in kept[k]:
                    print('  ' + rel)


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='backslashreplace')     # a console or file that cannot show a path's letters must not stop the list
    main(sys.argv[1:])
