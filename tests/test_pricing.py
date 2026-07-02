"""Audit item #2: the model-keyed price table. A record priced at the wrong model (e.g. an Opus
judge protocol priced at Sonnet rates) is the silent bug this prevents."""

from __future__ import annotations

from tokenbench import stats


def test_normalize_model_maps_ids_and_bare_names():
    assert stats.normalize_model("claude-opus-4-8") == "opus"
    assert stats.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert stats.normalize_model("claude-haiku-4-5") == "haiku"
    assert stats.normalize_model("opus") == "opus"
    assert stats.normalize_model("gpt-4") is None and stats.normalize_model(None) is None


def test_prices_are_distinct_per_family_with_cache_invariant():
    for fam in ("sonnet", "opus", "haiku"):
        p = stats.PRICES[fam]
        assert abs(p.cache_creation - 2 * p.input) < 1e-15   # 1-hour write = 2x input
        assert abs(p.cache_read - 0.1 * p.input) < 1e-15      # read = 0.1x input
    assert stats.PRICES["opus"].input == 5.0e-6 and stats.PRICES["haiku"].input == 1.0e-6


def test_input_cost_uses_the_records_model():
    rec = {"model": "claude-opus-4-8", "input_tokens": 100,
           "cache_creation_tokens": 1000, "cache_read_tokens": 10000}
    opus = stats.input_cost_usd(rec)
    assert abs(opus - (100 * 5e-6 + 1000 * 10e-6 + 10000 * 0.5e-6)) < 1e-12
    # The same tokens priced as Sonnet (the old single-model behavior) would be cheaper.
    sonnet = stats.input_cost_usd({**rec, "model": "claude-sonnet-4-6"})
    assert opus > sonnet


def test_unknown_model_falls_back_and_is_flagged():
    rec = {"model": "gpt-4o", "input_tokens": 100, "cache_creation_tokens": 0, "cache_read_tokens": 0}
    # Falls back to the Sonnet default rather than crashing...
    assert stats.input_cost_usd(rec) == 100 * stats.PRICES["sonnet"].input
    # ...but is surfaced as unknown so a report can warn.
    assert stats.unknown_models([rec]) == {"gpt-4o"}
    assert not stats.is_known_model("gpt-4o")


def test_report_warns_on_unknown_model():
    recs = [
        {"arm": "a", "valid": True, "model": "wat", "input_tokens": 5, "output_tokens": 10,
         "cache_creation_tokens": 0, "cache_read_tokens": 0, "total_tokens": 15, "total_cost_usd": 0.001},
        {"arm": "b", "valid": True, "model": "wat", "input_tokens": 5, "output_tokens": 10,
         "cache_creation_tokens": 0, "cache_read_tokens": 0, "total_tokens": 15, "total_cost_usd": 0.001},
    ]
    report = stats.format_report(stats.compare_arms(recs, "a", "b", "input_cost_usd"))
    assert "unknown model" in report and "wat" in report
