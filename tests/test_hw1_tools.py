"""Additional offline contract and permission checks for the HW1 tools."""

from types import SimpleNamespace

import pytest

from agent import db, tools
from agent.auth import AuthContext


SHOPPER = AuthContext(user_id=1, role="shopper")


def test_policy_ids_are_exact(world):
    result = tools.get_policy(SHOPPER, "CW-RETURNS")
    assert result["error"] == "not_found"
    assert "CW-RETURNS" in result["reason"]
    result = tools.get_policy(SHOPPER, "cw-returns")
    assert set(result) == {"ok", "policy_id", "title", "audience", "body"}


def test_product_filters_sorting_and_limits(world_copy):
    with db.connection() as conn:
        products = db.list_products(conn, store_id=1)[:30]
        for index, product in enumerate(products):
            conn.execute(
                "UPDATE products SET title = ?, description = ?, price_cents = ? WHERE id = ?",
                ("ContractFixture Mug", "handmade blue", 1000 if index < 2 else 2000, product.id),
            )
        conn.commit()
        store = db.get_store(conn, 1)
    result = tools.search_products(
        SHOPPER, "  CONTRACTFIXTURE blue  ", store.slug.upper(), max_price_usd=10, limit=99
    )
    assert result["count"] == 2
    assert result["products"] == [
        {"product_id": p.id, "store_id": 1, "title": "ContractFixture Mug", "price_usd": 10.0}
        for p in products[:2]
    ]
    assert tools.search_products(SHOPPER, "ContractFixture", limit=99)["count"] == 25
    assert tools.search_products(SHOPPER, "ContractFixture", limit=0)["count"] == 1
    assert tools.search_products(SHOPPER, "ContractFixture absent") == {
        "ok": True, "products": [], "count": 0
    }


@pytest.mark.parametrize("price", [0, -1, float("nan")])
def test_product_search_rejects_nonpositive_ceiling(price):
    assert tools.search_products(SHOPPER, "mug", max_price_usd=price)["error"] == "invalid_argument"


@pytest.mark.parametrize("role", ["shopper", "merchant", "support"])
def test_cancellation_scope_precedes_status_and_writes(world_copy, role):
    with db.connection() as conn:
        order = db.get_order(conn, 4127)
    owner = AuthContext(user_id=order.user_id, role=role, store_id=order.store_id)
    if role != "support":
        stranger = AuthContext(user_id=order.user_id + 1, role=role, store_id=order.store_id + 1)
        denied = tools.cancel_order(stranger, order.id, "cancel")
        assert denied["error"] == "permission_denied"
        assert order.status not in denied["reason"]
        assert role in denied["reason"] and str(order.id) in denied["reason"]
    rejected = tools.cancel_order(owner, order.id, "cancel")
    assert rejected["error"] == "not_eligible"
    assert order.status in rejected["reason"]
    with db.connection() as conn:
        assert db.get_order(conn, order.id).status == order.status
        db.set_order_status(conn, order.id, "placed")
    assert tools.cancel_order(owner, order.id, "cancel")["status"] == "cancelled"
    with db.connection() as conn:
        assert db.get_order(conn, order.id).status == "cancelled"


def test_cancellation_kill_switch_precedes_database(monkeypatch):
    monkeypatch.setenv("CARTWHEEL_KILL_SWITCH", "readonly")

    def forbidden():
        pytest.fail("paused cancellation must not open the database")

    monkeypatch.setattr(db, "connection", forbidden)
    assert tools.cancel_order(SHOPPER, 4127, "cancel")["error"] == "paused"


@pytest.mark.parametrize("role,user_id,store_id", [
    ("unknown", 1, None), ("shopper", None, None),
    ("merchant", 9001, None), ("support", None, None),
])
def test_find_order_rejects_invalid_identity_before_database(monkeypatch, role, user_id, store_id):
    def forbidden():
        pytest.fail("invalid search scope must not open the database")

    monkeypatch.setattr(db, "connection", forbidden)
    ctx = SimpleNamespace(role=role, user_id=user_id, store_id=store_id)
    assert tools.find_order(ctx, "product")["error"] == "permission_denied"


def test_find_order_substrings_and_embedded_product_name(order_search_cases):
    title, expected = order_search_cases
    for query in ("search FIXTURE", f"the {title.upper()} I bought last week"):
        result = tools.find_order(SHOPPER, query)
        assert [order["order_id"] for order in result["orders"]] == expected["shopper"]
    assert tools.find_order(SHOPPER, "   ") == {"ok": True, "orders": []}
