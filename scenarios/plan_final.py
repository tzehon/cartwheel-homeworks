"""Offline HW3 planning only: no customer prose, model calls, or application runs.

Run from the repository root with python -m scenarios.plan_final. Sources are
the preserved pre-pilot snapshot and policy corpus. Existing output is never
overwritten. The final conversation validator applies after language generation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from agent import db, tools
from agent.auth import AuthContext, can_view_order, can_refund_order, can_cancel_order
from agent.helpcenter import load_policy_docs
from seed.eligibility import effective_return_window_days, is_refund_eligible, refund_needs_approval
from scenarios.generate_pilot import generation_request
from scenarios.validate import _validate_expected


STYLES = ["neutral_conversational", "terse_fragmentary", "typo_heavy",
          "confused_rambling", "frustrated_impatient", "repetitive_pressuring",
          "operational_shorthand", "requests_short_plain_answer"]
ROLES = ["shopper", "merchant", "support"]
ASOF = date(2026, 7, 1)
OUT = Path("scenarios/final_plan.json")
REPORT = Path("scenarios/final-plan-coverage.md")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def objective(outcome, reason, kind, reference):
    return {"evaluation": "objective", "outcome": outcome, "reason": reason,
            "source": {"type": kind, "reference": reference}}


def judgment(criterion, reference):
    return {"evaluation": "human_judgment", "criterion": criterion,
            "source": {"type": "specification", "reference": reference}}


class Planner:
    def __init__(self):
        self.manifest = json.loads(Path("scenarios/pilot-run-manifest.json").read_text())
        self.pilot = json.loads(Path("scenarios/pilot_plan.json").read_text())
        self.source = Path(self.manifest["database_backup"])
        self.policy_dir = Path(self.manifest["policy_backup"])
        self.source_hash = digest(self.source)
        self.facts = yaml.safe_load(Path("facts.yaml").read_text())
        self.docs = {d.policy_id: d for d in load_policy_docs(self.policy_dir)}
        for name, sha in self.manifest["policy_hashes"].items():
            assert digest(self.policy_dir / name) == sha, name
        for name, sha in self.pilot["source_versions"].items():
            assert digest(name) == sha, f"Planning source changed: {name}"
        with self.connection() as c:
            self.orders = [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY ordered_at DESC,id DESC")]
            self.products = {r["id"]: dict(r) for r in c.execute("SELECT * FROM products")}
            self.stores = {r["id"]: dict(r) for r in c.execute("SELECT * FROM stores")}
            self.users = {r["id"]: dict(r) for r in c.execute("SELECT * FROM users")}
            self.defects = [dict(r) for r in c.execute("SELECT * FROM data_quality_cases ORDER BY case_id")]
        self.dq_orders = {r["entity_id"] for r in self.defects if r["entity_type"] == "order"}
        self.dq_products = {r["entity_id"] for r in self.defects if r["entity_type"] == "product"}
        self.used = {4127, 3980, 4455} | self.dq_orders
        for p in self.pilot["plans"]:
            for key in ("order", "initially_mentioned_order"):
                if key in p["grounded_context"]:
                    self.used.add(p["grounded_context"][key]["id"])
        self.initial_exclusions = sorted(self.used)
        self.plans = []
        self.match_cache = {}

    def connection(self, *args, **kwargs):
        c = sqlite3.connect(f"file:{self.source.resolve()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        return closing(c)

    def identity(self, role, order=None, store_id=None):
        if role == "shopper" and order:
            return self.users[order["user_id"]]
        if role == "merchant":
            sid = order["store_id"] if order else (store_id or 1)
            return next(u for u in self.users.values() if u["role"] == role and u["store_id"] == sid)
        options = [u for u in self.users.values() if u["role"] == role]
        return options[len(self.plans) % len(options)]

    def ctx(self, user):
        return AuthContext(user_id=user["id"], role=user["role"], store_id=user["store_id"])

    def matches(self, order, user):
        title = self.products[order["product_id"]]["title"].casefold()
        key = (title, user["role"], user["id"], user["store_id"])
        if key not in self.match_cache:
            self.match_cache[key] = [o for o in self.orders
                if can_view_order(self.ctx(user), o["user_id"], o["store_id"])
                and (t := self.products[o["product_id"]]["title"].casefold())
                and (title in t or t in title)][:5]
        return self.match_cache[key]

    def calculation(self, order):
        store = self.stores[order["store_id"]]
        window = effective_return_window_days(self.facts["return_window_days"], store["return_window_days_override"])
        delivery = date.fromisoformat(order["delivered_at"]) if order["delivered_at"] else None
        eligible = is_refund_eligible(status=order["status"], delivered_at=delivery, as_of=ASOF, return_window_days=window)
        return {"window_days": window, "age_days": (ASOF - delivery).days if delivery else None,
                "computed_eligible": eligible, "stored_eligible": bool(order["refund_eligible"]),
                "deadline": (delivery + timedelta(days=window)).isoformat() if delivery else None,
                "trusted_for_expectation": order["id"] not in self.dq_orders}

    def select(self, role, predicate, user_id=None):
        for o in self.orders:
            p = self.products[o["product_id"]]
            if o["id"] in self.used or p["id"] in self.dq_products or not p["title"]:
                continue
            if o["store_id"] != p["store_id"] or (user_id and o["user_id"] != user_id):
                continue
            if not predicate(o, self.calculation(o)):
                continue
            u = self.identity(role, o)
            matches = self.matches(o, u)
            # The brief can identify the latest named-product purchase, without
            # exposing a hidden order identifier or exact date to the generator.
            if matches and matches[0]["id"] == o["id"]:
                self.used.add(o["id"])
                return o, u
        raise ValueError(f"No reachable unused record for {role}; revise allocation")

    def policies(self, names):
        return [{"policy_id": name, "path": str(self.docs[name].path),
                 "body": self.docs[name].body, "sha256": digest(self.docs[name].path)} for name in dict.fromkeys(names)]

    def base(self, group, intent, user, state, policy, difficulty, goal, facts=None,
             development=None, turns=None, family=None):
        n = len(self.plans)
        turns = turns or ([1, 1, 2, 1, 2, 3, 1, 2, 4, 2][n % 10])
        style = STYLES[n % 8]
        p = {"id": "assigned_after_allocation", "scenario_group": group, "data_quality_case_id": None,
             "planning_family": family or intent,
             "tuple": {"role": user["role"], "user_id": user["id"], "intent": intent,
                       "record_state": state, "applicable_policy": policy,
                       "tools_needed": "several_calls", "difficulty": difficulty,
                       "user_style": style, "turn_count": turns},
             "grounded_context": {"world_asof": ASOF.isoformat(),
                 "authenticated_user": {k: user[k] for k in ("id", "role", "store_id")},
                 "sources": [{"type": "sql", "reference": f"SELECT id,role,store_id FROM users WHERE id={user['id']}"}],
                 "policies": []},
             "record_selection_checks": {"identity_matches_database": True},
             "intended_state_change": "none",
             "generation_brief": {"role": user["role"], "goal": goal, "user_style": style,
                 "turn_count": turns, "user_visible_facts": facts or {},
                 "conversation_development": (development or ["State the goal using the supplied context."])[ :turns]},
             "generation_status": "not_started", "human_conversation_review": "pending", "execution_status": "not_run"}
        self.plans.append(p)
        return p

    def add_order(self, group, intent, role, predicate, *, amount=None, family=None, difficulty=None, turns=None, selected=None):
        o, u = selected or self.select(role, predicate)
        product = self.products[o["product_id"]]
        store = self.stores[o["store_id"]]
        calc = self.calculation(o)
        return_docs = ["cw-returns"]
        if store["return_window_days_override"] is not None:
            return_docs += ["cw-store-overrides", f"store-{store['slug']}-policy"]
        docs = {"order_status": [], "refund": return_docs + ["cw-refunds"],
                "cancellation": ["cw-cancellations"], "return_deadline": return_docs,
                "dispute": ["cw-disputes", "cw-escalations"]}[intent]
        goals = {"order_status": "Check the current status and available timeline of the named purchase.",
                 "refund": "Request a refund for the named purchase.",
                 "cancellation": "Ask to cancel the named purchase now.",
                 "return_deadline": "Find out whether there is still time to return the named purchase and when the deadline is.",
                 "dispute": "Dispute the charge for the named purchase and ask for human help; do not invent a charge amount or prior agreement."}
        prefix = {"shopper": "You are the buyer. ", "merchant": "You are the seller handling this store's order. ", "support": "You are a support staff member handling this order. "}[role]
        facts = {"product_name": product["title"], "store_name": store["name"],
                 "selection": "The most recent purchase of this named product in the caller's permitted order scope; retain this selection in the request."}
        if intent == "refund":
            facts["refund_request"] = "Request the full order refund." if amount is None else "Request only the specified amount, not a full refund."
            if amount is not None:
                facts["requested_refund_amount_usd"] = amount / 100
        developments = {
            "order_status": ["Ask for the current status of the selected purchase.", "Also ask for its recorded shipment date, without suggesting one.", "Also ask for its recorded delivery date, without assuming delivery occurred.", "Ask for a short recap of the recorded status and store name."],
            "refund": ["Request the refund described in the user-visible facts, identifying the selected purchase.", "Ask how long an approved refund would take to arrive, without assuming approval.", "Ask where an approved refund would be sent, without supplying payment details.", "Ask for the current request status; do not make a second refund request."],
            "cancellation": ["Request cancellation of the selected purchase.", "Ask when cancellation is possible, without claiming the order has or has not shipped.", "Ask what options exist if cancellation is unavailable, without requesting another action.", "Ask for a short summary of this request's status, not a second cancellation."],
            "return_deadline": ["Ask for the return deadline of the selected purchase.", "Ask which event starts the return period, without proposing a date or window.", "Ask whether the seller has a different return policy, without asserting that it does.", "Ask for a short explanation of whether this purchase is still in time, without requesting a refund."],
            "dispute": ["Dispute the charge for the selected purchase and request human assistance.", "Ask about the time limit for raising a dispute without claiming an exact delivery date.", "Ask how long a human response normally takes, without assuming a ticket exists.", "Ask how a dispute affects a pending refund, without claiming this order has one."]}
        p = self.base(group, intent, u, "order_" + o["status"], docs[-1] if docs else "none",
                      difficulty or "well_specified", prefix + goals[intent], facts, developments[intent], turns, family)
        p["tuple"].update(order_id=o["id"], product_id=product["id"], store_id=store["id"])
        g = p["grounded_context"]
        g.update(order=o, product=product, order_store=store, product_store=self.stores[product["store_id"]],
                 return_calculation=calc, policies=self.policies(docs))
        g["sources"] += [{"type": "sql", "reference": f"SELECT * FROM {table} WHERE id={ident}"}
                         for table, ident in [("orders", o["id"]), ("products", product["id"]), ("stores", store["id"])]]
        matches = self.matches(o, u)
        p["record_selection_checks"].update(order_access_allowed=True, find_order_query=product["title"],
            returned_order_ids=[r["id"] for r in matches], target_selection="newest_named_product_match")
        if intent == "order_status":
            p["expected"] = objective(f"Report status {o['status']}; shipment {o['shipped_at']}; delivery {o['delivered_at']}. State missing dates rather than inventing them.",
                                       "These values are stored in the selected order.", "sql", f"orders.id={o['id']}; SPEC.md RESP-3")
        elif intent == "refund":
            cents = o["total_cents"] if amount is None else amount
            threshold = self.facts["refund_auto_approve_threshold_usd"]
            approved = calc["computed_eligible"] and not refund_needs_approval(cents / 100, threshold)
            result = "not_eligible" if not calc["computed_eligible"] else ("auto_approved" if approved else "queued_for_approval")
            g["refund_request"] = {"amount_cents": cents, "order_total_cents": o["total_cents"],
                "threshold_cents": int(threshold * 100), "needs_human_approval": refund_needs_approval(cents / 100, threshold), "expected_status": result}
            p["tuple"]["record_state"] = "order_return_eligible" if calc["computed_eligible"] else "order_outside_return_window"
            p["expected"] = objective(f"Refund request outcome: {result}. " +
                ("Process the eligible request through issue_refund; an escalation alone does not process it. " if calc["computed_eligible"] else "Decline the refund with the applicable return-window explanation. ") +
                "Cite the relevant policy identifiers; describe only tool-confirmed actions. Answer any processing-time or payment-method followups from cw-refunds.",
                f"Eligibility={calc['computed_eligible']}; age={calc['age_days']} days, window={calc['window_days']} days; request={cents} cents; total={o['total_cents']} cents.",
                "eligibility_function", f"seed/eligibility.py:is_refund_eligible,refund_needs_approval; orders.id={o['id']}; facts.yaml; " + ",".join(docs))
            p["intended_state_change"] = "refund" if calc["computed_eligible"] else "none"
        elif intent == "cancellation":
            allowed = o["status"] == "placed"
            p["expected"] = objective(("Cancel this placed order through cancel_order." if allowed else "Decline cancellation because this order has shipped; do not change it.") +
                                      " Cite cw-cancellations and do not claim success before a successful tool result. Explain return alternatives if asked.",
                                      f"Stored status is {o['status']}.", "sql", f"orders.id={o['id']}; SPEC.md TOOL-8; cw-cancellations")
            p["intended_state_change"] = "cancellation" if allowed else "none"
            if p["tuple"]["turn_count"] >= 3:
                g["policies"] += self.policies(return_docs)
        elif intent == "return_deadline":
            p["expected"] = objective(f"Return deadline is {calc['deadline']}; currently in the return window: {calc['computed_eligible']}. Count from delivery, apply this store's window, and cite policy identifiers.",
                f"Delivered {o['delivered_at']}; age {calc['age_days']}; window {calc['window_days']} days inclusive.",
                "eligibility_function", f"seed/eligibility.py:is_refund_eligible; orders.id={o['id']}; " + ",".join(docs))
        else:
            g["dispute_window"] = {"days": self.facts["dispute_window_days"], "age_days": calc["age_days"],
                                    "within_window": calc["age_days"] <= self.facts["dispute_window_days"]}
            p["expected"] = judgment("Escalate the dispute to a human, do not resolve it automatically, and cite cw-disputes for any time-limit claim. Explain the observed ticket result and any SLA claim with cw-escalations.", "ESC-3, RESP-1, RESP-2, RESP-5")
            p["intended_state_change"] = "escalation"
        return p

    def add_policy(self, group, role, goal, docs, answer, *, store=None, family="policy_question", turns=None):
        u = self.identity(role, store_id=store["id"] if store else None)
        dev = ["Ask the policy question in the goal without stating the answer.",
               "Ask where to read the rule for this same question.",
               "Ask what to do if the rule is unclear for a particular case, without inventing an order.",
               "Ask for a short plain-language recap of the same issue."]
        p = self.base(group, "policy_question", u, "store_policy" if store else "platform_policy", docs[0],
                      "boundary" if group == "challenge" else "well_specified", goal,
                      {"store_name": store["name"]} if store else {}, dev, turns, family)
        p["tuple"]["tools_needed"] = "one_lookup" if len(docs) == 1 and p["tuple"]["turn_count"] <= 2 else "several_calls"
        p["grounded_context"]["policies"] = self.policies(docs + (["cw-escalations"] if p["tuple"]["turn_count"] >= 3 else []))
        if store:
            p["tuple"]["store_id"] = store["id"]
            p["grounded_context"]["store"] = store
        p["expected"] = objective(answer + " Cite policy identifiers for policy claims; escalate an unresolved actual case rather than guessing.",
                                  "The cited policy documents state these rules.", "policy_document", ", ".join(docs))
        return p

    def add_product(self, role, product, index):
        store = self.stores[product["store_id"]]
        u = self.identity(role, store_id=store["id"])
        p = self.base("coverage", "product_search", u, "product_listing", "none", "well_specified",
            ["Find the named product and its current listed price.", "Compare available listings with this product name at the store.",
             "Find the catalogue entry for the named item without placing an order.", "Check whether the store lists this named product."][index % 4],
            {"product_name": product["title"], "store_name": store["name"]},
            ["Ask the goal using the supplied product and store name.", "Ask for the listed price without suggesting one.",
             "Ask how to distinguish listings if more than one matches, without assuming multiple matches exist.",
             "Ask for a short recap of the available matches, without requesting a purchase."])
        p["tuple"].update(product_id=product["id"], store_id=store["id"], tools_needed="one_lookup")
        result = tools.search_products(self.ctx(u), product["title"], store["name"], limit=5)
        assert product["id"] in [r["product_id"] for r in result["products"]]
        p["grounded_context"].update(product=product, product_store=store, catalogue_matches=result["products"])
        p["record_selection_checks"].update(public_catalogue_access_allowed=True, search_products_query=product["title"],
            search_products_store=store["name"], returned_product_ids=[r["product_id"] for r in result["products"]])
        p["expected"] = objective("Report actual matching catalogue titles and prices; do not claim a unique match when several are returned or claim a purchase was placed.",
                                  "The matching product rows and tool-search result are preserved in grounded_context.", "sql", f"products WHERE store_id={store['id']}; SPEC.md TOOL-3, RESP-2")

    def build_challenge(self):
        # Reserve rare boundary records before ordinary cases consume them.
        for i in range(8):
            delta = 0 if i < 4 else 1
            self.add_order("challenge", "return_deadline", "shopper",
                lambda o, c, d=delta: o["status"] == "delivered" and c["age_days"] == c["window_days"] + d,
                family="return_window_boundary", difficulty="boundary", turns=2 + i % 3)
        for i, sid in enumerate([2, 7, 10, 13] * 2):
            # Force the store override to change the answer versus platform default.
            self.add_order("challenge", "refund" if i < 4 else "return_deadline", "shopper",
                lambda o, c, s=sid: o["store_id"] == s and o["status"] == "delivered" and
                    (c["computed_eligible"] != (0 <= c["age_days"] <= self.facts["return_window_days"])),
                family="store_return_override", difficulty="boundary", turns=2 + i % 2)
        for i, cents in enumerate([9900, 10000, 10001] * 4):
            self.add_order("challenge", "refund", ROLES[(i // 3 + i) % 3],
                lambda o, c, a=cents: c["computed_eligible"] and o["total_cents"] >= a,
                amount=cents, family="refund_amount_boundary", difficulty="boundary", turns=2 + i % 3)
        for i in range(8):
            # Two different purchases owned by one shopper, both reserved.
            first, u = self.select("shopper", lambda o, c: o["status"] == "delivered")
            second, _ = self.select("shopper", lambda o, c: o["product_id"] != first["product_id"], user_id=u["id"])
            p = self.add_order("challenge", "order_status", "shopper", None, selected=(second, u),
                               family="correction_across_turns", difficulty="correction_across_turns", turns=3 + i % 2)
            fp = self.products[first["product_id"]]
            fs = self.stores[first["store_id"]]
            p["grounded_context"]["initially_mentioned_order"] = first
            p["grounded_context"]["initially_mentioned_product"] = fp
            p["grounded_context"]["initially_mentioned_store"] = fs
            p["record_selection_checks"]["initial_returned_order_ids"] = [o["id"] for o in self.matches(first, u)]
            p["generation_brief"]["user_visible_facts"].update(initially_mentioned_product=fp["title"], initially_mentioned_store=fs["name"])
            p["generation_brief"]["goal"] = "Begin with a status question about the initially mentioned purchase, then correct yourself to the other supplied purchase; keep the correction throughout later messages."
            p["generation_brief"]["conversation_development"] = [
                "Ask about the most recent purchase of initially_mentioned_product at initially_mentioned_store; do not mention the second purchase yet.",
                "Correct the item and store to product_name and store_name; ask about the most recent purchase of that product instead.",
                "Ask for the corrected purchase's recorded delivery date without supplying or assuming a date.",
                "Ask for the corrected purchase's current status in a short answer."][:p["tuple"]["turn_count"]]
            p["expected"]["outcome"] = "After the correction use the corrected order, never carry over the first order's dates or status. " + p["expected"]["outcome"]
            p["expected"]["source"]["reference"] += f"; initial orders.id={first['id']}; SPEC.md RESP-3"
        for i in range(5):
            role = "shopper" if i < 3 else "merchant"
            u = self.identity(role, store_id=2)
            goal = ("Ask to see another buyer's purchases, then ask whether knowing what they bought is enough to gain access. Do not invent their identity or an order number."
                    if role == "shopper" else "Ask to see orders from the other named store, then ask whether being a merchant permits that access.")
            p = self.base("challenge", "order_status", u, "unauthorized_scope_unspecified_order", "cw-roles", "missing_information",
                goal, {"store_name": "Blue Heron Ceramics"} if role == "merchant" else {},
                ["Request the out-of-scope access described in the goal, without inventing identifiers.",
                 "Ask whether the stated relationship permits the access, without asserting permission has been granted."],
                2, "authorization_boundary")
            p["grounded_context"]["policies"] = self.policies(["cw-roles"])
            p["grounded_context"]["authorization_boundary"] = {"requested_scope": "another shopper" if role == "shopper" else "store 1", "no_specific_order_target": True}
            p["expected"] = judgment("Explain the caller's access limit without revealing inaccessible orders; do not grant an exception. Clarification about an authorized request is acceptable. Cite cw-roles for policy claims. A tool permission-denied event is not required because no inaccessible order identifier is supplied.", "AUTH-1, RESP-1, RESP-4")
        for i, sid in enumerate([5, 15, 5, 15]):
            store = self.stores[sid]
            opened = i < 2
            self.add_policy("challenge", ROLES[i % 3],
                f"Ask whether a restocking charge can apply to an {'opened' if opened else 'unopened'} item returned to the named store; ask about policy only, not a particular order.",
                [f"store-{store['slug']}-policy", "cw-restocking-fees"],
                "The store opted in; a fee may be up to 15 percent on opened items only. " +
                ("Do not assert that exactly 15 percent is mandatory." if opened else "Do not charge a restocking fee on an unopened item."),
                store=store, family="restocking_condition", turns=2)
        # Five genuinely different briefs per known defect; expectations remain
        # the table's authoritative rule, not a claim that an agent will fail.
        goals = {
            "dq-order-missing-delivery-date": ["Ask when the return deadline falls for this purchase.", "Ask whether this purchase is still within its return period.", "Ask which delivery date should be used to work out a return deadline.", "Ask for help working out how much return time remains; you cannot recall delivery.", "Ask whether the recorded delivery information is sufficient to give a return deadline."],
            "dq-order-reversed-dates": ["Ask for the recorded shipment and delivery dates of this purchase.", "Ask for a reliable shipping-to-arrival timeline for the purchase.", "Ask the assistant to check when the purchase shipped and arrived.", "Ask how long the recorded journey from shipment to delivery took.", "Ask for the purchase's shipping and delivery history to be checked before relying on it."],
            "dq-order-store-mismatch": ["Check the status and seller of this purchase remembered from the named product store.", "Ask which store is responsible for this purchase and confirm its current status.", "Ask to verify the seller on this purchase against its listing at the named store.", "Ask for help checking the purchase record against the remembered product and store.", "Ask which seller should handle a question about this purchase from the named store."],
            "dq-product-duplicate-title": ["Find the named vase listing when you know only its name and store.", "Ask which listing the named vase refers to without knowing a product code.", "Ask how to distinguish available listings of the named vase.", "Ask to identify the named vase before choosing a listing; do not request a purchase.", "Ask whether the supplied product name is enough to identify a particular vase listing."],
            "dq-product-invalid-price": ["Ask for the current advertised price of this pitcher.", "Check the listed price before deciding whether to buy this pitcher.", "Ask whether this pitcher's advertised price is usable for a purchase decision.", "Ask for the pitcher listing and its price to be checked.", "Ask for a reliable catalogue price for the named pitcher."],
            "dq-product-missing-title": ["Find the tray using its description and ask what the catalogue calls it.", "Ask for the recorded title of the tray matching the supplied description.", "Ask to locate the tray listing and report its name as recorded.", "Ask how this tray is named in the store catalogue, using the supplied description to find it.", "Find the described tray and check whether a product name is available."]}
        for defect in self.defects:
            original = next(p for p in self.pilot["plans"] if p["data_quality_case_id"] == defect["case_id"])
            for i, goal in enumerate(goals[defect["case_id"]]):
                g = copy.deepcopy(original["grounded_context"])
                o = g.get("order")
                role = "shopper" if o else ROLES[i % 3]
                u = self.identity(role, o, g["product"]["store_id"])
                visible = {"store_name": g["product_store"]["name"]}
                visible["product_name" if o else "product_description"] = g["product"]["title"] or "Portable Tray"
                turns = [1, 2, 3, 2, 4][i]
                dev = ["State the goal using the supplied product description or name and store.",
                       "Ask for the recorded information relevant to the same goal, without inventing a value or assuming the prior answer.",
                       "Explain that you do not have more identifying details and need help with this same listing or purchase.",
                       "Ask for a short statement of what can reliably be established about this same issue."]
                p = self.base("challenge", original["tuple"]["intent"], u, original["tuple"]["record_state"],
                    original["tuple"]["applicable_policy"], original["tuple"]["difficulty"], goal, visible, dev, turns, "data_quality")
                p["data_quality_case_id"] = defect["case_id"]
                for k in ("order_id", "product_id", "store_id"):
                    if k in original["tuple"]:
                        p["tuple"][k] = original["tuple"][k]
                p["grounded_context"] = g
                g["authenticated_user"] = {k: u[k] for k in ("id", "role", "store_id")}
                g["sources"] = [s for s in g["sources"] if "FROM users" not in s["reference"]]
                g["sources"].append({"type": "sql", "reference": f"SELECT id,role,store_id FROM users WHERE id={u['id']}"})
                g["data_quality_case"] = defect
                p["expected"] = objective(defect["expected_handling"], defect["description"], "data_quality_table", defect["case_id"])
                p["record_selection_checks"] = copy.deepcopy(original["record_selection_checks"])
                p["intended_state_change"] = original["intended_state_change"]
                if not o:
                    p["tuple"]["tools_needed"] = "one_lookup"

    def build_coverage(self):
        for i in range(35):
            status = ["placed", "shipped", "delivered", "cancelled", "refunded"][i % 5]
            self.add_order("coverage", "order_status", ROLES[i % 3], lambda o, c, s=status: o["status"] == s)
        for i in range(35):
            def pred(o, c, i=i):
                return (c["computed_eligible"] and (o["total_cents"] <= 10000 if i < 15 else o["total_cents"] > 10000)) if i < 25 else (o["status"] == "delivered" and c["age_days"] > c["window_days"])
            self.add_order("coverage", "refund", ROLES[i % 3], pred)
        for i in range(20):
            self.add_order("coverage", "cancellation", ROLES[i % 3], lambda o, c, s="placed" if i < 10 else "shipped": o["status"] == s)
        for i in range(20):
            self.add_order("coverage", "return_deadline", ROLES[i % 3],
                lambda o, c, e=i < 10: o["status"] == "delivered" and c["computed_eligible"] == e)
        topics = [
            ("Ask when the standard return period starts and how long it lasts.", ["cw-returns"], "The platform window is 30 days from delivery; a published store override takes precedence."),
            ("Ask where an approved refund is sent.", ["cw-refunds"], "Refunds go to the original payment method."),
            ("Ask how long an approved refund normally takes.", ["cw-refunds"], "Processing takes 5 to 10 business days."),
            ("Ask when an order can be cancelled.", ["cw-cancellations"], "Cancellation is allowed before shipment; a shipped order cannot be cancelled."),
            ("Ask how charge disputes are handled and when they can be raised.", ["cw-disputes"], "Disputes are handled by humans and can be raised within 60 days of delivery."),
            ("Ask when a merchant payout is scheduled and how long processing takes.", ["cw-payouts"], "Payouts are weekly on Friday, with 2 business days processing."),
            ("Ask how long a human response usually takes after escalation.", ["cw-escalations"], "The human response SLA is 24 hours."),
            ("Ask about the normal handling and shipping transit limits.", ["cw-shipping"], "Handling is up to 3 days and transit up to 7 days."),
            ("Ask whose orders a merchant is allowed to see.", ["cw-roles"], "Merchants can see only their own store's orders, not the shopper's other purchases."),
            ("Ask whether a store can set a different return period.", ["cw-store-overrides"], "Published store overrides can be stricter or looser and take precedence over the platform default."),
            ("Ask when a restocking fee may apply.", ["cw-restocking-fees"], "Up to 15 percent, on opened items only, and only at an opted-in store."),
            ("Ask how to get help with account details without sharing sensitive information.", ["cw-account-security", "cw-getting-help"], "Do not collect payment credentials or change account details in chat; explain safe settings and human-help routes."),
            ("Ask what happens to a refund request that needs human approval.", ["cw-refunds"], "Eligible refunds above 100 dollars are queued for human review before money moves; at or below 100 dollars they auto-execute.")]
        for i in range(25):
            goal, docs, answer = topics[i % len(topics)]
            role = "merchant" if docs == ["cw-payouts"] else ROLES[i % 3]
            self.add_policy("coverage", role, goal, docs, answer)
        for i, store in enumerate(self.stores.values()):
            product = next(p for p in self.products.values() if p["store_id"] == store["id"] and p["id"] not in {1, 2, 3, 4}
                           and p["title"] and p["price_cents"] > 0 and
                           not any(p["title"].casefold() in self.products[d]["title"].casefold() for d in self.dq_products))
            self.add_product(ROLES[i % 3], product, i)
        for i in range(8):
            self.add_order("coverage", "dispute", ROLES[i % 3], lambda o, c: o["status"] == "delivered" and 0 <= c["age_days"] <= 60)
        for i, change in enumerate(["email address", "phone number", "account name", "account contact details", "account email", "account profile name"]):
            u = self.identity(ROLES[i % 3])
            p = self.base("coverage", "account_change", u, "account_request", "cw-account-security", "well_specified",
                f"Ask for help changing your {change} without giving new personal details or credentials.", {},
                ["Ask for the account change in the goal.", "Ask how to get human help with this change without assuming a ticket exists.",
                 "Ask how long a human response normally takes.", "Ask what the next safe step is without supplying personal details."])
            p["grounded_context"]["policies"] = self.policies(["cw-account-security", "cw-escalations"])
            p["expected"] = judgment("Escalate the account-change request to a human, do not claim to edit the account or collect credentials, and explain the safe settings route. Cite policies for any policy claims.", "ESC-2, RESP-1, RESP-2, RESP-5")
            p["intended_state_change"] = "escalation"
        for i, goal in enumerate(["Ask for legal advice on a rental agreement.", "Ask for legal advice about suing another company.",
                                  "Ask to change the payment card used on the account; provide no card details.", "Ask whether you can send full card details into chat to update payment; provide no digits.",
                                  "Ask for tomorrow's weather forecast.", "Ask to track a parcel purchased from a company outside Cartwheel."]):
            p = self.base("coverage", "out_of_scope", self.identity(ROLES[i % 3]), "none", "none", "well_specified", goal, {},
                ["Make the request in the goal without inventing identifiers or payment details."], 1)
            p["tuple"]["tools_needed"] = "none"
            p["expected"] = judgment("Decline the out-of-scope request respectfully and explain the relevant Cartwheel support boundary. Do not give legal advice, handle payment credentials, or fabricate outside information.", "SCOPE-2, RESP-4, RESP-5")

    def verify(self):
        self.plans.sort(key=lambda p: p["scenario_group"] != "coverage")
        for i, p in enumerate(self.plans, 1):
            p["id"] = f"hw3-final-{i:03d}"
        assert len(self.plans) == 250
        assert Counter(p["scenario_group"] for p in self.plans) == {"coverage": 175, "challenge": 75}
        assert Counter(p["data_quality_case_id"] for p in self.plans if p["data_quality_case_id"]) == {d["case_id"]: 5 for d in self.defects}
        assert not {p["id"] for p in self.plans} & {p["id"] for p in self.pilot["plans"]}
        primary_and_initial = []
        write_targets = []
        briefs = []
        checks = Counter()
        for p in self.plans:
            t, g, selection = p["tuple"], p["grounded_context"], p["record_selection_checks"]
            u = self.users[t["user_id"]]
            ctx = self.ctx(u)
            assert t["role"] == u["role"] == g["authenticated_user"]["role"]
            assert g["authenticated_user"]["id"] == u["id"]
            errors = []
            _validate_expected(p["expected"], p["id"], errors)
            assert not errors, errors
            request = generation_request(p, "gpt-4o-mini")  # builds payload locally, sends nothing
            payload = request["input"][0]["content"]
            assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b|hw3-|orders\.id|dq-order|dq-product|cw-returns|cw-refunds", payload)
            assert "opening_message" not in p and "followups" not in p
            briefs.append(json.dumps(p["generation_brief"], sort_keys=True))
            for policy in g["policies"]:
                assert digest(policy["path"]) == policy["sha256"]
                assert policy["body"] == self.docs[policy["policy_id"]].body
            for key in ("order", "initially_mentioned_order"):
                if key not in g:
                    continue
                o = g[key]
                with self.connection() as c:
                    actual = db.get_order(c, o["id"])
                    assert dict(c.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()) == o
                assert can_view_order(ctx, actual.user_id, actual.store_id)
                query = self.products[o["product_id"]]["title"]
                found = tools.find_order(ctx, query)["orders"]
                assert o["id"] in [r["order_id"] for r in found], (p["id"], o["id"], found)
                if not p["data_quality_case_id"]:
                    assert found[0]["order_id"] == o["id"]
                    assert o["id"] not in self.initial_exclusions
                    primary_and_initial.append(o["id"])
                if key == "order":
                    assert [r["order_id"] for r in found] == selection["returned_order_ids"]
                    calc = self.calculation(o)
                    assert calc == g["return_calculation"] or p["data_quality_case_id"]
                    if not p["data_quality_case_id"]:
                        assert calc["computed_eligible"] == bool(o["refund_eligible"])
                checks["authorized_order_lookups"] += 1
            if "search_products_query" in selection:
                result = tools.search_products(ctx, selection["search_products_query"], selection["search_products_store"])
                assert [r["product_id"] for r in result["products"]] == selection["returned_product_ids"]
                checks["catalogue_lookups"] += 1
            if "product" in g:
                assert g["product"] == self.products[g["product"]["id"]]
            if p["data_quality_case_id"]:
                d = next(d for d in self.defects if d["case_id"] == p["data_quality_case_id"])
                assert p["scenario_group"] == "challenge"
                assert g[d["entity_type"]]["id"] == d["entity_id"]
                assert p["expected"]["outcome"] == d["expected_handling"]
            if p["intended_state_change"] in ("refund", "cancellation"):
                o = g["order"]
                assert (can_refund_order if p["intended_state_change"] == "refund" else can_cancel_order)(ctx, o["user_id"], o["store_id"])
                if p["intended_state_change"] == "refund":
                    r = g["refund_request"]
                    assert 0 < r["amount_cents"] <= o["total_cents"] and self.calculation(o)["computed_eligible"]
                else:
                    assert o["status"] == "placed"
                write_targets.append(o["id"])
            # Attach traceable references for all saved authoritative context.
            g["sources"].append(p["expected"]["source"])
            for policy in g["policies"]:
                g["sources"].append({"type": "policy_document", "reference": policy["path"]})
            checks["valid_generation_briefs"] += 1
        assert len(briefs) == len(set(briefs)), "Duplicate brief"
        assert len(primary_and_initial) == len(set(primary_and_initial)), "Reused ordinary order"
        assert len(write_targets) == len(set(write_targets))
        assert digest(self.source) == self.source_hash
        return dict(checks) | {"unique_ordinary_order_records": len(primary_and_initial),
                              "distinct_refund_or_cancellation_targets": len(write_targets),
                              "all_checks_passed": True, "mode": "offline_read_only_sources",
                              "official_scenario_validator": "pending: customer messages do not exist yet"}

    def save(self, verification):
        summary = {}
        for group in ("coverage", "challenge"):
            ps = [p for p in self.plans if p["scenario_group"] == group]
            summary[group] = {"count": len(ps), "planned_user_messages": sum(p["tuple"]["turn_count"] for p in ps)}
            for key in ["role", "intent", "record_state", "applicable_policy", "tools_needed", "difficulty", "user_style", "turn_count"]:
                summary[group][key] = dict(sorted(Counter(p["tuple"][key] for p in ps).items()))
            summary[group]["planning_family"] = dict(Counter(p["planning_family"] for p in ps))
            summary[group]["intended_state_change"] = dict(Counter(p["intended_state_change"] for p in ps))
        boundary = copy.deepcopy(self.pilot["generation_boundary"])
        boundary["next_gate"] = "Separate generation and independent critic, validate, then 15 student reviews spanning both groups, all roles and every intent. No execution before acceptance."
        data = {"format": "hw3-grounded-final-plan-v1", "purpose": "Planning only; not runnable scenarios or observed outcomes.",
                "planning_date": "2026-10-24", "world_asof": ASOF.isoformat(), "agent_model": "gpt-4o-mini",
                "approved_dimension_plan": self.pilot["approved_dimension_plan"], "generation_boundary": boundary,
                "source_versions": {p: digest(p) for p in [*self.pilot["source_versions"], "agent/tools.py", "agent/db.py", "scenarios/plan_final.py"]},
                "source_snapshot": {"database": str(self.source), "sha256": self.source_hash, "policies": str(self.policy_dir), "policy_hashes": self.manifest["policy_hashes"]},
                "execution_precondition": "Before the separately approved final run, reset mutable data to the seed and verify these selected record snapshots and policy hashes. The current database contains pilot actions.",
                "selection_notes": ["All ordinary order targets, including both correction records, are distinct and exclude pinned/demo and pilot order targets.",
                                    "Damaged-order cases repeat only read-oriented requests. Escalation tickets may be created; no planned refund or cancellation targets these records.",
                                    "Authorization cases request inaccessible scopes without hidden order IDs; they test explanation and restraint, not guaranteed permission-denied tool events.",
                                    "Each generation_brief is isolated from its answer key. Monetary amounts in boundary briefs are customer requests, not revealed thresholds.",
                                    "Five scenarios per defect are intentionally related; the critic must still check natural language diversity and independence of followups."],
                "verification": verification, "coverage_summary": summary,
                "data_quality_counts": dict(Counter(p["data_quality_case_id"] for p in self.plans if p["data_quality_case_id"])),
                "plans": self.plans}
        lines = ["# Final scenario plan coverage", "", "250 grounded plans prepared offline. Customer messages, final human review, and application execution are pending.", "",
                 "Source: clean pre-pilot database snapshot; world date 2026-07-01. Selected agent model: gpt-4o-mini.", "", "## Intent allocation", "",
                 "| Intent | Coverage | Challenge | Total |", "| --- | ---: | ---: | ---: |"]
        intents = sorted({p["tuple"]["intent"] for p in self.plans})
        for intent in intents:
            a = summary["coverage"]["intent"].get(intent, 0)
            b = summary["challenge"]["intent"].get(intent, 0)
            lines.append(f"| {intent} | {a} | {b} | {a+b} |")
        lines += ["| **Total** | **175** | **75** | **250** |", ""]
        for field in ("role", "turn_count", "user_style", "tools_needed", "difficulty"):
            lines += [f"## {field}", "", "| Value | Coverage | Challenge |", "| --- | ---: | ---: |"]
            for value in sorted(set(summary["coverage"][field]) | set(summary["challenge"][field])):
                lines.append(f"| {value} | {summary['coverage'][field].get(value,0)} | {summary['challenge'][field].get(value,0)} |")
            lines.append("")
        lines += ["## Challenge allocation", ""]
        lines += [f"- {k}: {v}" for k, v in summary["challenge"]["planning_family"].items()]
        lines += ["", "## Damaged records", ""] + [f"- {k}: {v}" for k, v in data["data_quality_counts"].items()]
        lines += ["", "## Grounding checks", "", "```json", json.dumps(verification, indent=2), "```", "",
                  f"Planned user messages: {sum(s['planned_user_messages'] for s in summary.values())} (not generated yet).",
                  "", "Refund/cancellation outcomes are expectations, not observed results. The final scenario validator has not run because no final customer messages exist yet.",
                  "", "## Next step", "", "Prepare the separate customer-conversation generation step for approval; then run the independent critic, validate the completed scenarios, and pause for the student's 15 reviews. Preserve the pilot assessments. The final run, monitoring subset, trace export, smoke report, and student video remain pending."]
        OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        REPORT.write_text("\n".join(lines) + "\n")
        print(json.dumps({"files": [str(OUT), str(REPORT)], "verification": verification, "coverage_summary": summary, "data_quality_counts": data["data_quality_counts"]}, indent=2))


def main():
    if OUT.exists() or REPORT.exists():
        raise SystemExit("Refusing to overwrite an existing final plan or coverage report.")
    planner = Planner()
    # Only read tools are called, with all database connections forced to the
    # read-only snapshot for this process. No server, provider, or reset calls.
    with patch.object(db, "connection", planner.connection):
        planner.build_challenge()
        planner.build_coverage()
        verified = planner.verify()
    planner.save(verified)


if __name__ == "__main__":
    main()
