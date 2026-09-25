"""B-028 — symbols that share a scope must not leave ghost vectors.

A getter/setter pair (or an overload set) gets one scope, hence one stable id, and
SQLite keeps one row for it. Ingest must add exactly one FAISS vector per chunk row.
The embedder is stubbed, so this never loads a model.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import faiss
import numpy as np

import core
from core import DocumentStore
from db import CodeDB
from incremental_indexer import dedupe_chunks_by_scope, ingest_file
from stable_id import TIER_CONFIGS, TIER_NUM

_DIM = 8

_ACCESSOR_PAIR = """\
export class Queue {
    #concurrency = 1;

    get concurrency(): number {
        return this.#concurrency;
    }

    set concurrency(value: number) {
        this.#concurrency = value;
    }
}
"""


def _fake_embed(texts, batch_size=32):
    rng = np.random.default_rng(len(texts))
    return rng.standard_normal((len(texts), _DIM)).astype(np.float32)


def test_dedupe_keeps_the_last_chunk_per_scope():
    chunks = [SimpleNamespace(scope=s, text=t) for s, t in
              [("A", "a1"), ("B", "b"), ("A", "a2"), ("C", "c")]]
    kept = dedupe_chunks_by_scope(chunks)
    assert [(c.scope, c.text) for c in kept] == [("B", "b"), ("A", "a2"), ("C", "c")]


def test_ingest_adds_one_vector_per_chunk_row(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    db_path = os.path.join(str(tmp_path), "graph.db")
    db = CodeDB(db_path)
    indexes = {name: faiss.IndexIDMap(faiss.IndexFlatIP(_DIM)) for name, _, _ in TIER_CONFIGS}

    ingest_file("src/queue.ts", _ACCESSOR_PAIR, "h1", indexes, DocumentStore(db_path), db)

    rows = dict(db._conn.execute("SELECT tier, COUNT(*) FROM chunks GROUP BY tier").fetchall())
    scopes = [s for (s,) in db._conn.execute("SELECT scope FROM chunks WHERE tier = 1")]
    db.close()
    assert "src/queue.ts::Queue.concurrency" in scopes, scopes
    for name, idx in indexes.items():
        assert idx.ntotal == rows.get(TIER_NUM[name], 0), name
