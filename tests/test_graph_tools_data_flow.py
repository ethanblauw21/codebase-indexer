"""ADR-053 §2–§4 (B-042): trace_data_flow's per-language rules, and both graph tools'
failure when there is no index.

`trace_data_flow` classifies ranked search results, which need the embedder. Here the
search is faked: `_search` returns the fixture's chunks in a fixed order, and the bound
index state holds the same chunks as its doc store. Everything after retrieval — the
Firestore pre-pass, the rules, the buckets, the sweeps — is the real code.
"""
import os
import sys
from types import SimpleNamespace

import anyio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import CodeDB  # noqa: E402
from hybrid_retriever import RetrievedChunk  # noqa: E402
import MCPServer as M  # noqa: E402

T1, T2 = "tier1_surgical", "tier2_component"


def _wire(monkeypatch, tmp_path, chunks, ranked=None):
    """Serve ``chunks`` ((tier, file, scope, text)) as the index; ``ranked`` (indexes into
    chunks) is what the fake search returns, default all of them in order."""
    import incremental_indexer
    db = CodeDB(str(tmp_path / "graph.db"))
    db.meta_set("last_verified_at", "2026-10-20T00:00:00Z")
    db.close()
    monkeypatch.setattr(incremental_indexer, "INDEX_DIR", str(tmp_path))

    docs = {str(i): {"tier": t, "file": f, "scope": s, "text": x, "tags": []}
            for i, (t, f, s, x) in enumerate(chunks)}
    state = SimpleNamespace(doc_store=SimpleNamespace(docs=docs))
    monkeypatch.setattr(M, "_ensure_indexes", lambda: state)
    order = range(len(chunks)) if ranked is None else ranked
    hits = [RetrievedChunk(faiss_id=i, score=1.0, file=chunks[i][1], scope=chunks[i][2],
                           tier=chunks[i][0], text=chunks[i][3], source="semantic")
            for i in order]
    monkeypatch.setattr(M, "_search", lambda query, top_n=10: hits)


def _buckets(out: str) -> dict[str, str]:
    """Each ### section of the report, by name."""
    parts = {}
    for block in out.split("### ")[1:]:
        name, _, body = block.partition("\n")
        parts[name.strip()] = body
    return parts


# ------------------------------------------------------------------- Python / SQL

PY_CHUNKS = [
    (T1, "src/db.py", "src/db.py::CodeDB.get_cached_summaries",
     "def get_cached_summaries(self, hashes):\n"
     "    rows = self._conn.execute(\n"
     "        f\"SELECT text_hash, summary FROM chunk_summaries WHERE text_hash IN ({ph})\",\n"
     "        hashes).fetchall()\n"
     "    return dict(rows)\n"),
    (T1, "src/db.py", "src/db.py::CodeDB.cache_summaries",
     "def cache_summaries(self, items):\n"
     "    with self._tx() as cur:\n"
     "        cur.executemany(\n"
     "            \"INSERT OR IGNORE INTO chunk_summaries(text_hash, summary) VALUES (?, ?)\",\n"
     "            items)\n"),
    # The schema lives in a module-level string: a tier-2 window, which the ranked loop
    # skips once db.py's tier-1 chunks have been seen.
    (T2, "src/db.py", "src/db.py::anonymous_part_1",
     "_SCHEMA = \"\"\"\n"
     "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY);\n"
     "CREATE TABLE IF NOT EXISTS chunk_summaries (\n"
     "    text_hash TEXT PRIMARY KEY,\n"
     "    summary   TEXT NOT NULL\n"
     ");\n\"\"\"\n"),
    (T1, "src/summarizer.py", "src/summarizer.py::summarize_pending",
     "def summarize_pending(db):\n"
     "    \"\"\"Update chunk_summaries for every chunk that has none.\"\"\"\n"
     "    cached = db.get_cached_summaries(keys)  # reads chunk_summaries\n"
     "    return [k for k in keys if k not in cached]\n"),
    (T1, "src/cache.py", "src/cache.py::Cache.remember",
     "def remember(self, key, value):\n"
     "    self.chunk_summaries[key] = value\n"),
    (T1, "src/cache.py", "src/cache.py::Cache.forget_all",
     "def forget_all(self):\n"
     "    self.chunk_summaries.clear()\n"),
    (T1, "src/cache.py", "src/cache.py::Cache.is_empty",
     "def is_empty(self):\n"
     "    return self.chunk_summaries == {}\n"),
]


def test_chunk_summaries_finds_its_definition_and_its_producer(monkeypatch, tmp_path):
    # The search ranks the writer below the reader and never returns the schema window first.
    _wire(monkeypatch, tmp_path, PY_CHUNKS, ranked=[0, 2, 3, 1, 4, 5, 6])
    b = _buckets(M.trace_data_flow("chunk_summaries"))

    assert "CREATE TABLE IF NOT EXISTS chunk_summaries" in b["DEFINITIONS"]
    assert "CodeDB.cache_summaries" in b["PRODUCERS"]
    assert "CodeDB.cache_summaries" not in b["CONSUMERS"]
    assert "CodeDB.get_cached_summaries" in b["CONSUMERS"]


def test_attribute_subscript_and_mutating_calls_are_producers(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, PY_CHUNKS)
    b = _buckets(M.trace_data_flow("chunk_summaries"))

    assert "Cache.remember" in b["PRODUCERS"]          # self.chunk_summaries[key] = value
    assert "Cache.forget_all" in b["PRODUCERS"]        # self.chunk_summaries.clear()
    assert "Cache.is_empty" in b["CONSUMERS"]          # == is a comparison, not a write


def test_a_docstring_saying_update_is_not_a_writer(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, PY_CHUNKS)
    b = _buckets(M.trace_data_flow("chunk_summaries"))
    assert "summarize_pending" in b["CONSUMERS"]
    assert "summarize_pending" not in b["PRODUCERS"]


def test_the_producer_sweep_finds_a_writer_search_did_not_return(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, PY_CHUNKS, ranked=[0])
    b = _buckets(M.trace_data_flow("chunk_summaries"))
    assert "CodeDB.cache_summaries" in b["PRODUCERS"]
    assert "CREATE TABLE IF NOT EXISTS chunk_summaries" in b["DEFINITIONS"]


def test_python_classes_and_dataclasses_are_definitions(monkeypatch, tmp_path):
    chunks = [
        (T1, "src/model.py", "src/model.py::Order",
         "@dataclass(frozen=True)\nclass Order:\n    id: int\n"),
        (T1, "src/shop.py", "src/shop.py::place",
         "def place(order: Order):\n    return order.id\n"),
    ]
    _wire(monkeypatch, tmp_path, chunks)
    b = _buckets(M.trace_data_flow("Order"))
    assert "src/model.py::Order" in b["DEFINITIONS"]
    assert "src/shop.py::place" in b["CONSUMERS"]


# --------------------------------------------------------------- Firestore / Next.js

FIRESTORE_CHUNKS = [
    (T1, "lib/types/inventory.ts", "lib/types/inventory.ts::AggregatedInventory",
     "export interface AggregatedInventory {\n  sku: string;\n  onHand: number;\n}\n"),
    (T1, "functions/src/aggregate.ts", "functions/src/aggregate.ts::rebuild",
     "export async function rebuild() {\n"
     "  const aggregatedInventory = await compute();\n"
     "  await aggRef.set({ aggregatedInventory });\n}\n"),
    (T1, "app/inventory/page.tsx", "app/inventory/page.tsx::Page",
     "export default async function Page() {\n"
     "  const aggregatedInventory = await getAggregatedInventory();\n"
     "  return <Table rows={aggregatedInventory} />;\n}\n"),
    (T1, "components/InventoryTable.tsx", "components/InventoryTable.tsx::InventoryTable",
     "'use client'\nexport function InventoryTable({ aggregatedInventory }) {\n"
     "  const rows = aggregatedInventory.filter(r => r.onHand > 0);\n"
     "  return rows;\n}\n"),
    (T1, "components/EditStock.tsx", "components/EditStock.tsx::EditStock",
     "'use client'\nexport function EditStock({ aggregatedInventory, form }) {\n"
     "  form.setValue('sku', aggregatedInventory.sku);\n"
     "  docRef.update({ aggregatedInventory });\n}\n"),
    (T1, "lib/admin/inventory.ts", "lib/admin/inventory.ts::getAggregatedInventory",
     "export async function getAggregatedInventory() {\n"
     "  const snap = await db.collection('aggregatedInventory').get();\n"
     "  return snap.data();\n}\n"),
]

# trace_data_flow("aggregatedInventory") on the fixture above, from the code at 871b55c
# (before ADR-053), captured by running that commit's MCPServer on it. ADR-053 must not
# change a character of it.
FIRESTORE_GOLDEN = """--- DATA FLOW TRACE: aggregatedInventory ---

### DEFINITIONS
- [CORE LOGIC / LIB] lib/types/inventory.ts
  [Scope]: lib/types/inventory.ts::AggregatedInventory
  [Snippet]: export interface AggregatedInventory {
  sku: string;
  onHand: number;
}...


### PRODUCERS
- [CLOUD FUNCTION] functions/src/aggregate.ts
  [Scope]: functions/src/aggregate.ts::rebuild
  [Snippet]: export async function rebuild() {   const aggregatedInventory = await compute();   await aggRef.set({ aggregatedInventory }); }...


### TRANSFORMERS
- [CLIENT COMPONENT (UI)] components/InventoryTable.tsx
  [Scope]: components/InventoryTable.tsx::InventoryTable
  [Snippet]: 'use client' export function InventoryTable({ aggregatedInventory }) {   const rows = aggregatedInventory.filter(r => r.onHand > 0);   retur...


### CONSUMERS
- [SERVER COMPONENT] app/inventory/page.tsx
  [Scope]: app/inventory/page.tsx::Page
  [Snippet]: export default async function Page() {   const aggregatedInventory = await getAggregatedInventory();   return <Table rows={aggregatedInvento...

- [CLIENT COMPONENT (UI)] components/EditStock.tsx
  [Scope]: components/EditStock.tsx::EditStock
  [⚠️ ARCHITECTURAL VIOLATION]: Client-side Firestore write detected.
  [Snippet]: 'use client' export function EditStock({ aggregatedInventory, form }) {   form.setValue('sku', aggregatedInventory.sku);   docRef.update({ a...

- [DATABASE (Admin SDK)] lib/admin/inventory.ts
  [Scope]: lib/admin/inventory.ts::getAggregatedInventory
  [Snippet]: export async function getAggregatedInventory() {   const snap = await db.collection('aggregatedInventory').get();   return snap.data(); }...


"""


def test_firestore_and_nextjs_output_is_unchanged(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, FIRESTORE_CHUNKS)
    assert M.trace_data_flow("aggregatedInventory") == FIRESTORE_GOLDEN


def test_a_nextjs_form_setvalue_is_not_an_apps_script_writer(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, FIRESTORE_CHUNKS)
    b = _buckets(M.trace_data_flow("aggregatedInventory"))
    assert "EditStock" not in b["PRODUCERS"]


def test_the_lib_definition_fallback_still_finds_an_unranked_type(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, FIRESTORE_CHUNKS, ranked=[2])
    b = _buckets(M.trace_data_flow("aggregatedInventory"))
    assert "[CORE LOGIC / LIB] lib/types/inventory.ts" in b["DEFINITIONS"]
    assert b["PRODUCERS"].strip() == "[No entries detected]"     # no rule sweep for Firestore


# --------------------------------------------------------------------- Apps Script

APPS_SCRIPT_CHUNKS = [
    (T1, "Code.js", "Code.js::recordSale",
     "function recordSale(row) {\n"
     "  SpreadsheetApp.getActive().getSheetByName('Sales').appendRow(row);\n}\n"),
    (T1, "Code.js", "Code.js::updateTotals",
     "function updateTotals(totals) {\n"
     "  const sheet = SpreadsheetApp.getActive().getSheetByName('Sales');\n"
     "  sheet.getRange(2, 2, totals.length, 1).setValues(totals);\n}\n"),
    (T1, "Code.js", "Code.js::markSynced",
     "function markSynced() {\n"
     "  PropertiesService.getScriptProperties().setProperty('Sales', new Date().toISOString());\n}\n"),
    (T1, "Code.js", "Code.js::readSales",
     "function readSales() {\n"
     "  return SpreadsheetApp.getActive().getSheetByName('Sales').getDataRange().getValues();\n}\n"),
    (T1, "Code.js", "Code.js::Sales",
     "class Sales {\n  constructor(rows) { this.rows = rows; }\n}\n"),
]


def test_apps_script_writers_are_producers(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, APPS_SCRIPT_CHUNKS)
    b = _buckets(M.trace_data_flow("Sales"))
    for writer in ("recordSale", "updateTotals", "markSynced"):
        assert f"Code.js::{writer}" in b["PRODUCERS"]
    assert "Code.js::readSales" in b["CONSUMERS"]
    assert "Code.js::Sales" in b["DEFINITIONS"]


# ---------------------------------------------------------------------------- C#

def test_csharp_ef_and_sql_writers_and_type_definitions(monkeypatch, tmp_path):
    chunks = [
        (T1, "Data/Order.cs", "Shop.Data.Order",
         "public record Order(int Id, decimal Total);\n"),
        (T1, "Data/OrderService.cs", "Shop.Data.OrderService.Place",
         "public async Task Place(Order order) {\n"
         "    _db.Order.Add(order);\n    await _db.SaveChangesAsync();\n}\n"),
        (T1, "Data/OrderRepo.cs", "Shop.Data.OrderRepo.Delete",
         "public Task Delete(int id) =>\n"
         "    _conn.ExecuteAsync(\"DELETE FROM Order WHERE Id = @id\", new { id });\n"),
        (T1, "Data/OrderRepo.cs", "Shop.Data.OrderRepo.Get",
         "public Task<Order> Get(int id) =>\n"
         "    _conn.QuerySingleAsync<Order>(\"SELECT * FROM Order WHERE Id = @id\", new { id });\n"),
    ]
    _wire(monkeypatch, tmp_path, chunks)
    b = _buckets(M.trace_data_flow("Order"))
    assert "Shop.Data.Order\n" in b["DEFINITIONS"]
    assert "OrderService.Place" in b["PRODUCERS"]
    assert "OrderRepo.Delete" in b["PRODUCERS"]
    assert "OrderRepo.Get" in b["CONSUMERS"]


def test_an_empty_name_is_refused(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, PY_CHUNKS)
    with pytest.raises(ValueError):
        M.trace_data_flow("  ")


# ------------------------------------------------------------- no index: isError

def _call(tool: str, args: dict):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def main():
        async with create_connected_server_and_client_session(M.mcp._mcp_server) as client:
            return await client.call_tool(tool, args)
    return anyio.run(main)


@pytest.mark.parametrize("tool,args", [
    ("trace_data_flow", {"target_symbol": "chunk_summaries"}),
    ("map_module_communities", {}),
])
def test_no_index_is_an_error_through_the_mcp_layer(tool, args, tmp_path, monkeypatch):
    import incremental_indexer
    monkeypatch.setattr(incremental_indexer, "INDEX_DIR", str(tmp_path))
    CodeDB(str(tmp_path / "graph.db")).close()        # what binding an empty index leaves
    state = SimpleNamespace(doc_store=SimpleNamespace(docs={}))
    monkeypatch.setattr(M, "_ensure_indexes", lambda: state)
    monkeypatch.setattr(M, "_search", lambda *a, **k: pytest.fail("searched an empty index"))

    result = _call(tool, args)

    assert result.isError is True
    assert "no index has been built" in result.content[0].text
