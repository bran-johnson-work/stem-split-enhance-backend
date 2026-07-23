"""Billing / Stripe subscription tests — fully mocked, no DB or network.

Covers webhook signature verification, the event->subscription state machine,
and the endpoint layer (auth, error mapping, idempotency).
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.dependencies import get_current_user
from app.core import stripe as stripe_mod
from app.core.config import get_settings
from app.main import app
from app.models.plan import Plan
from app.models.subscription import Subscription, SubscriptionStatus
from app.schemas.billing import PlanResponse, SubscriptionResponse
from app.services import billing_service as bsvc
from app.services.billing_service import (
    BillingRequestError,
    BillingService,
    PlanNotFoundError,
)

WEBHOOK_SECRET = "whsec_test_secret"


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(event_type: str, obj: dict, event_id: str = "evt_test_1") -> bytes:
    return json.dumps(
        {"id": event_id, "type": event_type, "data": {"object": obj}}
    ).encode()


# --- Signature verification (pure) ----------------------------------------


def test_construct_event_accepts_valid_signature():
    payload = _event("ping", {})
    event = stripe_mod.construct_event(payload, _sign(payload), WEBHOOK_SECRET)
    assert event["type"] == "ping"


def test_construct_event_rejects_bad_signature():
    payload = _event("ping", {})
    header = "t=%d,v1=deadbeef" % int(time.time())
    with pytest.raises(stripe_mod.SignatureVerificationError):
        stripe_mod.construct_event(payload, header, WEBHOOK_SECRET)


def test_construct_event_rejects_missing_header():
    with pytest.raises(stripe_mod.SignatureVerificationError):
        stripe_mod.construct_event(b"{}", None, WEBHOOK_SECRET)


def test_construct_event_rejects_stale_timestamp():
    payload = _event("ping", {})
    old = _sign(payload, timestamp=int(time.time()) - 10_000)
    with pytest.raises(stripe_mod.SignatureVerificationError):
        stripe_mod.construct_event(payload, old, WEBHOOK_SECRET)


def test_construct_event_rejects_tampered_payload():
    payload = _event("ping", {})
    header = _sign(payload)
    with pytest.raises(stripe_mod.SignatureVerificationError):
        stripe_mod.construct_event(payload + b" ", header, WEBHOOK_SECRET)


# --- Helpers --------------------------------------------------------------


def test_status_map_handles_stripe_spelling():
    assert bsvc._map_status("canceled") is SubscriptionStatus.CANCELLED
    assert bsvc._map_status("active") is SubscriptionStatus.ACTIVE
    assert bsvc._map_status("past_due") is SubscriptionStatus.PAST_DUE
    assert bsvc._map_status("unknown") is SubscriptionStatus.INCOMPLETE


def test_ts_converts_unix_to_aware_datetime():
    dt = bsvc._ts(1_700_000_000)
    assert dt == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert dt.tzinfo is timezone.utc
    # 0 / None are treated as "absent" (a real Stripe period is never epoch 0).
    assert bsvc._ts(None) is None
    assert bsvc._ts(0) is None


def test_first_price_id_and_user_id_extraction():
    sub = {"items": {"data": [{"price": {"id": "price_x"}}]}}
    assert bsvc._first_price_id(sub) == "price_x"
    assert bsvc._first_price_id({}) is None

    uid = uuid.uuid4()
    assert bsvc._extract_user_id({"metadata": {"user_id": str(uid)}}) == uid
    assert bsvc._extract_user_id({"client_reference_id": str(uid)}) == uid
    assert bsvc._extract_user_id({"metadata": {"user_id": "not-a-uuid"}}) is None


# --- Webhook state machine ------------------------------------------------


async def test_subscription_upsert_creates_when_absent(fake_db, monkeypatch):
    user_id = uuid.uuid4()
    plan = MagicMock()
    plan.id = uuid.uuid4()

    monkeypatch.setattr(
        bsvc.plan_repository, "get_by_stripe_price_id", AsyncMock(return_value=plan)
    )
    sub_repo = MagicMock()
    sub_repo.get_by_stripe_subscription_id = AsyncMock(return_value=None)
    sub_repo.get_by_stripe_customer_id = AsyncMock(return_value=None)
    sub_repo.get_for_user = AsyncMock(return_value=None)
    sub_repo.create = AsyncMock()
    monkeypatch.setattr(bsvc, "subscription_repository", sub_repo)

    sub_obj = {
        "object": "subscription",
        "id": "sub_123",
        "customer": "cus_123",
        "status": "active",
        "current_period_start": 1_700_000_000,
        "current_period_end": 1_702_000_000,
        "items": {"data": [{"price": {"id": "price_x"}}]},
        "metadata": {"user_id": str(user_id)},
    }
    await BillingService()._on_subscription_upsert(fake_db, sub_obj)

    sub_repo.create.assert_awaited_once()
    created = sub_repo.create.await_args.args[1]
    assert created.user_id == user_id
    assert created.plan_id == plan.id
    assert created.status is SubscriptionStatus.ACTIVE
    assert created.stripe_subscription_id == "sub_123"


async def test_subscription_upsert_updates_existing(fake_db, monkeypatch):
    plan = MagicMock()
    plan.id = uuid.uuid4()
    existing = MagicMock()

    monkeypatch.setattr(
        bsvc.plan_repository, "get_by_stripe_price_id", AsyncMock(return_value=plan)
    )
    sub_repo = MagicMock()
    sub_repo.get_by_stripe_subscription_id = AsyncMock(return_value=existing)
    sub_repo.update = AsyncMock()
    sub_repo.create = AsyncMock()
    monkeypatch.setattr(bsvc, "subscription_repository", sub_repo)

    sub_obj = {
        "object": "subscription",
        "id": "sub_123",
        "customer": "cus_123",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_x"}}]},
    }
    await BillingService()._on_subscription_upsert(fake_db, sub_obj)

    sub_repo.update.assert_awaited_once()
    sub_repo.create.assert_not_awaited()
    fields = sub_repo.update.await_args.args[2]
    assert fields["status"] is SubscriptionStatus.ACTIVE
    assert fields["plan_id"] == plan.id


async def test_webhook_dedupes_duplicate_event(fake_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)

    event_repo = MagicMock()
    event_repo.get_by_stripe_event_id = AsyncMock(return_value=MagicMock())  # already seen
    event_repo.create = AsyncMock()
    monkeypatch.setattr(bsvc, "billing_event_repository", event_repo)

    payload = _event("customer.subscription.updated", {"id": "sub_1"})
    result = await BillingService().handle_webhook(
        fake_db, payload=payload, sig_header=_sign(payload)
    )
    assert result["status"] == "duplicate"
    event_repo.create.assert_not_awaited()


# --- Endpoints ------------------------------------------------------------


@pytest.fixture
def auth_client(client, sample_user):
    app.dependency_overrides[get_current_user] = lambda: sample_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def test_list_plans_is_public(client, monkeypatch):
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.name = "creator"
    plan.display_name = "Creator"
    plan.price_monthly_cents = 1200
    plan.price_annual_cents = 12000
    plan.max_projects_per_month = 50
    plan.max_files_per_month = 50
    plan.max_file_size_mb = 100
    plan.features = {"bullets": ["50 uploads / mo"]}
    plan.is_active = True
    monkeypatch.setattr(
        bsvc.plan_repository, "list_active", AsyncMock(return_value=[plan])
    )
    resp = await client.get("/api/v1/billing/plans")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "creator"


def _plan_orm(name: str = "free", **overrides) -> Plan:
    defaults = dict(
        id=uuid.uuid4(),
        name=name,
        display_name=name.title(),
        price_monthly_cents=0,
        price_annual_cents=0,
        max_projects_per_month=3,
        max_file_size_mb=20,
        features={"bullets": ["3 uploads"]},
        is_active=True,
    )
    defaults.update(overrides)
    return Plan(**defaults)


async def test_subscription_defaults_to_free_when_none(auth_client, monkeypatch):
    """A user with no subscription row is reported as free, not null."""
    monkeypatch.setattr(
        bsvc.subscription_repository, "get_for_user", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        bsvc.plan_repository, "get_by_name", AsyncMock(return_value=_plan_orm("free"))
    )
    resp = await auth_client.get("/api/v1/billing/subscription")
    assert resp.status_code == 200
    body = resp.json()
    assert body is not None
    assert body["has_subscription"] is False
    assert body["status"] is None
    assert body["plan"]["name"] == "free"


async def test_subscription_reports_active_row(auth_client, monkeypatch):
    plan = _plan_orm("pro", price_monthly_cents=2900, max_projects_per_month=None)
    subscription = Subscription(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=False,
        cancelled_at=None,
    )
    subscription.plan = plan
    monkeypatch.setattr(
        bsvc.subscription_repository, "get_for_user", AsyncMock(return_value=subscription)
    )
    resp = await auth_client.get("/api/v1/billing/subscription")
    body = resp.json()
    assert body["has_subscription"] is True
    assert body["status"] == "active"
    assert body["plan"]["name"] == "pro"


async def test_checkout_disabled_returns_503(auth_client, monkeypatch):
    # Default settings have no Stripe key => billing disabled.
    resp = await auth_client.post(
        "/api/v1/billing/checkout", json={"plan_name": "pro", "interval": "monthly"}
    )
    assert resp.status_code == 503


async def test_checkout_unknown_plan_returns_422(auth_client, monkeypatch):
    # ``plan_name`` is now a ``PlanName`` enum, so an unknown value is rejected at
    # request validation before the service runs.
    called = AsyncMock(side_effect=PlanNotFoundError("Unknown plan 'ghost'."))
    monkeypatch.setattr(bsvc.billing_service, "create_checkout_session", called)
    resp = await auth_client.post(
        "/api/v1/billing/checkout", json={"plan_name": "ghost", "interval": "monthly"}
    )
    assert resp.status_code == 422
    called.assert_not_awaited()


async def test_checkout_free_plan_returns_400(auth_client, monkeypatch):
    monkeypatch.setattr(
        bsvc.billing_service,
        "create_checkout_session",
        AsyncMock(side_effect=BillingRequestError("The free plan does not require checkout.")),
    )
    resp = await auth_client.post(
        "/api/v1/billing/checkout", json={"plan_name": "free", "interval": "monthly"}
    )
    assert resp.status_code == 400


async def test_checkout_happy_path_returns_url(auth_client, monkeypatch):
    monkeypatch.setattr(
        bsvc.billing_service,
        "create_checkout_session",
        AsyncMock(return_value="https://checkout.stripe.com/c/pay/cs_test_123"),
    )
    resp = await auth_client.post(
        "/api/v1/billing/checkout", json={"plan_name": "pro", "interval": "annual"}
    )
    assert resp.status_code == 200
    assert resp.json()["checkout_url"].startswith("https://checkout.stripe.com/")


async def test_portal_without_account_returns_400(auth_client, monkeypatch):
    monkeypatch.setattr(
        bsvc.billing_service,
        "create_portal_session",
        AsyncMock(side_effect=BillingRequestError("No billing account exists for this user yet.")),
    )
    resp = await auth_client.post("/api/v1/billing/portal")
    assert resp.status_code == 400


async def test_cancel_calls_stripe_at_period_end(fake_db, monkeypatch):
    existing = MagicMock()
    existing.stripe_subscription_id = "sub_1"
    sub_repo = MagicMock()
    sub_repo.get_for_user = AsyncMock(return_value=existing)
    monkeypatch.setattr(bsvc, "subscription_repository", sub_repo)

    cancel_mock = AsyncMock(return_value={"object": "subscription", "id": "sub_1"})
    monkeypatch.setattr(bsvc.stripe, "cancel_subscription_at_period_end", cancel_mock)
    monkeypatch.setattr(BillingService, "_on_subscription_upsert", AsyncMock())
    monkeypatch.setattr(BillingService, "get_subscription_view", AsyncMock(return_value=None))
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test")

    user = MagicMock()
    user.id = uuid.uuid4()
    await BillingService().cancel_subscription(fake_db, user=user, immediate=False)

    cancel_mock.assert_awaited_once_with(subscription_id="sub_1")


async def test_cancel_without_subscription_raises(fake_db, monkeypatch):
    sub_repo = MagicMock()
    sub_repo.get_for_user = AsyncMock(return_value=None)
    monkeypatch.setattr(bsvc, "subscription_repository", sub_repo)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test")

    user = MagicMock()
    user.id = uuid.uuid4()
    with pytest.raises(BillingRequestError):
        await BillingService().cancel_subscription(fake_db, user=user)


async def test_cancel_endpoint_returns_updated_subscription(auth_client, monkeypatch):
    view = SubscriptionResponse(
        has_subscription=True,
        status=SubscriptionStatus.ACTIVE,
        plan=PlanResponse.model_validate(
            _plan_orm("pro", price_monthly_cents=2900, max_projects_per_month=None)
        ),
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=True,
        cancelled_at=None,
    )
    monkeypatch.setattr(
        bsvc.billing_service, "cancel_subscription", AsyncMock(return_value=view)
    )
    resp = await auth_client.post("/api/v1/billing/cancel", json={"immediate": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancel_at_period_end"] is True
    assert body["status"] == "active"
    assert body["plan"]["name"] == "pro"
    assert body["has_subscription"] is True


async def test_cancel_endpoint_no_account_returns_400(auth_client, monkeypatch):
    monkeypatch.setattr(
        bsvc.billing_service,
        "cancel_subscription",
        AsyncMock(side_effect=BillingRequestError("No active subscription to cancel.")),
    )
    resp = await auth_client.post("/api/v1/billing/cancel", json={"immediate": False})
    assert resp.status_code == 400


async def test_webhook_bad_signature_returns_400(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=b'{"id":"evt_1"}',
        headers={"stripe-signature": "t=1,v1=bad"},
    )
    assert resp.status_code == 400


async def test_webhook_disabled_returns_404(client):
    # No Stripe key configured => webhook route behaves as not found.
    resp = await client.post(
        "/api/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "x"}
    )
    assert resp.status_code == 404


# ---- invoices (unit: mapping) ---------------------------------------------


def _stripe_invoice(**overrides) -> dict:
    """A minimal Stripe invoice object, shaped like the real API response."""
    inv = {
        "id": "in_test_123",
        "created": 1_752_000_000,
        "total": 7900,
        "currency": "usd",
        "status": "paid",
        "description": None,
        "hosted_invoice_url": "https://invoice.stripe.com/i/acct_1/test",
        "invoice_pdf": "https://invoice.stripe.com/i/acct_1/test.pdf",
        "lines": {"data": [{"description": "Pro — monthly"}]},
    }
    inv.update(overrides)
    return inv


def test_map_invoice_converts_cents_to_dollars_and_status():
    result = bsvc._map_invoice(_stripe_invoice())
    assert result.amount == 79.0  # 7900 cents
    assert result.currency == "USD"
    assert result.status == "paid"
    assert result.invoice_pdf.endswith(".pdf")


def test_map_invoice_falls_back_to_line_item_description():
    result = bsvc._map_invoice(_stripe_invoice(description=None))
    assert result.description == "Pro — monthly"


def test_map_invoice_prefers_top_level_description():
    result = bsvc._map_invoice(_stripe_invoice(description="Manual charge"))
    assert result.description == "Manual charge"


def test_map_invoice_status_normalises_unpayable_to_failed():
    assert bsvc._map_invoice(_stripe_invoice(status="void")).status == "failed"
    assert bsvc._map_invoice(_stripe_invoice(status="uncollectible")).status == "failed"
    assert bsvc._map_invoice(_stripe_invoice(status="open")).status == "open"


def test_draft_invoices_are_hidden():
    assert bsvc._is_listable_invoice(_stripe_invoice(status="draft")) is False
    assert bsvc._is_listable_invoice(_stripe_invoice(status="paid")) is True


# ---- invoices (service + endpoint) ----------------------------------------


async def test_list_invoices_empty_without_billing(auth_client, monkeypatch):
    """Free users / billing disabled → an empty list, not an error."""
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    resp = await auth_client.get("/api/v1/billing/invoices")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_invoices_empty_without_customer(auth_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test")
    monkeypatch.setattr(
        bsvc.subscription_repository, "get_for_user", AsyncMock(return_value=None)
    )
    resp = await auth_client.get("/api/v1/billing/invoices")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_invoices_returns_mapped_stripe_data(auth_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test")

    sub = MagicMock()
    sub.stripe_customer_id = "cus_test_1"
    monkeypatch.setattr(
        bsvc.subscription_repository, "get_for_user", AsyncMock(return_value=sub)
    )
    monkeypatch.setattr(
        bsvc.stripe,
        "list_invoices",
        AsyncMock(return_value=[_stripe_invoice(), _stripe_invoice(status="draft")]),
    )

    resp = await auth_client.get("/api/v1/billing/invoices")
    assert resp.status_code == 200
    body = resp.json()
    # The draft is filtered out; the paid one is mapped.
    assert len(body) == 1
    assert body[0]["amount"] == 79.0
    assert body[0]["status"] == "paid"
    assert body[0]["hosted_invoice_url"].startswith("https://invoice.stripe.com/")


async def test_list_invoices_maps_stripe_outage_to_502(auth_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test")
    sub = MagicMock()
    sub.stripe_customer_id = "cus_test_1"
    monkeypatch.setattr(
        bsvc.subscription_repository, "get_for_user", AsyncMock(return_value=sub)
    )
    monkeypatch.setattr(
        bsvc.stripe,
        "list_invoices",
        AsyncMock(side_effect=stripe_mod.StripeError("boom")),
    )
    resp = await auth_client.get("/api/v1/billing/invoices")
    assert resp.status_code == 502
