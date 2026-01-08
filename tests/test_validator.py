import os
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import validator  # noqa: E402


def test_validator_success(monkeypatch):
    calls = []

    def fake_validator(run_id, episode):
        calls.append((run_id, episode))
        return validator.ValidationResult(True)

    monkeypatch.setattr(validator, "_load_validators", lambda modules: [("fake", fake_validator)])

    result = validator.validate("run_001", 1)
    assert result.success is True
    assert calls == [("run_001", 1)]


def test_validator_failure(monkeypatch):
    def bad_validator(run_id, episode):
        return validator.ValidationResult(False, "video_missing", "fail")

    monkeypatch.setattr(validator, "_load_validators", lambda modules: [("bad", bad_validator)])

    result = validator.validate("run_001", 1)
    assert result.success is False
    assert result.error == "video_missing"
