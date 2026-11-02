"""ADR-055: the iterative retriever knows a chunk by its (file, text), not its scope.

A tier-2/3 scope is a positional label (`Full File_part_1`) that every file shares.
Keyed on it, one file's whole-file chunk hid every other file's from later rounds, and
a round of only such chunks ended the loop as "no new chunks". These pin: other files'
whole-file chunks survive into later rounds, a round of them doesn't stop the loop, and
a text seen in one round is not added again. No model: the base retriever is a stub
that returns a fixed list per round.
"""
import hybrid_retriever as hr
from iterative_retriever import IterativeRetriever

T1, T2, T3 = hr._TIER1_NAME, hr._TIER2_NAME, hr._TIER3_NAME


def mk(fid, score, text, tier=T2, file="a.py", scope="Full File_part_1"):
    return hr.RetrievedChunk(faiss_id=fid, score=score, file=file, scope=scope,
                             tier=tier, text=text, source="semantic")


class _Rounds:
    """Returns the next round's hits on each call, then nothing."""

    def __init__(self, *rounds):
        self._rounds = list(rounds)
        self.calls = 0

    def retrieve(self, query):
        self.calls += 1
        return self._rounds.pop(0) if self._rounds else []


def _run(*rounds, **kw):
    base = _Rounds(*rounds)
    kw.setdefault("confidence_threshold", 2.0)    # never plateau: run every round
    chunks, session = IterativeRetriever(base, db=None).retrieve("q", **kw)
    return chunks, session, base


def test_other_files_whole_file_chunks_survive_into_later_rounds():
    chunks, _, _ = _run(
        [mk(1, 0.9, "a body", file="a.py")],
        [mk(2, 0.8, "b body", file="b.py"), mk(3, 0.7, "c body", file="c.py")])
    assert [c.file for c in chunks] == ["a.py", "b.py", "c.py"]


def test_a_round_of_only_other_files_whole_file_chunks_does_not_stop_the_loop():
    _, session, base = _run(
        [mk(1, 0.9, "a body", file="a.py")],
        [mk(2, 0.8, "b body", file="b.py")],
        [mk(3, 0.7, "c body", file="c.py", tier=T3)])
    assert base.calls == 3 and session.iteration == 3


def test_a_text_seen_in_one_round_is_not_added_again():
    chunks, _, _ = _run(
        [mk(1, 0.9, "same body", tier=T2)],
        [mk(2, 0.95, "same body", tier=T3), mk(3, 0.5, "other", file="b.py")])
    assert [(c.faiss_id, c.file) for c in chunks] == [(1, "a.py"), (3, "b.py")]


def test_symbols_with_their_own_scopes_are_unaffected():
    chunks, _, _ = _run(
        [mk(1, 0.9, "def f(): ...", tier=T1, scope="a.py::f")],
        [mk(1, 0.9, "def f(): ...", tier=T1, scope="a.py::f"),
         mk(2, 0.8, "def g(): ...", tier=T1, scope="a.py::g")])
    assert [c.scope for c in chunks] == ["a.py::f", "a.py::g"]


def test_empty_text_falls_back_to_the_chunk_id():
    chunks, _, _ = _run(
        [mk(1, 0.9, "", file="a.py")],
        [mk(2, 0.8, "", file="a.py"), mk(1, 0.7, "", file="a.py")])
    assert sorted(c.faiss_id for c in chunks) == [1, 2]
