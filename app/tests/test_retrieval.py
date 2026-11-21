"""Meaning-based search (retrieval.Index.semantic): per-document filtering. Small made-up index, no real library, no model.
Never packaged."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import fixture  # noqa: E402
os.environ.setdefault('LASTBOOK_DATA', str(fixture.ensure()))
os.environ.setdefault('SURVIVAL_MODEL_BASE', 'http://127.0.0.1:9')
os.environ.setdefault('SURVIVAL_DISABLE_INDEX_WATCH', '1')
import numpy as np  # noqa: E402
import retrieval  # noqa: E402

TEXT = 'Boil water for one full minute before drinking it, then let it cool and store it in a clean covered container.'


def vec(*xs):
    v = np.zeros(retrieval.DIM, dtype=np.float32)
    v[:len(xs)] = xs
    return v


class FakeEmb:
    def embed(self, texts):
        return np.array([vec(1.0)] * len(texts))


def make_index(tmp, rows):
    """rows: [(doc_id, vector)]. Writes the chunk table and vectors, returns an Index with a fake embedder."""
    tmp = Path(tmp)
    with mock.patch.object(retrieval, 'IDX', tmp), mock.patch.object(retrieval, 'AVAILABLE', True), mock.patch.object(retrieval, 'Embedder', FakeEmb):
        idx = retrieval.Index(lambda: {}, lambda r: ('', 1), tmp / 'fts.sqlite')
        db = idx._db()
        for i, (d, v) in enumerate(rows, 1):
            db.execute('INSERT INTO chunk(id,doc_id,page,t,text,active) VALUES(?,?,?,?,?,1)', (i, d, 1, float(i), TEXT))
        db.close()
        np.array([v for _, v in rows], dtype=np.float16).tofile(idx.vec_path)
        idx.emb = FakeEmb()
        idx._load(truncate=False)
    return idx


def close_index(idx):
    """Close the read-only SQLite reader that _load() keeps per thread. Windows cannot delete the temp folder while it is open."""
    c = getattr(idx._tls, 'ro', None)
    if c is not None:
        c.close()
        idx._tls.ro = None


class Semantic(unittest.TestCase):
    def test_per_document_filter_applies_before_the_cut(self):
        rows = [('a', vec(1.0))] * 50 + [('b', vec(0.8, 0.6))]  # b scores 0.8: above the floor, far below fifty a's
        with tempfile.TemporaryDirectory() as tmp:
            idx = make_index(tmp, rows)
            try:
                got = idx.semantic('water', 1, lambda x: x == 'b')
            finally:
                close_index(idx)
        self.assertEqual([g['doc_id'] for g in got], ['b'])

    def test_per_document_filter_fetches_few_texts(self):
        rows = [('a', vec(1.0))] * 3000 + [('b', vec(0.8, 0.6))] * 3
        with tempfile.TemporaryDirectory() as tmp:
            idx = make_index(tmp, rows)
            try:
                fetched = []
                idx._ro().set_trace_callback(lambda q: fetched.append(q) if 'text FROM chunk WHERE id=' in q else None)
                got = idx.semantic('water', 2, lambda x: x == 'b')
            finally:
                close_index(idx)
        self.assertEqual(len(got), 2)
        self.assertLessEqual(len(fetched), 4)


if __name__ == '__main__':
    unittest.main()
