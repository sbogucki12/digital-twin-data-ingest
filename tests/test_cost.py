from types import SimpleNamespace

import pytest

from digital_twin_ingest.cost import DEFAULT_PRICING, CostLedger, compute_cost

HAIKU = DEFAULT_PRICING["claude-haiku-5-5"]


def test_basic_cost():
    c = compute_cost({"input_tokens": 50_000, "output_tokens": 2_000}, HAIKU)
    assert c.cost_usd == pytest.approx((50_000 * 0.10 + 2_000 * 0.50) / 1e6)
    assert not c.long_context


def test_long_context_rate_card_applies_above_threshold():
    c = compute_cost({"input_tokens": 100_001, "output_tokens": 1000}, HAIKU)
    assert c.long_context
    assert c.cost_usd == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)


def test_cache_tokens_count_toward_threshold_and_are_priced():
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=0,
        cache_read_input_tokens=10_000,
        cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=2000, ephemeral_1h_input_tokens=0),
    )
    c = compute_cost(usage, HAIKU)
    expected = (1000 * 0.10 + 2000 * 0.10 * 1.25 + 10_000 * 0.10 * 0.1) / 1e6
    assert c.cost_usd == pytest.approx(expected)


def test_missing_fields_are_zero():
    assert compute_cost(SimpleNamespace(), HAIKU).cost_usd == 0


def test_ledger_records_and_summarizes(tmp_path):
    db = tmp_path / "logs" / "cost.sqlite"
    with CostLedger(db) as ledger:
        ledger.record(run_id="r1", model="claude-haiku-5-5", usage={"input_tokens": 500, "output_tokens": 100},
                      pricing=HAIKU, doc_id="d1", stop_reason="end_turn")
        ledger.record(run_id="r1", model="claude-haiku-5-5", usage={"input_tokens": 500, "output_tokens": 100},
                      pricing=HAIKU, doc_id="d2")
        ledger.record(run_id="r2", model="claude-haiku-5-5", usage={"input_tokens": 10, "output_tokens": 0},
                      pricing=HAIKU)
        rows = {r["run_id"]: r for r in ledger.run_summaries()}
        assert rows["r1"]["calls"] == 2
        assert rows["r1"]["input_tokens"] == 1000
        assert rows["r1"]["cost_usd"] == pytest.approx(2 * (500 * 0.10 + 100 * 0.50) / 1e6)
        assert ledger.total_cost() == pytest.approx(rows["r1"]["cost_usd"] + rows["r2"]["cost_usd"])
    assert db.exists()
