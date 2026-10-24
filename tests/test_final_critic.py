"""Offline final-critic comparison, evidence, and retry checks."""

import argparse
import asyncio
import json
from types import SimpleNamespace

from scenarios import critique_final as final
from scenarios import critique_pilot as engine
from tests.test_pilot_generation import plan
from tests.test_pilot_critic import draft, review_result


def test_comparisons_are_bounded_deterministic_and_include_identical_language():
    target = draft(plan("target"))
    others = [draft(plan(f"other-{i:03d}")) for i in range(30)]
    for i, d in enumerate(others):
        d["opening_message"] = f"unrelated request number {i}"
    others[-1]["opening_message"] = target["opening_message"]
    batch = [target, *others]
    chosen = final.comparison_drafts(target, batch)
    assert len(chosen) == len({d["id"] for d in chosen}) == 15
    assert "target" not in {d["id"] for d in chosen}
    assert others[-1] in chosen
    assert final.comparison_drafts(target, list(reversed(batch))) == chosen


def test_final_payload_has_grounding_but_no_unrelated_secrets():
    p = plan()
    p["grounded_context"]["initially_mentioned_product"] = {"title": "First item"}
    p["unrelated_secret"] = "DO NOT SEND"
    d = draft(p)
    request = final.critic_request(p, d, [d], "gpt-4o-mini")
    payload = json.loads(request["input"][0]["content"])
    assert payload["review_only_expected_behavior"] == p["expected"]
    assert payload["review_only_evidence"]["initially_mentioned_product"]["title"] == "First item"
    assert "DO NOT SEND" not in json.dumps(request)
    assert "newest_named_product_match" in request["instructions"]
    assert request["store"] is False and "previous_response_id" not in request


def test_retry_retains_final_instructions_and_resume_makes_no_paid_calls(tmp_path, monkeypatch):
    p = plan("test-final")
    d = draft(p)
    calls = []

    class Client:
        def __init__(self, **kw):
            self.responses = self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def create(self, **request):
            calls.append(request["instructions"])
            result = review_result(d)
            if len(calls) == 1:
                result["decision"] = "revise"  # Invalid: wording is unchanged.
                result["issues"] = ["Change wording."]
            return SimpleNamespace(id="offline", model="offline", usage=None, status="completed", output_text=json.dumps(result))

    monkeypatch.setattr(engine, "AsyncOpenAI", Client)
    monkeypatch.setattr(engine, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    args = argparse.Namespace(output=tmp_path/"c.jsonl", review_output=tmp_path/"r.jsonl", log=tmp_path/"l.jsonl", resume=False, model="gpt-4o-mini", workers=2)
    identity = {"instructions_sha256": final.INSTRUCTIONS_HASH}
    assert asyncio.run(engine.critique([p], [d], args, identity, request_builder=final.critic_request, instructions=final.INSTRUCTIONS)) == 0
    assert len(calls) == 2 and all("FINAL-BATCH CLARIFICATIONS" in c for c in calls)
    saved = args.output.read_bytes(), args.review_output.read_bytes()
    args.resume = True
    assert asyncio.run(engine.critique([p], [d], args, identity, request_builder=final.critic_request, instructions=final.INSTRUCTIONS)) == 0
    assert len(calls) == 2 and saved == (args.output.read_bytes(), args.review_output.read_bytes())
