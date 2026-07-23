from smlgw.history import HistoryStore, _downsample, Sample


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_record_and_query_roundtrip():
    clk = FakeClock()
    store = HistoryStore(":memory:", sample_interval=0, retention_hours=24, clock=clk)
    for i in range(5):
        clk.t = 1000.0 + i
        store.record("m", "1-0:1.8.0*255", float(i))
    points = store.query("m", "1-0:1.8.0*255", since_seconds=3600, max_points=100)
    assert [p.value for p in points] == [0, 1, 2, 3, 4]


def test_sample_interval_throttles_writes():
    clk = FakeClock()
    store = HistoryStore(":memory:", sample_interval=10, clock=clk)
    assert store.record("m", "o", 1.0) is True   # first always stored
    clk.t += 5
    assert store.record("m", "o", 2.0) is False  # within interval -> skipped
    clk.t += 6
    assert store.record("m", "o", 3.0) is True   # 11s later -> stored
    assert [p.value for p in store.query("m", "o", since_seconds=3600)] == [1.0, 3.0]


def test_explicit_prune_removes_old_rows():
    clk = FakeClock()
    store = HistoryStore(":memory:", sample_interval=0, retention_hours=1, clock=clk)
    clk.t = 1000.0
    store.record("m", "o", 1.0)          # old
    clk.t = 1000.0 + 3600 + 100          # now, > 1h later
    removed = store.prune()
    assert removed == 1
    assert store.query("m", "o", since_seconds=10 ** 9) == []


def test_retention_applied_on_write():
    clk = FakeClock()
    store = HistoryStore(":memory:", sample_interval=0, retention_hours=1, clock=clk)
    clk.t = 1000.0
    store.record("m", "o", 1.0)          # old
    clk.t = 1000.0 + 3600 + 100
    store.record("m", "o", 2.0)          # opportunistic prune drops the old row
    values = [p.value for p in store.query("m", "o", since_seconds=10 ** 9)]
    assert values == [2.0]


def test_downsample_reduces_point_count():
    samples = [Sample(float(i), float(i)) for i in range(1000)]
    reduced = _downsample(samples, 50)
    assert len(reduced) <= 50
    # endpoints preserved approximately (bucket averages)
    assert reduced[0].value < reduced[-1].value


def test_latest_returns_most_recent():
    clk = FakeClock()
    store = HistoryStore(":memory:", sample_interval=0, clock=clk)
    clk.t = 1.0; store.record("m", "o", 10.0)
    clk.t = 2.0; store.record("m", "o", 20.0)
    assert store.latest("m", "o").value == 20.0
    assert store.latest("m", "nope") is None
