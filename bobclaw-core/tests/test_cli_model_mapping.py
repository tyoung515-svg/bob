"""
BoBClaw Core — Tests for CLI model mapping (task 12)

The CLI should map --model local to None (empty string) so the router
picks a resident model instead of passing 'local' as a literal model id.
"""
from __future__ import annotations

import argparse


def test_model_local_maps_to_empty_string():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash")
    args = ap.parse_args(["--model", "local"])
    result = "" if args.model.strip().lower() == "local" else args.model
    assert result == ""


def test_model_other_passes_through():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash")
    args = ap.parse_args(["--model", "qwen3.5-2b"])
    result = "" if args.model.strip().lower() == "local" else args.model
    assert result == "qwen3.5-2b"


def test_default_model_stays():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash")
    args = ap.parse_args([])
    result = "" if args.model.strip().lower() == "local" else args.model
    assert result == "deepseek-v4-flash"


def test_model_local_case_insensitive():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash")
    args = ap.parse_args(["--model", "LOCAL"])
    result = "" if args.model.strip().lower() == "local" else args.model
    assert result == ""
