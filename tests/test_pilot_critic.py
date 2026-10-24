"""Offline checks for critic boundaries, preservation, and independent calls."""

import argparse
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
import httpx
from openai import APIConnectionError

from scenarios import critique_pilot as critic
from scenarios.generate_pilot import assemble_draft
from tests.test_pilot_generation import plan


def draft(p):
    return assemble_draft(p, {"opening_message": "where is the mug", "followups": ["the plain one"]})


def review_result(d, **overrides):
    return {"decision": "keep", "issues": [], "review_note": "No language change needed.",
            **critic.messages(d), **overrides}


def test_critic_sees_grounding_and_comparison_but_not_unrelated_fields():
    p = plan()
    p["unrelated_secret"] = "DO NOT SEND"
    d = draft(p)
    other = draft(plan("another-plan"))
    request = critic.critic_request(p, d, [d, other], "gpt-4o-mini")
    body = json.loads(request["input"][0]["content"])
    assert body["review_only_expected_behavior"] == p["expected"]
    assert body["draft_to_review"] == critic.messages(d)
    assert body["other_drafts_for_repetition_check"] == [critic.messages(other)]
    assert "DO NOT SEND" not in json.dumps(request)
    assert "previous_response_id" not in request
    assert request["store"] is False
    assert list(request["text"]["format"]["schema"]["properties"]) == [
        "issues", "review_note", "decision", "opening_message", "followups",
    ]


def test_critic_cannot_change_answer_key_or_leak_hidden_facts():
    p = plan()
    d = draft(p)
    good = review_result(d, decision="revise", issues=["Improve clarity."], opening_message="status of the mug please")
    candidate, review = critic.assemble_review(p, d, good)
    assert candidate["expected"] == p["expected"]
    assert candidate["tuple"] == p["tuple"]
    assert review["original_messages"] == critic.messages(d)
    assert review["human_review_status"] == "pending"
    for wrong in (
        {**good, "expected": {}},
        {**good, "decision": "keep"},
        {**good, "opening_message": "Order #54321 delivered 2026-06-01"},
        {**good, "opening_message": "I'd like $30 back"},
        {**good, "followups": []},
    ):
        with pytest.raises(ValueError):
            critic.assemble_review(p, d, wrong)


def test_independent_bounded_reviews_resume_and_preserve_human_edits(tmp_path, monkeypatch):
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
            body = json.loads(request["input"][0]["content"])
            assert len(body["other_drafts_for_repetition_check"]) == 2
            assert "previous_response_id" not in request
            await asyncio.sleep(0.01)
            active -= 1
            result = review_result(body["draft_to_review"])
            return SimpleNamespace(id=f"fake-review-{calls}", status="completed", model="fake-offline-model",
                                   usage=SimpleNamespace(model_dump=lambda **kw: {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
                                   output_text=json.dumps(result))

    monkeypatch.setattr(critic, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    plans = [plan(f"case-{i}") for i in range(3)]
    drafts = [draft(p) for p in plans]
    args = argparse.Namespace(output=tmp_path/'candidate.jsonl', review_output=tmp_path/'review.jsonl',
                              log=tmp_path/'log.jsonl', resume=False, model='gpt-4o-mini', workers=2)
    identity = {"plan_sha256": "p", "drafts_sha256": "d", "instructions_sha256": critic.INSTRUCTIONS_HASH}
    assert asyncio.run(critic.critique(plans, drafts, args, identity)) == 0
    assert calls == 3 and peak == 2
    saved = args.output.read_bytes(), args.review_output.read_bytes()
    args.resume = True
    assert asyncio.run(critic.critique(plans, drafts, args, identity)) == 0
    assert calls == 3
    assert (args.output.read_bytes(), args.review_output.read_bytes()) == saved
    args.review_output.write_text(json.dumps({"scenario_id": "case-0", "human_decision": "revise"})+'\n')
    with pytest.raises(ValueError):
        asyncio.run(critic.critique(plans, drafts, args, identity))
    assert calls == 3


def test_altered_input_expectations_are_rejected_and_outputs_preserved(tmp_path, monkeypatch):
    plans = [plan(f"case-{i}") for i in range(30)]
    for p in plans[18:]:
        p["scenario_group"] = "challenge"
    drafts = [draft(p) for p in plans]
    plan_path, draft_path = tmp_path/'plan.json', tmp_path/'draft.jsonl'
    plan_path.write_text(json.dumps({"plans": plans}))
    drafts[0] = copy.deepcopy(drafts[0])
    drafts[0]["expected"]["outcome"] = "changed to fit language"
    draft_path.write_text('\n'.join(json.dumps(d) for d in drafts)+'\n')
    with pytest.raises(ValueError):
        critic.load_inputs(plan_path, draft_path)
    args = argparse.Namespace(output=tmp_path/'candidate.jsonl', review_output=tmp_path/'review.jsonl',
                              log=tmp_path/'log.jsonl', resume=False, model='gpt-4o-mini', workers=2)
    args.review_output.write_text('student review\n')
    monkeypatch.setattr(critic, 'AsyncOpenAI', lambda **kw: pytest.fail('No network call allowed'))
    with pytest.raises(FileExistsError):
        asyncio.run(critic.critique(plans, drafts, args, {}))
    assert args.review_output.read_text() == 'student review\n'


def test_persistent_connection_failure_stops_new_calls_without_leaking_error_body(tmp_path, monkeypatch):
    calls = 0

    class BrokenClient:
        def __init__(self, **kwargs):
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **request):
            nonlocal calls
            calls += 1
            try:
                raise PermissionError(13, "SECRET-IN-ERROR-BODY")
            except PermissionError as exc:
                raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses")) from exc

    monkeypatch.setattr(critic, "AsyncOpenAI", BrokenClient)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    monkeypatch.setattr(critic, "RETRY_DELAY_SECONDS", 0.01)
    plans = [plan(f"case-{i}") for i in range(6)]
    drafts = [draft(p) for p in plans]
    args = argparse.Namespace(output=tmp_path/'candidate.jsonl', review_output=tmp_path/'review.jsonl',
                              log=tmp_path/'log.jsonl', resume=False, model='gpt-4o-mini', workers=2)
    identity = {"plan_sha256": "p", "drafts_sha256": "d", "instructions_sha256": critic.INSTRUCTIONS_HASH}
    assert asyncio.run(critic.critique(plans, drafts, args, identity)) == 1
    assert calls == 2
    text = args.log.read_text()
    assert "SECRET-IN-ERROR-BODY" not in text
    events = [json.loads(line) for line in text.splitlines()]
    diagnostics = [e['connection_diagnostic'] for e in events if 'connection_diagnostic' in e]
    assert diagnostics[0]['exception_chain'] == [
        {'type': 'APIConnectionError'}, {'type': 'PermissionError', 'errno': 13},
    ]
    assert any(e.get('stop_reason') == 'two_consecutive_connection_errors' for e in events)


def test_resume_requests_only_unfinished_reviews_and_preserves_saved_records(tmp_path, monkeypatch):
    plans = [plan(f"case-{i}") for i in range(4)]
    for i, p in enumerate(plans):
        p['generation_brief']['goal'] = f'Goal {i}'
    drafts = [draft(p) for p in plans]
    identity = {"plan_sha256": "p", "drafts_sha256": "d", "instructions_sha256": critic.INSTRUCTIONS_HASH}
    args = argparse.Namespace(output=tmp_path/'candidate.jsonl', review_output=tmp_path/'review.jsonl',
                              log=tmp_path/'log.jsonl', resume=True, model='gpt-4o-mini', workers=2)
    old_candidates, old_reviews, log = [], [], []
    for p, d in zip(plans[:3], drafts[:3]):
        result = review_result(d)
        candidate, review = critic.assemble_review(p, d, result)
        call = {'response_id': 'original-paid-response-'+p['id'], 'usage': {'total_tokens': 20}}
        review['model_call'] = call
        old_candidates.append(candidate)
        old_reviews.append(review)
        # Old logs have no response-format version; they must remain recoverable.
        log.append({**identity, 'requested_model': args.model, 'status': 'completed',
                    'scenario_id': p['id'], 'critic_result': result, 'model_call': call})
    for path, rows in [(args.output, old_candidates), (args.review_output, old_reviews), (args.log, log)]:
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    log_prefix = args.log.read_bytes()
    requested_goals = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **request):
            payload = json.loads(request['input'][0]['content'])
            requested_goals.append(payload['generation_brief']['goal'])
            return SimpleNamespace(id='new-paid-response', status='completed', model='fake-offline-model',
                                   usage=None, output_text=json.dumps(review_result(payload['draft_to_review'])))

    monkeypatch.setattr(critic, 'AsyncOpenAI', FakeClient)
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-placeholder')
    assert asyncio.run(critic.critique(plans, drafts, args, identity)) == 0
    assert requested_goals == ['Goal 3']
    candidates = [json.loads(line) for line in args.output.read_text().splitlines()]
    reviews = [json.loads(line) for line in args.review_output.read_text().splitlines()]
    assert candidates[:3] == old_candidates
    assert reviews[:3] == old_reviews
    assert args.log.read_bytes().startswith(log_prefix)
