"""Offline final-batch checks; provider isolation/resume tests live in the pilot suite."""

import copy
import json
from pathlib import Path

import pytest

from scenarios.generate_final import validate_plan


@pytest.fixture
def final_plan():
    return json.loads(Path("scenarios/final_plan.json").read_text())


def test_accepts_final_batch_and_rejects_reused_pilot_identity(final_plan):
    assert len(validate_plan(final_plan, {"hw3-pilot-001"}, "gpt-4o-mini")) == 250
    with pytest.raises(ValueError, match="pilot identifiers"):
        validate_plan(final_plan, {final_plan["plans"][0]["id"]}, "gpt-4o-mini")


def test_rejects_broken_allocation_and_defect_coverage(final_plan):
    changed = copy.deepcopy(final_plan)
    changed["plans"][0]["scenario_group"] = "challenge"
    with pytest.raises(ValueError, match="175 coverage"):
        validate_plan(changed, set(), "gpt-4o-mini")
    damaged = next(p for p in final_plan["plans"] if p["data_quality_case_id"])
    damaged["data_quality_case_id"] = None
    with pytest.raises(ValueError, match="five challenge"):
        validate_plan(final_plan, set(), "gpt-4o-mini")


def test_rejects_answer_key_leak_before_any_calls(final_plan):
    final_plan["plans"][0]["generation_brief"]["user_visible_facts"]["expected"] = "private answer"
    with pytest.raises(ValueError, match="Unexpected generation-brief"):
        validate_plan(final_plan, set(), "gpt-4o-mini")
