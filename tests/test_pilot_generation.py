"""Offline checks for prompt isolation, bounded calls, and paid-work recovery."""

import argparse
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from scenarios import generate_pilot as generator


def plan(scenario_id="private-plan-id", turns=2):
    return {
        "id": scenario_id,
        "scenario_group": "coverage",
        "data_quality_case_id": None,
        "tuple": {"role": "shopper", "user_id": 12345, "order_id": 54321,
                  "user_style": "terse_fragmentary", "turn_count": turns},
        "expected": {"outcome": "PRIVATE ANSWER KEY"},
        "grounded_context": {"delivery_date": "2026-06-01"},
        "generation_brief": {
            "role": "shopper", "goal": "Ask about a purchase.",
            "user_style": "terse_fragmentary", "turn_count": turns,
            "user_visible_facts": {"product_name": "Plain Mug"},
            "conversation_development": ["State a detail."] * turns,
        },
    }


def test_private_plan_changes_cannot_change_provider_payload():
    original = plan()
    changed = copy.deepcopy(original)
    changed.update(id="different-private-id", expected={"outcome": "DIFFERENT SECRET"},
                   grounded_context={"delivery_date": "1900-01-01"})
    changed["tuple"].update(user_id=999, order_id=888)
    request = generator.generation_request(original, "gpt-4o-mini")
    assert request == generator.generation_request(changed, "gpt-4o-mini")
    serialized = json.dumps(request)
    for private in ("PRIVATE ANSWER KEY", "private-plan-id", "12345", "54321", "2026-06-01"):
        assert private not in serialized
    assert "Plain Mug" in serialized
    original["generation_brief"]["user_visible_facts"]["expected"] = "leaked answer"
    with pytest.raises(ValueError):
        generator.generation_request(original, "gpt-4o-mini")


def test_model_cannot_replace_expectations_or_return_wrong_turn_count():
    original = plan()
    messages = {"opening_message": "where is my mug", "followups": ["the plain one"]}
    assert generator.assemble_draft(original, messages)["expected"] == original["expected"]
    for bad in ({**messages, "expected": {}}, {**messages, "followups": []},
                {**messages, "followups": ["   "]}):
        with pytest.raises(ValueError):
            generator.assemble_draft(original, bad)


def test_bounded_independent_calls_and_resume_preserve_completed_work(tmp_path, monkeypatch):
    active = peak = calls = 0

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "https://api.openai.com/v1"
            assert kwargs["max_retries"] == 0
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **request):
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            assert len(request["input"]) == 1
            assert "previous_response_id" not in request
            await asyncio.sleep(0.01)
            active -= 1
            return SimpleNamespace(
                id=f"fake-{calls}", model="fake-offline-model", status="completed",
                usage=SimpleNamespace(model_dump=lambda **kw: {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
                output_text=json.dumps({"opening_message": "where is my mug", "followups": ["the plain one"]}),
            )

    monkeypatch.setattr(generator, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    plans = [plan(f"test-{i}") for i in range(3)]
    args = argparse.Namespace(output=tmp_path / "drafts.jsonl", log=tmp_path / "log.jsonl",
                              resume=False, model="gpt-4o-mini", workers=2)
    assert asyncio.run(generator.generate(plans, args, "test-hash")) == 0
    assert calls == 3 and peak == 2
    saved = args.output.read_bytes()
    args.resume = True
    assert asyncio.run(generator.generate(plans, args, "test-hash")) == 0
    assert calls == 3 and args.output.read_bytes() == saved
    with pytest.raises(ValueError):
        generator.recover(args.log, plans, "changed-plan-hash", args.model)
    rows = [json.loads(line) for line in args.output.read_text().splitlines()]
    rows[0]["opening_message"] = "A human edited this draft"
    args.output.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError):
        asyncio.run(generator.generate(plans, args, "test-hash"))
    assert calls == 3


def test_existing_work_is_not_overwritten(tmp_path, monkeypatch):
    output = tmp_path / "drafts.jsonl"
    output.write_text("student work\n")
    args = argparse.Namespace(output=output, log=tmp_path / "log.jsonl", resume=False,
                              model="gpt-4o-mini", workers=2)
    monkeypatch.setattr(generator, "AsyncOpenAI", lambda **kw: pytest.fail("No provider call allowed"))
    with pytest.raises(FileExistsError):
        asyncio.run(generator.generate([plan()], args, "test-hash"))
    assert output.read_text() == "student work\n"
