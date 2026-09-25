"""ADR-034 §2: `#` members and function-valued fields get symbols, chunks and call edges."""
from __future__ import annotations

import os

import faiss
import numpy as np

import core
from ast_chunker import parse_file
from call_resolver import resolve_call_edges
from core import DocumentStore
from db import CodeDB
from incremental_indexer import ingest_file
from stable_id import TIER_CONFIGS

TS = """\
export class Q {
  #count = 0;

  /** Starts the next job. */
  #next(): void { this.#count++; }

  run(): void { this.#next(); }

  get #busy(): boolean { return this.#count > 0; }

  private work = () => { this.#next(); };

  static make = function (): Q { return new Q(); };

  limit = 3;
}
"""


def _syms():
    return {s.fqn: s for s in parse_file("q.ts", TS).symbols}


def test_private_methods_and_accessors_are_symbols_with_the_hash_kept():
    syms = _syms()
    assert syms["q.ts::Q.#next"].kind == "method"
    assert syms["q.ts::Q.#busy"].kind == "method"
    assert syms["q.ts::Q.#next"].text.endswith("/** Starts the next job. */")


def test_function_valued_fields_are_symbols_and_plain_fields_are_not():
    syms = _syms()
    assert syms["q.ts::Q.work"].kind == "arrow_function"
    assert syms["q.ts::Q.make"].kind == "arrow_function"
    assert "q.ts::Q.#count" not in syms and "q.ts::Q.limit" not in syms


def test_skeleton_stubs_function_fields_and_keeps_plain_fields():
    skel = _syms()["q.ts::Q"].text
    assert "this.#next(); };" not in skel
    assert "private work = () =>  ..." in skel
    assert "limit = 3;" in skel and "#count = 0;" in skel


def test_private_calls_produce_call_edges():
    edges = {(e.source_fqn, e.target) for e in parse_file("q.ts", TS).edges if e.kind == "call"}
    assert ("q.ts::Q.run", "#next") in edges
    assert ("q.ts::Q.work", "#next") in edges


def _fake_embed(texts, batch_size=32):
    rng = np.random.default_rng(len(texts))
    return rng.standard_normal((len(texts), 8)).astype(np.float32)


def test_private_method_has_resolved_callers(tmp_path, monkeypatch):
    """find_dead_code skips the defining file's text, so a `#` member stays alive only
    through resolved call edges."""
    monkeypatch.setattr(core, "embed_batch", _fake_embed)
    db_path = os.path.join(str(tmp_path), "graph.db")
    db = CodeDB(db_path)
    indexes = {name: faiss.IndexIDMap(faiss.IndexFlatIP(8)) for name, _, _ in TIER_CONFIGS}
    ingest_file("src/q.ts", TS, "h1", indexes, DocumentStore(db_path), db)
    resolve_call_edges(db)
    callers = {n.fqn for n in db.get_callers("src/q.ts::Q.#next")}
    db.close()
    assert {"src/q.ts::Q.run", "src/q.ts::Q.work"} <= callers, callers
