"""Chunk-shape and embedder study (2026-09-25), on master (ADR-034 + ADR-035 merged).

    python shape_study.py build <arm> [repo ...]   # cwd = a master checkout
    python shape_study.py eval  <arm> [repo ...]
    python shape_study.py table <base-arm> <arm> ...

An arm is "<embedder>.<shape>", e.g. "bge.base", "qwen3.base", "bge.outline".

Embedders (all bf16, max_seq_length 512, the chunk windows the indexer is built for):
  bge       BAAI/bge-code-v1, as shipped (core's own query instruction)
  qwen3     Qwen/Qwen3-Embedding-0.6B
  c2llm     codefuse-ai/C2LLM-0.5B
  jina15    jinaai/jina-code-embeddings-1.5b   (CC-BY-NC-4.0: noted, not a blocker for a study)
  coderank  nomic-ai/CodeRankEmbed
Chunk token counts always use bge-code-v1's tokenizer, so every embedder arm embeds the exact
same chunk texts, and every summary is a cache hit.

Shapes (tier 1 is always the shipped AST chunking; only the whole-file tiers change):
  base      tier 2 = 1,500-token slices, tier 3 = 4,000-token slices (shipped)
  outline   tier 2 = one outline per file (imports, leading comment, one line per symbol),
            paged at 480 tokens; tier 3 = none. Files with no symbols get 512-token slices.
  outlined  as outline, plus the first sentence of each symbol's own doc on its line
  outline3  tier 2 = outline; tier 3 = the shipped 4,000-token slices
  slice512  tier 2 = 512-token slices (64 overlap), which the embedder reads whole; tier 3 = none
  both512   tier 2 = outline; tier 3 = 512-token slices
  t2only    tier 2 = the shipped 1,500-token slices; tier 3 = none (is tier 3 redundant? B-010)
  t3only    tier 2 = none; tier 3 = the shipped 4,000-token slices
  +t1lean   combined with any of the above: tier-1 text without its Tags and Lines lines
  +t1s<N>   combined with any of the above: tier-1 AST chunks capped at N tokens (shipped 500,
            overlap max(50, N/10)); e.g. "bge.t1s300", "bge.t1s1000"

Summaries on (the shipped default, ADR-030 layout). The summary cache is seeded from a pool of
every summary any earlier build produced (telemetry/retrieval/summary_pool.db, keyed by chunk
text hash), and each build adds its new ones back, so only new texts are summarized.

Query sets: dev repos p-queue, zustand, click (orig 83 / intent 95 / file 40); held-out repos
lru-cache and bullmq (sym / intent / file), never used to tune anything. Grades as arm_gate.py:
MRR@10 over distinct (file, scope) results; the file set also "whole" (only a tier-2/3 chunk of
the gold file counts); gold rank to depth 50 for the lost/gained >= 3 counts.
"""
import json, os, random, sqlite3, sys, time, importlib.util

KIT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(KIT, "telemetry", "retrieval", "study")
POOL = os.path.join(KIT, "telemetry", "retrieval", "summary_pool.db")
CORPUS = os.path.join(os.path.dirname(KIT), "benchmarks", "real_repo", "corpus")
DEV = ["p-queue", "zustand", "click"]
HELD = ["lru-cache", "bullmq"]
SETS_DEV = {"orig": os.path.join(os.path.dirname(KIT), "benchmarks", "real_repo", "fixtures"),
            "intent": os.path.join(KIT, "intent_fixtures_all"),
            "file": os.path.join(KIT, "file_fixtures_all")}
SETS_HELD = {"orig": os.path.join(KIT, "heldout_fixtures", "sym"),
             "intent": os.path.join(KIT, "heldout_fixtures", "intent"),
             "file": os.path.join(KIT, "heldout_fixtures", "file")}
DEPTH, TOP = 50, 10
BGE = "BAAI/bge-code-v1"
CODE_INSTRUCT = "Given a code search query, retrieve relevant code that answers it"

EMBEDDERS = {
    "bge": dict(model_id=BGE, q=None, d=""),
    # ADR-035's open question: does fp32 retrieve better than bf16? Fits alone on the card
    # because every summary is a cache hit, so the summarizer never loads.
    # At core's batch of 32, fp32 activations for long chunks overflow 8 GB and WDDM pages
    # (click/core.py stalled at 22 W); batches of 4 fit. Batch size does not change vectors.
    "bge32": dict(model_id=BGE, q=None, d="", dtype="float32", batch=4),
    # Query-side variants of bge: documents are untouched, so they evaluate against the bge
    # index of the same shape ("reuse") and need no build. core wraps the query as
    # "<instruct>{instruct}\n<query>{query}"; an empty instruct sends the bare query.
    "bgeq0": dict(model_id=BGE, instruct="", reuse="bge"),
    "bgeqa": dict(model_id=BGE, reuse="bge",
                  instruct="Given a question about a code repository, retrieve the code that answers it"),
    "bgeqb": dict(model_id=BGE, reuse="bge",
                  instruct="Given a description of what code does, retrieve the code that does it"),
    "bgeqc": dict(model_id=BGE, reuse="bge",
                  instruct="Given a code search query, retrieve relevant code or files that answer it"),
    # The study loaded a local copy (the hub download needs symlink rights on Windows).
    "qwen3": dict(model_id="Qwen/Qwen3-Embedding-0.6B",
                  q=f"Instruct: {CODE_INSTRUCT}\nQuery:", d=""),
    "c2llm": dict(model_id="codefuse-ai/C2LLM-0.5B",
                  q="Retrieve the code that solves the following query:", d="Retrieved Answer:",
                  st=dict(tokenizer_kwargs={"padding_side": "left"})),
    "jina15": dict(model_id="jinaai/jina-code-embeddings-1.5b",
                   q="Find the most relevant code snippet given the following query:\n",
                   d="Candidate code snippet:\n"),
    "coderank": dict(model_id="nomic-ai/CodeRankEmbed",
                     q="Represent this query for searching relevant code: ", d=""),
}


# ─────────────────────────────────────────────────────────────────────────────
# Setup: embedder patch, before anything imports core.embed by name
# ─────────────────────────────────────────────────────────────────────────────

def setup(arm):
    emb_key, shape = arm.split(".", 1)
    E = EMBEDDERS[emb_key]
    os.environ["CODE_INDEXER_DEVICE"] = "cuda"
    os.environ.pop("CUDA_VISIBLE_DEVICES", None)
    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
    import torch
    torch.set_num_threads(2)
    import faiss
    faiss.omp_set_num_threads(2)
    import core
    # Chunk boundaries come from bge's tokenizer whatever the embedder, so texts match.
    core.jina_tokenizer._get()
    st = core.SentenceTransformer

    def loader(*args, **kwargs):
        kwargs.setdefault("model_kwargs", {})["torch_dtype"] = getattr(torch, E.get("dtype", "bfloat16"))
        kwargs.update(E.get("st", {}))
        return st(*args, **kwargs)

    core.SentenceTransformer = loader
    if emb_key == "c2llm":
        # Its modeling file imports deepspeed and peft inside try/except (training only), but
        # transformers' static import check refuses the file when they are missing.
        import transformers.dynamic_module_utils as dmu
        _check = dmu.check_imports

        def check_imports(filename):
            try:
                return _check(filename)
            except ImportError as exc:
                if all(m in ("deepspeed", "peft") for m in str(exc).split(": ")[1].split(".")[0].split(", ")):
                    return dmu.get_relative_imports(filename)
                raise
        dmu.check_imports = check_imports
        # modeling_c2llm.py:80 imports deepspeed's ZeRO checkpoint loader unconditionally. It is
        # for training checkpoints only; inference never calls it. DeepSpeed does not install on
        # Windows, so stand in a module that raises if it is ever used.
        import types

        def _zero(*_a, **_k):
            raise RuntimeError("deepspeed stub: ZeRO checkpoints are not supported here")
        for name in ("deepspeed", "deepspeed.utils", "deepspeed.utils.zero_to_fp32"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["deepspeed.utils.zero_to_fp32"].get_fp32_state_dict_from_zero_checkpoint = _zero
    if "batch" in E:
        _eb = core.embed_batch
        core.embed_batch = lambda texts, batch_size=32: _eb(texts, batch_size=E["batch"])
    if "instruct" in E:
        core._emb_cfg_cache = {**core._emb_cfg(), "query_instruct": E["instruct"]}
    elif E["model_id"] != BGE:
        from sentence_transformers import SentenceTransformer as _ST
        # Dimension from the model itself: load once on the GPU, read it, free it.
        cfg = {**core._emb_cfg(), "model_id": E["model_id"], "query_instruct": ""}
        core._emb_cfg_cache = cfg
        m = core._get_embed_model()
        cfg["dimension"] = m.get_sentence_embedding_dimension()
        print(f"[study] {E['model_id']}: dimension {cfg['dimension']}, "
              f"max_seq_length {m.max_seq_length}", flush=True)
        _embed, _embed_batch = core.embed, core.embed_batch
        core.embed = lambda text: _embed(E["q"] + text) if text and text.strip() else _embed(text)
        core.embed_batch = lambda texts, batch_size=32: _embed_batch([E["d"] + t for t in texts],
                                                                      batch_size=batch_size)
    import incremental_indexer as ii
    if shape != "base":
        ii.chunk_all_tiers = shaped_chunker(shape, ii)
    return emb_key, shape


# ─────────────────────────────────────────────────────────────────────────────
# Shapes
# ─────────────────────────────────────────────────────────────────────────────

_IMPORT_PREFIXES = ("import ", "from ", "export * from", "export {", "#include", "using ",
                    "const ", "require(")


def _signature(text: str) -> str:
    """The first line of a symbol that is code: not a comment, decorator or blank."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("//", "/*", "*", "#", "@", '"""', "'''")):
            continue
        return s[:160]
    return ""


def _doc_line(text: str) -> str:
    """The first sentence of a symbol's own doc: JSDoc (after the code, ADR-034), docstring or comment."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if s.startswith(('"""', "'''")):
            s = s.strip("\"'").strip() or nxt
        elif s.startswith("/**"):
            s = s[3:].rstrip("/").rstrip("*").strip() or nxt.lstrip("*").strip()
        elif s.startswith(("//", "# ")) and i > 0:
            s = s.lstrip("/#").strip()
        else:
            continue
        s = s.split(". ")[0].strip()
        if s and not s.startswith("@"):
            return s[:110]
    return ""


def _leading_comment(content: str, limit: int = 6) -> list[str]:
    out = []
    for line in content.splitlines():
        s = line.strip()
        if not s and not out:
            continue
        if s.startswith(("//", "/*", "*", "#!", '"""', "'''")) or (out and out[-1].startswith('"""')):
            if s.startswith("#!"):
                continue
            out.append(s)
            if len(out) >= limit:
                break
        else:
            break
    return out


def outline_chunks(rel_path, content, max_tokens=480, docs=False):
    from ast_chunker import parse_file, fallback_token_chunker, jina_tokenizer
    from adapters.base import Chunk
    res = parse_file(rel_path, content)
    if not res.symbols:
        return fallback_token_chunker(content, rel_path, 512, 64, parent_scope="Full File")
    imports = [l.strip() for l in content.splitlines()
               if l.startswith(_IMPORT_PREFIXES) and ("import" in l or "require" in l
                                                     or l.startswith(("#include", "using ")))][:40]
    body = []
    head = _leading_comment(content)
    if head:
        body.append("About:")
        body += head
    if imports:
        body.append("Imports:")
        body += imports
    body.append("Symbols:")
    for sym in sorted(res.symbols, key=lambda s: s.start_line):
        name = sym.fqn.split("::", 1)[-1]
        sig = _signature(sym.text)
        line = f"{sym.kind} {name}: {sig[:120]}" if sig else f"{sym.kind} {name}"
        doc = _doc_line(sym.text) if docs else ""
        body.append(f"{line} -- {doc}" if doc else line)
    pages, cur, cur_t = [], [], 0
    head_t = jina_tokenizer.count_tokens(f"File: {rel_path}\nScope: File Outline (Part 1/1)\n")
    for line in body:
        t = jina_tokenizer.count_tokens(line + "\n")
        if cur and cur_t + t > max_tokens - head_t:
            pages.append(cur); cur, cur_t = [], 0
        cur.append(line[:400]); cur_t += t
    if cur:
        pages.append(cur)
    return [Chunk(text=f"File: {rel_path}\nScope: File Outline (Part {i}/{len(pages)})\n" + "\n".join(p),
                  file=rel_path, start_line=0, end_line=0, scope=f"File Outline_part_{i}")
            for i, p in enumerate(pages, 1)]


def _lean_rich_text(sym, file_path, tags=None, sym_type=None):
    """t1lean: tier-1 text without the Tags and Lines header lines."""
    type_line = f"Type: {sym_type.return_type}\n" if (sym_type and sym_type.return_type) else ""
    return f"File: {file_path}\nEntity: {sym.fqn} ({sym.kind})\n{type_line}Code:\n{sym.text}"


def shaped_chunker(shape, ii):
    """shape: parts joined by '+'. One whole-file part (outline, outlined, outline3, slice512,
    both512, or base when none is given) and optionally t1lean for tier 1."""
    import ast_chunker
    from ast_chunker import chunk_file_ast, fallback_token_chunker
    parts = shape.split("+")
    if "t1lean" in parts:
        ast_chunker._symbol_rich_text = _lean_rich_text
    t1 = next((int(p[3:]) for p in parts if p.startswith("t1s")), 500)
    whole = next((p for p in parts if p != "t1lean" and not p.startswith("t1s")), "base")

    def chunk_all_tiers(rel_path, content):
        t = {"tier1_surgical": chunk_file_ast(rel_path, content, t1, max(50, t1 // 10))}
        slices = lambda n, o: fallback_token_chunker(content, rel_path, n, o, parent_scope="Full File")
        if whole == "base":
            t["tier2_component"], t["tier3_architectural"] = slices(1500, 100), slices(4000, 200)
        elif whole == "outline":
            t["tier2_component"], t["tier3_architectural"] = outline_chunks(rel_path, content), []
        elif whole == "outlined":
            t["tier2_component"], t["tier3_architectural"] = outline_chunks(rel_path, content, docs=True), []
        elif whole == "outline3":
            t["tier2_component"], t["tier3_architectural"] = outline_chunks(rel_path, content), slices(4000, 200)
        elif whole == "t2only":
            t["tier2_component"], t["tier3_architectural"] = slices(1500, 100), []
        elif whole == "t3only":
            t["tier2_component"], t["tier3_architectural"] = [], slices(4000, 200)
        elif whole == "slice512":
            t["tier2_component"], t["tier3_architectural"] = slices(512, 64), []
        elif whole == "both512":
            t["tier2_component"], t["tier3_architectural"] = outline_chunks(rel_path, content), slices(512, 64)
        else:
            raise ValueError(shape)
        for name, chunks in t.items():
            kept = ii.dedupe_chunks_by_scope(chunks)
            if len(kept) != len(chunks):
                t[name] = kept
        return t

    return chunk_all_tiers


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def pool_rows():
    if not os.path.exists(POOL):
        con = sqlite3.connect(POOL)
        con.execute("create table if not exists s (text_hash text primary key, summary text)")
        root = os.path.join(KIT, "telemetry", "retrieval")
        n = 0
        for dirpath, _dirs, files in os.walk(root):
            if "graph.db" in files and os.path.join("retrieval", "study") not in dirpath:
                try:
                    src = sqlite3.connect(os.path.join(dirpath, "graph.db"))
                    rows = src.execute("select text_hash, summary from chunk_summaries").fetchall()
                    src.close()
                except sqlite3.Error:
                    continue
                con.executemany("insert or ignore into s values (?, ?)", rows); n += 1
        con.commit(); con.close()
        print(f"[study] summary pool built from {n} indexes", flush=True)
    con = sqlite3.connect(POOL)
    rows = con.execute("select text_hash, summary from s").fetchall()
    con.close()
    return rows


def pool_add(db_path):
    src = sqlite3.connect(db_path)
    rows = src.execute("select text_hash, summary from chunk_summaries").fetchall()
    src.close()
    con = sqlite3.connect(POOL)
    before = con.execute("select count(*) from s").fetchone()[0]
    con.executemany("insert or ignore into s values (?, ?)", rows); con.commit()
    after = con.execute("select count(*) from s").fetchone()[0]
    con.close()
    return after - before


def build(arm, repos):
    if "reuse" in EMBEDDERS[arm.split(".", 1)[0]]:
        print(f"[study] {arm}: query-side variant, evaluates on the "
              f"{EMBEDDERS[arm.split('.', 1)[0]]['reuse']} index; nothing to build", flush=True)
        return
    setup(arm)
    import gc, torch, core, config
    import incremental_indexer as ii
    from call_resolver import resolve_call_edges
    from db import CodeDB
    config._sum_cfg_cache = {**config._sum_cfg(), "enabled": True}
    seed = pool_rows()
    for repo in repos:
        idx = os.path.join(OUT, arm, repo)
        if os.path.exists(os.path.join(idx, "DONE")):
            print(f"[study] {arm}/{repo}: already built", flush=True)
            continue
        os.makedirs(idx, exist_ok=True)
        core._embed_model = None
        gc.collect(); torch.cuda.empty_cache()
        ii.INDEX_DIR, ii.DB_PATH = idx, os.path.join(idx, "graph.db")
        with CodeDB(ii.DB_PATH) as db:
            db.cache_summaries(seed)
        t0 = time.time()
        ii.run_incremental(repo_path=os.path.join(CORPUS, repo))
        with CodeDB(ii.DB_PATH) as db:
            resolve_call_edges(db)
        con = sqlite3.connect(ii.DB_PATH)
        counts = {f"t{t}": con.execute("select count(*) from chunks where tier=?", (t,)).fetchone()[0]
                  for t in (1, 2, 3)}
        con.close()
        new = pool_add(ii.DB_PATH)
        s = round(time.time() - t0, 1)
        with open(os.path.join(idx, "DONE"), "w", encoding="utf-8") as fh:
            json.dump({"seconds": s, "new_summaries": new, **counts}, fh)
        print(f"[study] {arm}/{repo}: built in {s}s {counts}, {new} new summaries", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Eval
# ─────────────────────────────────────────────────────────────────────────────

def _load(path_dir, repo):
    p = os.path.join(path_dir, f"{repo}.jsonl")
    if not os.path.exists(p):
        return []
    rows = []
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("//"):
            rows.append(json.loads(line))
    return rows


def evaluate(arm, repos):
    setup(arm)
    spec = importlib.util.spec_from_file_location("ag", os.path.join(KIT, "arm_gate.py"))
    ag = importlib.util.module_from_spec(spec); spec.loader.exec_module(ag)
    fle, rre = ag.fle, ag.rre
    from hybrid_retriever import HybridRetriever
    path = os.path.join(OUT, arm, "eval.json")
    per = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    for repo in repos:
        sets = SETS_DEV if repo in DEV else SETS_HELD
        emb_key, shape = arm.split(".", 1)
        built = f"{EMBEDDERS[emb_key]['reuse']}.{shape}" if "reuse" in EMBEDDERS[emb_key] else arm
        idx = os.path.join(OUT, built, repo)
        os.makedirs(os.path.join(OUT, arm), exist_ok=True)
        assert os.path.exists(os.path.join(idx, "DONE")), f"{built}/{repo} not built"
        with HybridRetriever(index_dir=idx, db_path=os.path.join(idx, "graph.db"),
                             device="cuda", **rre.ARMS["B"]) as r:
            for set_name, d in sets.items():
                for f in _load(d, repo):
                    items = ag.all_distinct([(c.file, c.scope, c.tier)
                                             for c in r.retrieve(f["query"], top_n=DEPTH)])
                    g = fle.grades(set_name, items[:TOP], f["gold"])
                    if set_name == "file":
                        g["rank_any"] = ag.gold_rank(items, f["gold"], "file")
                        g["rank_whole"] = ag.gold_rank(items, f["gold"], "file", whole=True)
                    else:
                        g["rank"] = ag.gold_rank(items, f["gold"], set_name)
                    per.setdefault(set_name, {})[f"{repo}/{f['id']}"] = g
        print(f"[study] eval {arm}/{repo} done", flush=True)
    json.dump(per, open(path, "w", encoding="utf-8"), indent=1)


def _ci(d, n=5000):
    rng = random.Random(0)
    ms = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(n))
    return sum(d) / len(d), ms[int(.025 * n)], ms[int(.975 * n) - 1]


def table(base, arms):
    B = json.load(open(os.path.join(OUT, base, "eval.json"), encoding="utf-8"))
    cols = [("orig", "sym", "rank"), ("intent", "sym", "rank"), ("file", "any", "rank_any"),
            ("file", "whole", "rank_whole")]
    for group, repos in (("dev", DEV), ("held-out", HELD)):
        print(f"\n== {group} ({', '.join(repos)}) vs {base}")
        for arm in [base] + arms:
            p = os.path.join(OUT, arm, "eval.json")
            if not os.path.exists(p):
                continue
            C = json.load(open(p, encoding="utf-8"))
            cells = []
            for s, m, rk in cols:
                qs = [q for q in B.get(s, {}) if q.split("/")[0] in repos and q in C.get(s, {})]
                if not qs:
                    cells.append(f"{s}/{m}: -"); continue
                mc = sum(C[s][q][m] for q in qs) / len(qs)
                if arm == base:
                    cells.append(f"{s}/{m} {mc:.3f} (n={len(qs)})"); continue
                mean, lo, hi = _ci([C[s][q][m] - B[s][q][m] for q in qs])
                lost = sum(1 for q in qs if (C[s][q][rk] or DEPTH + 1) - (B[s][q][rk] or DEPTH + 1) >= 3)
                won = sum(1 for q in qs if (B[s][q][rk] or DEPTH + 1) - (C[s][q][rk] or DEPTH + 1) >= 3)
                star = "*" if lo > 0 or hi < 0 else " "
                cells.append(f"{s}/{m} {mc:.3f} {mean:+.3f}{star} [{lo:+.3f},{hi:+.3f}] +{won}/-{lost}")
            print(f"  {arm:18s} " + " | ".join(cells))


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "table":
        table(rest[0], rest[1:])
    else:
        arm, repos = rest[0], rest[1:] or DEV + HELD
        (build if cmd == "build" else evaluate)(arm, repos)
