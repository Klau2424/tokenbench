"""Free tests for the runner's pure pieces: command building and JSON parsing."""

from __future__ import annotations

import json

from tokenbench import runner
from tokenbench.experiment import Arm, v0_experiment


def _claude_json(**over):
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 4200,
        "session_id": "abc-123",
        "result": "done",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 9000,
            "output_tokens": 1400,
            "cache_read_input_tokens": 12000,
            "cache_creation_input_tokens": 4000,
        },
    }
    data.update(over)
    return json.dumps(data)


def test_build_command_baseline_has_no_system_prompt():
    cmd = runner.build_command(["claude"], "do it", "sonnet", "Read,Write", None, bare=True)
    assert cmd[:2] == ["claude", "-p"]
    assert "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "sonnet" in cmd
    assert "--bare" in cmd
    assert "--allowedTools" in cmd
    assert "--append-system-prompt" not in cmd


def test_build_command_treatment_injects_rule():
    cmd = runner.build_command(["claude"], "do it", "sonnet", "Read", "Be terse.", bare=False)
    assert "--append-system-prompt" in cmd
    assert "Be terse." in cmd
    assert "--bare" not in cmd


def test_parse_result_valid():
    rec = runner.parse_result(_claude_json(), 0)
    assert rec["valid"] is True
    assert rec["input_tokens"] == 9000
    assert rec["output_tokens"] == 1400
    assert rec["cache_read_tokens"] == 12000
    assert rec["cache_creation_tokens"] == 4000
    assert rec["total_tokens"] == 9000 + 1400 + 12000 + 4000
    assert rec["total_cost_usd"] == 0.0123
    assert rec["num_turns"] == 3


def test_parse_result_is_error_marked_invalid():
    rec = runner.parse_result(_claude_json(is_error=True, subtype="error_max_turns"), 0)
    assert rec["valid"] is False
    assert "is_error=True" in rec["error"]


def test_parse_result_nonzero_returncode_invalid():
    rec = runner.parse_result(_claude_json(), 1)
    assert rec["valid"] is False


def test_parse_result_zero_turns_invalid():
    rec = runner.parse_result(_claude_json(num_turns=0), 0)
    assert rec["valid"] is False


def test_parse_result_bad_json_invalid():
    rec = runner.parse_result("not json", 0)
    assert rec["valid"] is False
    assert "json parse failed" in rec["error"]


def test_parse_result_model_from_model_usage():
    rec = runner.parse_result(_claude_json(modelUsage={"claude-sonnet-4-6": {}}), 0)
    assert rec["model"] == "claude-sonnet-4-6"


def test_parse_result_primary_model_is_highest_volume():
    # Claude Code logs a small helper model alongside the main one; pick the main one.
    mu = {
        "claude-haiku-4-5": {"inputTokens": 50, "outputTokens": 10},
        "claude-sonnet-4-6": {"inputTokens": 9000, "outputTokens": 1400},
    }
    rec = runner.parse_result(_claude_json(modelUsage=mu), 0)
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["model_usage"] == mu


def test_config_hash_differs_between_arms():
    exp = v0_experiment()
    base, terse = exp.arms[0], exp.arms[1]
    assert runner.config_hash(exp, base) != runner.config_hash(exp, terse)
    # Stable across calls.
    assert runner.config_hash(exp, base) == runner.config_hash(exp, base)


def test_reset_fixture_removes_artifact(tmp_path):
    artifact = tmp_path / "NOTES.md"
    artifact.write_text("stuff", encoding="utf-8")
    runner.reset_fixture(tmp_path, "NOTES.md")
    assert not artifact.exists()
    # Idempotent when already clean.
    runner.reset_fixture(tmp_path, "NOTES.md")
