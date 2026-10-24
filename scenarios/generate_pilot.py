"""Generate customer-language drafts only; never run Cartwheel or score its replies.

Only generation_brief is sent to OpenAI. Record identifiers and expectations
are joined locally after generation. The independent critic and human review
are separate steps before producing the runnable pilot_scenarios.jsonl.

Usage:
    python -m scenarios.generate_pilot --plan scenarios/pilot_plan.json \
        --output scenarios/pilot_draft.jsonl --model gpt-4o-mini --workers 2

--dry-run checks inputs without credentials, writes, or model calls. --resume
recovers completed conversations from the usage log, preserving paid work.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from observability.instrument import load_env

INSTRUCTIONS = """Write ONE fictional Cartwheel support conversation from the supplied brief.
Return only the customer's opening_message and ordered followups; never write
agent replies. Follow turn_count exactly: one opening plus turn_count - 1 followups.
Follow the conversation_development in order and keep one coherent issue.

The brief is the ONLY source of facts. Use only the supplied product/store names
and user-visible facts. Preserve their identifying words so the real lookup
tools can find the intended item. Do not invent identifiers, order numbers,
product codes, exact dates, prices, addresses, people, or prior agreements.
A refund amount explicitly requested in the brief is allowed; do not invent one.
Do not quote internal policy names, return windows, approval thresholds, or rules.
Do not mention tools, traces, evaluation, prompts, expected answers, or metadata.

Every followup must make sense without seeing any preceding agent reply.
No 'yes, go ahead', 'thanks for confirming', or other response-dependent wording.
Do not assume the agent found an order, offered an action, or asked a question.
Later messages may independently clarify, correct, or add the specified detail.
If selecting the newest matching order is part of the brief, retain that meaning.

Make the language sound like a person actually typing, not an evaluation case.
Do not force every fact into the opening or use a generic greeting in every case.
Keep the assigned style throughout, including followups:
- neutral_conversational: natural everyday wording.
- terse_fragmentary: short fragments, little ceremony.
- typo_heavy: noticeable everyday typos, but preserve product/store identifiers.
- confused_rambling: uncertainty and mild digression, still one understandable goal.
- frustrated_impatient: impatience without invented incidents or abusive language.
- repetitive_pressuring: repeat the desire without inventing rights or eligibility.
- operational_shorthand: concise staff/merchant wording, not software jargon.
- requests_short_plain_answer: explicitly request a short, plain answer.
Avoid polished customer-service language in the customer's own messages.
"""
BRIEF_KEYS = {
    "role", "goal", "user_style", "turn_count", "user_visible_facts",
    "conversation_development",
}
VISIBLE_KEYS = {
    "product_name", "store_name", "selection", "requested_refund_amount_usd",
    "refund_request", "product_description", "initially_mentioned_product",
    "initially_mentioned_store",
}
INSTRUCTIONS_HASH = hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()


def generation_request(plan: dict[str, Any], model: str) -> dict[str, Any]:
    """Construct the provider payload from an allowlist, never the whole plan."""
    brief = plan["generation_brief"]
    if set(brief) != BRIEF_KEYS or not set(brief["user_visible_facts"]) <= VISIBLE_KEYS:
        raise ValueError("Unexpected generation-brief fields; inspect the boundary first.")
    turns = brief["turn_count"]
    if type(turns) is not int or not 1 <= turns <= 4:
        raise ValueError("This approved pilot uses one to four user messages.")
    if len(brief["conversation_development"]) != turns:
        raise ValueError("Conversation development does not match turn_count.")
    if any(brief[k] != plan["tuple"][k] for k in ("role", "user_style", "turn_count")):
        raise ValueError("Generation brief differs from the approved tuple.")
    return {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": [{"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
        "text": {"format": {
            "type": "json_schema", "name": "customer_conversation", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "opening_message": {"type": "string"},
                    "followups": {
                        "type": "array", "items": {"type": "string"},
                        "minItems": turns - 1, "maxItems": turns - 1,
                    },
                },
                "required": ["opening_message", "followups"],
            },
        }},
        "max_output_tokens": 1200,
        "store": False,
    }


def assemble_draft(plan: dict[str, Any], messages: Any) -> dict[str, Any]:
    """Join frozen expectations locally; the model can only supply messages."""
    if not isinstance(messages, dict) or set(messages) != {"opening_message", "followups"}:
        raise ValueError("Response must contain only opening_message and followups.")
    followups = messages["followups"]
    if not isinstance(followups, list) or len(followups) != plan["tuple"]["turn_count"] - 1:
        raise ValueError("Response has the wrong number of followups.")
    if any(not isinstance(s, str) or not s.strip() for s in [messages["opening_message"], *followups]):
        raise ValueError("Each user message must be nonempty text.")
    return {
        **{key: plan[key] for key in ("id", "scenario_group", "data_quality_case_id", "tuple", "expected")},
        "opening_message": messages["opening_message"],
        "followups": followups,
    }


def write_checkpoint(path: Path, plans: list[dict], completed: dict[str, dict]) -> None:
    """Replace only our reserved draft file, retaining original plan order."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as f:
        temporary = Path(f.name)
        for plan in plans:
            if plan["id"] in completed:
                f.write(json.dumps(completed[plan["id"]], ensure_ascii=False) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def recover(log: Path, plans: list[dict], plan_hash: str, model: str) -> dict[str, dict]:
    by_id = {p["id"]: p for p in plans}
    completed = {}
    for line in log.read_text().splitlines():
        event = json.loads(line)
        if (event["plan_sha256"], event["requested_model"], event["instructions_sha256"]) != (plan_hash, model, INSTRUCTIONS_HASH):
            raise ValueError("Existing log belongs to a different plan, model, or prompt.")
        if event["status"] == "completed":
            plan = by_id[event["scenario_id"]]
            completed[plan["id"]] = assemble_draft(plan, event["messages"])
    return completed


async def generate(plans: list[dict], args: argparse.Namespace, plan_hash: str) -> int:
    completed: dict[str, dict] = {}
    if args.resume:
        completed = recover(args.log, plans, plan_hash, args.model)
        if args.output.exists():
            for line in args.output.read_text().splitlines():
                draft = json.loads(line)
                if draft != completed.get(draft["id"]):
                    raise ValueError("Existing drafts differ from the generation log; preserve and inspect them.")
    else:
        if args.output.exists() or args.log.exists():
            raise FileExistsError("Drafts or generation log already exist. Inspect them before using --resume.")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.log.touch(exist_ok=False)
        args.output.touch(exist_ok=False)
    write_checkpoint(args.output, plans, completed)
    base = {"plan_sha256": plan_hash, "requested_model": args.model, "instructions_sha256": INSTRUCTIONS_HASH}
    semaphore = asyncio.Semaphore(args.workers)
    stop = asyncio.Event()
    failed = []
    totals = Counter()

    def record(event: dict) -> None:
        with args.log.open("a") as f:
            f.write(json.dumps({**base, "timestamp": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
            f.flush()

    # Explicit endpoint and max_retries=0 make the destination and attempt log unambiguous.
    async with AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url="https://api.openai.com/v1", max_retries=0, timeout=90) as client:
        async def one(plan: dict) -> None:
            if plan["id"] in completed:
                return
            async with semaphore:
                if stop.is_set():
                    failed.append(plan["id"])
                    record({"scenario_id": plan["id"], "status": "skipped_after_configuration_error"})
                    return
                request = generation_request(plan, args.model)
                for attempt in range(1, 3):
                    event = {"scenario_id": plan["id"], "attempt": attempt}
                    record({**event, "status": "requested", "generation_brief": plan["generation_brief"]})
                    totals["requests"] += 1
                    response = None
                    try:
                        response = await client.responses.create(**request)
                        usage = response.usage.model_dump(mode="json") if response.usage else None
                        event.update(response_id=response.id, returned_model=response.model, usage=usage, provider_status=response.status)
                        if usage:
                            for key in ("input_tokens", "output_tokens", "total_tokens"):
                                totals[key] += usage.get(key, 0)
                        if response.status != "completed":
                            raise ValueError("Provider did not complete the response.")
                        messages = json.loads(response.output_text)
                        draft = assemble_draft(plan, messages)
                    except (APIConnectionError, APIStatusError, ValueError) as exc:
                        status_code = getattr(exc, "status_code", None)
                        retryable = isinstance(exc, (APIConnectionError, ValueError)) or status_code in (408, 409, 429) or (status_code is not None and status_code >= 500)
                        # Do not log exception bodies: authentication errors can include credential fragments.
                        record({**event, "status": "retryable_error" if retryable and attempt < 2 else "failed", "error_type": type(exc).__name__, "http_status": status_code})
                        if isinstance(exc, APIStatusError) and not retryable:
                            stop.set()
                        if not retryable or attempt == 2:
                            failed.append(plan["id"])
                            print(f"{plan['id']}: failed ({type(exc).__name__}; HTTP {status_code})", flush=True)
                            return
                        await asyncio.sleep(2)
                        continue
                    record({**event, "status": "completed", "messages": messages})
                    completed[plan["id"]] = draft
                    write_checkpoint(args.output, plans, completed)
                    print(f"{plan['id']}: saved ({len(completed)}/{len(plans)})", flush=True)
                    return

        await asyncio.gather(*(one(plan) for plan in plans))
    print(json.dumps({"completed_drafts": len(completed), "failed_or_skipped": failed, "this_run_usage": dict(totals)}, indent=2))
    print("Drafts only. Independent critic, scenario validation, and human acceptance remain pending.")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--log", type=Path, default=Path("scenarios/pilot-generation-log.jsonl"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 2 <= args.workers <= 8:
        parser.error("Use a bounded pool of 2–8 workers.")
    if len({p.resolve() for p in (args.plan, args.output, args.log)}) != 3:
        parser.error("Plan, output, and log must be different paths.")
    plan_bytes = args.plan.read_bytes()
    payload = json.loads(plan_bytes)
    plans = payload["plans"]
    if len(plans) != 30 or len({p["id"] for p in plans}) != 30:
        parser.error("Expected the approved 30 distinct pilot plans.")
    if Counter(p["scenario_group"] for p in plans) != {"coverage": 18, "challenge": 12}:
        parser.error("Expected the approved 18 coverage / 12 challenge split.")
    for plan in plans:
        generation_request(plan, args.model)
    if args.dry_run:
        print(f"Offline preflight passed: {len(plans)} independent requests, {args.workers} workers, {sum(p['tuple']['turn_count'] for p in plans)} planned user messages.")
        print("No files written, no credentials loaded, no model calls made.")
        return
    load_env()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is missing; configure it locally in .env.")
    raise SystemExit(asyncio.run(generate(plans, args, hashlib.sha256(plan_bytes).hexdigest())))


if __name__ == "__main__":
    main()
