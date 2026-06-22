"""tokenbench — a controlled A/B harness for Claude Code token-reduction measurement.

v0 scope: run one fixed task headless N times per arm (baseline vs. one trivial rule),
capture input+output tokens and USD cost per run, and report mean +/- spread per arm.
"""

__version__ = "0.0.0"
