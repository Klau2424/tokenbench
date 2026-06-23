"""Free tests for the runner's pure pieces: command building and JSON parsing."""

from __future__ import annotations

import json
import sys

from tokenbench import runner
from tokenbench.experiment import Arm, Experiment, v0_experiment


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


# --- v1: coverage scoring + replication accumulation ---------------------------------

def test_score_artifact_reads_and_scores(tmp_path):
    art = tmp_path / "NOTES.md"
    art.write_text("camelize and pluralize are documented", encoding="utf-8")
    rec: dict = {}
    runner.score_artifact(rec, art, ("camelize", "pluralize", "underscore"))
    assert rec["output_quality"] == 2 / 3
    assert rec["quality_detail"]["n_mentioned"] == 2
    assert rec["quality_detail"]["missing"] == ["underscore"]


def test_score_artifact_missing_file_sets_none(tmp_path):
    rec: dict = {}
    runner.score_artifact(rec, tmp_path / "nope.md", ("camelize",))
    assert rec["output_quality"] is None


def test_score_artifact_no_symbols_sets_none(tmp_path):
    art = tmp_path / "NOTES.md"
    art.write_text("anything", encoding="utf-8")
    rec: dict = {}
    runner.score_artifact(rec, art, ())
    assert rec["output_quality"] is None


def _mini_experiment(tmp_path, n=1):
    """A tiny self-contained experiment so run_experiment can be exercised via the stub."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "inflection.py").write_text("def camelize(s):\n    return s\n", encoding="utf-8")
    return Experiment(
        id="test-exp",
        fixture_dir=fixture,
        prompt="do it",
        model="sonnet",
        allowed_tools="Read,Write,Edit",
        arms=[Arm("baseline", None), Arm("terse", "be terse")],
        n=n,
        expected_symbols=("camelize",),
        results_dir=tmp_path / "results",
    )


def test_run_experiment_accumulates_and_tags_batch(tmp_path):
    exp = _mini_experiment(tmp_path, n=1)
    stub_cmd = [sys.executable, str(runner.STUB)]

    p1 = runner.run_experiment(exp, base_cmd=stub_cmd, fresh=True)
    recs1 = [json.loads(line) for line in p1.read_text().splitlines() if line]
    assert len(recs1) == 2  # one per arm
    assert all(r["batch_id"] for r in recs1)
    assert all("output_quality" in r for r in recs1)
    first_batch = recs1[0]["batch_id"]

    # A second run appends (accumulates) with a distinct batch_id.
    p2 = runner.run_experiment(exp, base_cmd=stub_cmd, fresh=False)
    recs2 = [json.loads(line) for line in p2.read_text().splitlines() if line]
    assert len(recs2) == 4
    assert recs2[-1]["batch_id"] != first_batch


def test_run_experiment_fresh_truncates(tmp_path):
    exp = _mini_experiment(tmp_path, n=1)
    stub_cmd = [sys.executable, str(runner.STUB)]
    runner.run_experiment(exp, base_cmd=stub_cmd, fresh=True)
    p = runner.run_experiment(exp, base_cmd=stub_cmd, fresh=True)
    recs = [json.loads(line) for line in p.read_text().splitlines() if line]
    assert len(recs) == 2  # truncated, not accumulated


# --- v1.x: LLM judge ------------------------------------------------------------------

class _FakeJudge:
    def __init__(self, score):
        self._score = score

    def score(self, text):
        return {"judge_quality": self._score / 10, "judge_score": self._score, "judge_reason": "x"}


def test_score_judge_attaches_fields():
    rec: dict = {}
    runner.score_judge(rec, _FakeJudge(7), "artifact")
    assert rec["judge_score"] == 7
    assert rec["judge_quality"] == 0.7
    assert rec["judge_error"] is None


def test_score_judge_is_defensive_on_failure():
    class Boom:
        def score(self, t):
            raise RuntimeError("judge down")

    rec: dict = {}
    runner.score_judge(rec, Boom(), "artifact")
    assert rec["judge_score"] is None
    assert "RuntimeError" in rec["judge_error"]  # recorded, not raised


def test_run_experiment_judge_attaches_scores(tmp_path):
    # Full pipeline through the stub, which answers judge calls for $0.
    exp = _mini_experiment(tmp_path, n=1)
    stub_cmd = [sys.executable, str(runner.STUB)]
    p = runner.run_experiment(exp, base_cmd=stub_cmd, fresh=True, judge=True, judge_samples=2)
    recs = [json.loads(line) for line in p.read_text().splitlines() if line]
    assert all(isinstance(r["judge_score"], (int, float)) for r in recs)
    assert all(r["judge_n"] == 2 for r in recs)         # averaged two samples
    assert all("artifact_text" in r for r in recs)


def test_rejudge_rescores_only_saved_valid_artifacts(tmp_path):
    exp = _mini_experiment(tmp_path, n=1)
    path = exp.runs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    recs = [
        {"valid": True, "arm": "baseline", "run_index": 0,
         "artifact_text": "## camelize\n## pluralize\n"},
        {"valid": True, "arm": "terse", "run_index": 0, "artifact_text": "## camelize\n"},
        {"valid": False, "arm": "baseline", "run_index": 1, "artifact_text": None},  # skipped
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    stub_cmd = [sys.executable, str(runner.STUB)]
    runner.rejudge(exp, base_cmd=stub_cmd, samples=2)

    out = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert isinstance(out[0]["judge_score"], (int, float)) and out[0]["judge_n"] == 2
    assert len(out[0]["judge_scores"]) == 2
    assert out[2].get("judge_score") is None            # invalid / no-artifact row left alone
    # Token/coverage fields are never touched by rejudge (none here to begin with).
    assert "output_tokens" not in out[0]
