"""ADR-027: adaptive summarizer batching, the memory pause, and worker job handling.

No model and no GPU. The batching loop takes the model call and the memory probe
as arguments, so these drive it with fakes that raise OOM at chosen batch sizes.
The worker-job tests swap the process pool for a fake executor.
"""
import os
import sys
from concurrent.futures import TimeoutError as FutureTimeout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config                                                    # noqa: E402
import summarizer as sm                                          # noqa: E402
from summarizer import BatchStats, run_adaptive_batches          # noqa: E402


class FakeOOM(Exception):
    pass


def is_fake_oom(exc):
    return isinstance(exc, FakeOOM)


class FakeModel:
    """Summarizes item i as 's<i>'. Raises FakeOOM for batches over `fits`."""

    def __init__(self, fits=99, oom_items=(), error_items=(), blank_items=()):
        self.fits = fits
        self.oom_items = set(oom_items)
        self.error_items = set(error_items)
        self.blank_items = set(blank_items)
        self.batches = []

    def __call__(self, idx):
        self.batches.append(list(idx))
        if len(idx) > self.fits or self.oom_items & set(idx):
            raise FakeOOM("CUDA out of memory")
        if self.error_items & set(idx):
            raise ValueError("generation blew up")
        return ["" if i in self.blank_items else f"s{i}" for i in idx]


def run(lengths, model, **kw):
    kw.setdefault("is_oom", is_fake_oom)
    kw.setdefault("max_batch", 16)
    return run_adaptive_batches(lengths, model, **kw)


# ---------------------------------------------------------------------------
# The batching loop
# ---------------------------------------------------------------------------

def test_results_come_back_in_the_callers_order():
    lengths = [5, 50, 1, 30, 7, 2]
    results, stats = run(lengths, FakeModel())
    assert results == [f"s{i}" for i in range(6)]
    assert stats.summarized == 6 and stats.empty == 0


def test_items_go_longest_first():
    model = FakeModel()
    run([5, 50, 1, 30, 7, 2], model, start_budget=2)
    flat = [i for batch in model.batches for i in batch]
    assert flat == [1, 3, 4, 0, 5, 2]


def test_oom_steps_back_then_halves_and_retries_the_same_items_without_losing_any():
    model = FakeModel(fits=2)
    results, stats = run([1] * 8, model, start_budget=8, max_batch=8)
    assert results == [f"s{i}" for i in range(8)]
    assert stats.empty == 0
    assert [len(b) for b in model.batches[:5]] == [8, 7, 6, 3, 1]   # two step-backs, then halving
    assert stats.oom_backoffs == 4
    assert model.batches[1] == model.batches[0][:7], "the retry must start with the same items"


def test_step_back_finds_the_ceiling_and_stays_under_it():
    """With room for more step-backs, the batch lands exactly on the largest size
    that fits, and later batches stay there instead of walking back into the wall."""
    model = FakeModel(fits=5)
    _results, stats = run([100] * 40, model, start_budget=800, max_batch=48, step_back_tries=10)
    sizes = [len(b) for b in model.batches]
    assert sizes[:4] == [8, 7, 6, 5]
    assert set(sizes[4:]) == {5}
    assert stats.oom_backoffs == 3


def test_the_budget_sizes_each_batch_by_its_longest_chunk():
    """One budget, many lengths: long chunks go in small batches, short ones in large."""
    lengths = [4000] * 4 + [1000] * 8 + [250] * 64
    model = FakeModel()
    run(lengths, model, start_budget=16000, max_batch=48)
    assert [len(b) for b in model.batches] == [4, 16, 48, 8]


def test_an_item_that_does_not_fit_alone_is_dropped_and_counted():
    model = FakeModel(oom_items={3})
    results, stats = run([1] * 6, model, start_budget=1)
    assert results[3] == ""
    assert [r for i, r in enumerate(results) if i != 3] == [f"s{i}" for i in (0, 1, 2, 4, 5)]
    assert stats.empty_oom == 1
    assert stats.summarized == 5


def test_batch_grows_back_after_a_clean_streak_and_stops_at_the_ceiling():
    model = FakeModel()
    _, stats = run([1] * 200, model, start_budget=1, grow_after=2, max_batch=5)
    sizes = [len(b) for b in model.batches]
    assert max(sizes) == 5
    assert sizes[:5] == [1, 1, 2, 2, 3]
    assert stats.largest_batch == 5


def test_max_batch_one_reproduces_one_chunk_at_a_time():
    model = FakeModel()
    run([3, 1, 2] * 10, model, max_batch=1, grow_after=1)
    assert all(len(b) == 1 for b in model.batches)


def test_a_non_oom_error_empties_that_batch_and_the_run_continues():
    model = FakeModel(error_items={0})
    results, stats = run([9, 1, 1, 1], model, start_budget=1)
    assert results[0] == ""
    assert results[1:] == ["s1", "s2", "s3"]
    assert stats.empty_error == 1 and stats.oom_backoffs == 0


def test_blank_model_output_is_counted_not_hidden():
    results, stats = run([1, 1, 1], FakeModel(blank_items={1}))
    assert results == ["s0", "", "s2"]
    assert stats.empty_output == 1 and stats.summarized == 2


def test_on_oom_hook_runs_for_every_backoff():
    calls = []
    run([1] * 8, FakeModel(fits=1), start_budget=8, max_batch=8, on_oom=lambda: calls.append(1))
    assert len(calls) == 4                            # 8 -> 7 -> 6 -> 3 -> 1


# ---------------------------------------------------------------------------
# Pausing for memory
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_low_memory_pauses_until_it_comes_back():
    clock = FakeClock()
    readings = iter([100, 100, 100, 5000])            # checked, still low, low, recovered

    def free_mb():
        return next(readings, 5000)

    results, stats = run([1, 1], FakeModel(), free_mb=free_mb, reserve_mb=1024,
                         sleep=clock.sleep, clock=clock, pause_poll_s=5)
    assert results == ["s0", "s1"]
    assert stats.pauses == 1 and stats.pause_timeouts == 0
    assert clock.t == 10


def test_a_pause_that_never_recovers_times_out_and_drops_to_batch_one():
    clock = FakeClock()
    model = FakeModel()
    results, stats = run([1] * 6, model, free_mb=lambda: 10, reserve_mb=1024,
                         sleep=clock.sleep, clock=clock, pause_poll_s=5, pause_timeout_s=20,
                         start_budget=4)
    assert results == [f"s{i}" for i in range(6)]
    assert stats.pause_timeouts >= 1
    assert all(len(b) == 1 for b in model.batches)


def test_no_probe_means_no_pause():
    _, stats = run([1, 1], FakeModel(), free_mb=None, reserve_mb=10**9)
    assert stats.pauses == 0


def test_growth_carries_across_calls():
    """Each worker call is one group of at most 32 chunks. If every call started
    over at the starting size, the batch could never grow; chained calls must
    pick up the size and the clean streak where the last one stopped."""
    size, streak, sizes = 4, 0, []
    for _ in range(6):
        model = FakeModel()
        _results, stats = run([1] * 32, model, start_budget=size, start_streak=streak)
        sizes.append(stats.largest_batch)
        size, streak = stats.end_budget, stats.end_streak
    assert sizes[0] == 4                              # 8 batches of 4: grows only as the call ends
    assert sizes == sorted(sizes) and sizes[-1] >= 7, sizes


def test_a_call_that_starts_over_the_ceiling_is_clamped():
    _results, stats = run([1] * 4, FakeModel(), start_budget=40, max_batch=16)
    assert stats.largest_batch == 4
    _results, stats = run([1] * 40, FakeModel(), start_budget=40, max_batch=16)
    assert stats.largest_batch == 16


def test_an_oom_shrinks_the_size_the_next_call_starts_at():
    _results, stats = run([1] * 8, FakeModel(fits=2), start_budget=8, max_batch=8)
    assert stats.end_budget <= 2


# ---------------------------------------------------------------------------
# BatchStats
# ---------------------------------------------------------------------------

def test_stats_merge_adds_counts_and_keeps_maxima():
    total = BatchStats(summarized=3, largest_batch=4, peak_mb=3000)
    total.merge({"summarized": 2, "empty_oom": 1, "largest_batch": 8, "peak_mb": 2500})
    assert total.summarized == 5 and total.empty_oom == 1
    assert total.largest_batch == 8 and total.peak_mb == 3000
    assert "5 summarized, 1 empty" in total.line()


def test_stats_merge_keeps_the_latest_end_state():
    total = BatchStats(end_budget=8000, end_streak=3)
    total.merge({"end_budget": 6000, "end_streak": 0})
    assert (total.end_budget, total.end_streak) == (6000, 0)
    total.merge({})                                   # a failed group reports nothing
    assert (total.end_budget, total.end_streak) == (6000, 0)


# ---------------------------------------------------------------------------
# Worker jobs: grouping, restart on failure, disable after a second failure
# ---------------------------------------------------------------------------

class FakeFuture:
    def __init__(self, outcome):
        self.outcome = outcome

    def result(self, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeExecutor:
    """Plays back a script of outcomes, one per submitted job."""

    instances = []

    def __init__(self, script):
        self.script = script
        self.submitted = []
        self.killed = False
        self.shut = False
        FakeExecutor.instances.append(self)

    def submit(self, fn, codes, *args):
        self.submitted.append(len(codes))
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "ok":
            return FakeFuture(([f"sum:{c}" for c in codes], {"summarized": len(codes)}))
        return FakeFuture(outcome)

    def kill_workers(self):
        self.killed = True

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut = True


def make_summarizer(script):
    FakeExecutor.instances = []
    shared = list(script)
    return sm.IsolatedChunkSummarizer(
        device="cuda", max_batch_size=8, vram_reserve_mb=512,
        executor_factory=lambda: FakeExecutor(shared),
    )


def test_a_large_tier_is_sent_in_bounded_groups():
    summ = make_summarizer([])
    out = summ.summarize_batch([f"c{i}" for i in range(200)])
    assert out == [f"sum:c{i}" for i in range(200)]
    assert FakeExecutor.instances[0].submitted == [96, 96, 8]
    assert summ.stats.summarized == 200


def test_a_timeout_kills_the_worker_and_retries_the_group_once():
    summ = make_summarizer([FutureTimeout()])
    out = summ.summarize_batch(["a", "b"])
    assert out == ["sum:a", "sum:b"]
    first, second = FakeExecutor.instances
    assert first.killed, "a timed-out worker must be killed, not left holding GPU memory"
    assert second.submitted == [2]
    assert summ._failed is False


def test_two_failures_in_a_row_disable_summarization_and_are_counted():
    summ = make_summarizer([FutureTimeout(), RuntimeError("worker died")])
    out = summ.summarize_batch(["a", "b", "c"])
    assert out == ["", "", ""]
    assert summ._failed is True
    assert summ.stats.empty_error == 3
    assert summ.summarize_batch(["d"]) == [""]         # stays off, no new worker
    assert len(FakeExecutor.instances) == 2


def test_the_knobs_reach_the_worker_call():
    seen = {}

    class Recorder(FakeExecutor):
        def submit(self, fn, codes, max_new_tokens, max_batch, reserve, budget):
            seen.update(fn=fn, max_batch=max_batch, reserve=reserve, budget=budget)
            return super().submit(fn, codes)

    summ = sm.IsolatedChunkSummarizer(device="cuda", max_batch_size=6, vram_reserve_mb=700,
                                      token_budget=9000, executor_factory=lambda: Recorder([]))
    summ.summarize_batch(["x"])
    assert seen == {"fn": sm._worker_summarize, "max_batch": 6, "reserve": 700, "budget": 9000}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_batch_knobs_read_config_and_clamp(tmp_path, monkeypatch):
    (tmp_path / "indexer.toml").write_text(
        "[summarization]\nmax_batch_size = 0\nvram_reserve_mb = -5\nbatch_token_budget = 0\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config.reset_config_cache()
    try:
        assert config.summarizer_max_batch_size() == 1
        assert config.summarizer_vram_reserve_mb() == 0
        assert config.summarizer_batch_token_budget() == 1
        summ = sm.IsolatedChunkSummarizer(device="cpu")
        assert summ._max_batch_size == 1 and summ._reserve_mb == 0
    finally:
        config.reset_config_cache()
