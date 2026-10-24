"""Independent review of 250 fictional HW3 customer conversations.

Original drafts and plans are read-only. The critic sees review-only synthetic
evidence but may rewrite only messages, using the user-visible brief. Each
fresh call compares 12 similar original drafts and up to 3 spread across the
batch. No Cartwheel requests or student assessments are made.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from observability.instrument import load_env
from scenarios import critique_pilot as engine
from scenarios.generate_final import validate_plan
from scenarios.generate_pilot import assemble_draft


INSTRUCTIONS = engine.INSTRUCTIONS + """

FINAL-BATCH CLARIFICATIONS:
Treat newest_named_product_match and latest_matching_order as the same selection
requirement: keep most recent/latest purchase of the named product explicit.
For corrections, keep latest selection for BOTH the initially named purchase
and the corrected purchase, in their assigned turns.
Only the brief supplies facts the speaker knows. Do not add the selected
order's status, dates, return eligibility, or a diagnosed data defect.
Preserve requested refund amounts exactly. A full-refund request needs no amount.
Do not treat a customer's question as an obligation to state the policy answer.
Do not add extra turns, actions, or requests beyond the assigned development.
The comparison examples are a bounded selection from the full original batch,
not all other conversations. Check repeated phrasing, but do not rewrite a
natural shared phrase solely to force uniqueness. Request all necessary repairs
in one pass and actually apply them to the returned wording.
"""
INSTRUCTIONS_HASH = hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()
COMPARISON_VERSION = "12-jaccard-neighbors-plus-3-spread-v1"


def tokens(draft: dict) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", " ".join([draft["opening_message"], *draft["followups"]]).casefold()))


def comparison_drafts(draft: dict, drafts: list[dict]) -> list[dict]:
    """Deterministic selection; never includes the target or generated revisions."""
    others = [d for d in drafts if d["id"] != draft["id"]]
    target = tokens(draft)

    def rank(other):
        words = tokens(other)
        similarity = len(target & words) / max(1, len(target | words))
        return (-similarity, other["id"])

    selected = sorted(others, key=rank)[:12]
    chosen = {d["id"] for d in selected}
    remaining = sorted((d for d in others if d["id"] not in chosen), key=lambda d: d["id"])
    if remaining:
        for index in sorted({len(remaining) // 4, len(remaining) // 2, 3 * len(remaining) // 4}):
            selected.append(remaining[index])
    return selected


def critic_request(plan: dict, draft: dict, drafts: list[dict], model: str) -> dict:
    comparisons = comparison_drafts(draft, drafts)
    request = engine.critic_request(plan, draft, comparisons, model)
    payload = json.loads(request["input"][0]["content"])
    # Final plans include additional authoritative context beyond pilot fields.
    context = plan.get("grounded_context", {})
    for key in ("store", "initially_mentioned_product", "initially_mentioned_store", "dispute_window", "authorization_boundary"):
        if key in context:
            payload["review_only_evidence"][key] = context[key]
    payload["comparison_scope"] = {"method": COMPARISON_VERSION, "original_batch_size": len(drafts),
                                   "comparison_scenario_ids": [d["id"] for d in comparisons]}
    request.update(instructions=INSTRUCTIONS, input=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    return request


def load_inputs(plan_path: Path, draft_path: Path, model: str):
    plan_bytes, draft_bytes = plan_path.read_bytes(), draft_path.read_bytes()
    pilot_ids = {json.loads(line)["id"] for line in Path("scenarios/pilot_scenarios.jsonl").read_text().splitlines() if line.strip()}
    plans = validate_plan(json.loads(plan_bytes), pilot_ids, model)
    drafts = [json.loads(line) for line in draft_bytes.decode().splitlines() if line.strip()]
    by_id = {d["id"]: d for d in drafts}
    if len(drafts) != 250 or len(by_id) != 250 or set(by_id) != {p["id"] for p in plans}:
        raise ValueError("Expected exactly one draft for each of the 250 final plans.")
    for p in plans:
        d = by_id[p["id"]]
        if any(d[k] != p[k] for k in engine.FROZEN_KEYS):
            raise ValueError("A draft changed the approved metadata or expectation.")
        assemble_draft(p, engine.messages(d))
    return plans, [by_id[p["id"]] for p in plans], {
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "drafts_sha256": hashlib.sha256(draft_bytes).hexdigest(),
        "instructions_sha256": INSTRUCTIONS_HASH,
        "comparison_version": COMPARISON_VERSION,
        "final_critic_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "critic_engine_sha256": hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--log", type=Path, default=Path("scenarios/final-critic-log.jsonl"))
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    paths = (args.plan, args.drafts, args.output, args.review_output, args.log)
    if len({p.resolve() for p in paths}) != 5:
        parser.error("Inputs and three outputs must be different files.")
    if not 2 <= args.workers <= 8:
        parser.error("Use 2–8 bounded workers.")
    protected = {p.resolve() for p in Path("scenarios").glob("pilot*")}
    protected |= {Path("scenarios", name).resolve() for name in (
        "final_plan.json", "final_draft.jsonl", "final-generation-log.jsonl", "support_scenarios.jsonl", "support_review.jsonl")}
    if protected & {p.resolve() for p in paths[2:]}:
        parser.error("Critic outputs must not replace original or reviewed artifacts.")
    plans, drafts, identity = load_inputs(args.plan, args.drafts, args.model)
    for p, d in zip(plans, drafts):
        critic_request(p, d, drafts, args.model)
    if args.resume:
        saved = engine.restore_existing(plans, drafts, args, {**identity, "requested_model": args.model})[0]
    else:
        saved = {}
        if any(p.exists() for p in paths[2:]):
            parser.error("Critic outputs already exist; inspect before using --resume.")
    if args.dry_run:
        signatures = Counter(json.dumps(engine.messages(d), sort_keys=True) for d in drafts)
        duplicates = sum(n - 1 for n in signatures.values() if n > 1)
        print(f"Offline critic preflight passed: {len(saved)} reviews preserved; {250-len(saved)} pending; {args.workers} workers.")
        print(f"Each fresh request reviews one full conversation plus up to 15 comparison drafts. Exact duplicate conversations: {duplicates}.")
        print("Only message wording may change. No files written, credentials loaded, or model calls made.")
        return
    load_env()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is missing; configure it locally in .env.")
    raise SystemExit(asyncio.run(engine.critique(plans, drafts, args, identity,
                                               request_builder=critic_request, instructions=INSTRUCTIONS)))


if __name__ == "__main__":
    main()
