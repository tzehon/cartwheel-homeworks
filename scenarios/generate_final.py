"""Generate the 250 final customer drafts, without running Cartwheel.

Reuses the pilot's brief-only requests, bounded independent calls, checkpoints,
usage logging, and resume safeguards. --dry-run performs offline checks only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from observability.instrument import load_env
from scenarios.generate_pilot import generate, generation_request


def validate_plan(payload: dict, pilot_ids: set[str], model: str) -> list[dict]:
    if payload.get("format") != "hw3-grounded-final-plan-v1":
        raise ValueError("Expected the grounded final plan format.")
    plans = payload["plans"]
    ids = {p["id"] for p in plans}
    if len(plans) != 250 or len(ids) != 250:
        raise ValueError("Expected 250 distinct final plans.")
    if ids & pilot_ids:
        raise ValueError("Final identifiers must not reuse pilot identifiers.")
    if Counter(p["scenario_group"] for p in plans) != {"coverage": 175, "challenge": 75}:
        raise ValueError("Expected 175 coverage and 75 challenge plans.")
    defects = Counter(p["data_quality_case_id"] for p in plans if p["data_quality_case_id"])
    expected_defects = {
        "dq-order-missing-delivery-date", "dq-order-reversed-dates", "dq-order-store-mismatch",
        "dq-product-duplicate-title", "dq-product-invalid-price", "dq-product-missing-title",
    }
    if defects != {name: 5 for name in expected_defects} or any(
        p["data_quality_case_id"] and p["scenario_group"] != "challenge" for p in plans
    ):
        raise ValueError("Expected five challenge plans for each of the six documented defects.")
    if model != payload["agent_model"]:
        raise ValueError("Use the model selected in the approved final plan.")
    for plan in plans:
        generation_request(plan, model)
    return plans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, default=Path("scenarios/final-generation-log.jsonl"))
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 2 <= args.workers <= 8:
        parser.error("Use a bounded pool of 2–8 workers.")
    if len({p.resolve() for p in (args.plan, args.output, args.log)}) != 3:
        parser.error("Plan, output, and log must be different paths.")
    protected = {p.resolve() for p in Path("scenarios").glob("pilot*")}
    protected |= {Path("scenarios/support_scenarios.jsonl").resolve(), Path("scenarios/support_review.jsonl").resolve()}
    if {args.output.resolve(), args.log.resolve()} & protected:
        parser.error("Draft outputs must not replace pilot artifacts or reviewed final scenarios.")
    plan_bytes = args.plan.read_bytes()
    pilot_ids = {json.loads(line)["id"] for line in Path("scenarios/pilot_scenarios.jsonl").read_text().splitlines() if line.strip()}
    try:
        plans = validate_plan(json.loads(plan_bytes), pilot_ids, args.model)
        if args.resume:
            if not args.log.exists():
                raise ValueError("Resume requires an existing generation log.")
        elif args.output.exists() or args.log.exists():
            raise ValueError("Drafts or log already exist; inspect before using --resume.")
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(f"Offline preflight passed: {len(plans)} independent requests, {args.workers} workers, "
              f"{sum(p['tuple']['turn_count'] for p in plans)} planned user messages.")
        print("175 coverage; 75 challenge; five per defect; no pilot identifier reuse.")
        print("No files written, no credentials loaded, no model calls made.")
        return
    load_env()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is missing; configure it locally in .env.")
    raise SystemExit(asyncio.run(generate(plans, args, hashlib.sha256(plan_bytes).hexdigest())))


if __name__ == "__main__":
    main()
