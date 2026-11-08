"""Tier catalogue + per-tier enforcement tests.

Two things are verified here:

1. **Catalogue integrity** — the seeded plans (free / creator / pro / studio)
   are all distinct and match the agreed pricing table exactly. The expected
   values below are the *contract*; if a migration ever makes two tiers
   identical or drifts from the table, these tests fail. Data is read straight
   from the reseed migration's ``PLANS`` list so the test tracks what actually
   ships.
2. **Per-tier enforcement** — the app hard-enforces two plan limits, tested per
   tier here:
   - ``max_projects_per_month`` (``ProjectService._enforce_project_quota``): free
     blocks the 4th project, creator the 51st, pro/studio (unlimited) never block.
   - ``max_file_size_mb`` (``ProjectService._enforce_file_size``): each tier
     accepts a file at its limit and rejects one a byte over.

NOTE (deliberately *not* tested, because the behaviour does not exist yet):
the ``features`` flags (priority_processing, api_access, team_seats,
stem_quality, download_format, enhanced_master) are *advertised* on the plan but
not enforced anywhere in application code. Writing "only studio gets api_access"
tests today would assert behaviour no code implements. When enforcement is
added, add its tests alongside it — see ``test_feature_flags_form_a_capability_ladder``
which pins the *intended* ladder so the data is ready.
"""

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.plan import PlanName
from app.models.subscription import SubscriptionStatus
from app.services import project_service as project_service_module
from app.services.project_service import ProjectService

# --- Load the seed catalogue straight from the migration -------------------

def _load_seed_plans() -> dict[str, dict]:
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "d9b2e5f3c7a4_reseed_klearnote_plans.py"
    )
    spec = importlib.util.spec_from_file_location("reseed_klearnote_plans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {plan["name"]: plan for plan in module.PLANS}


SEED = _load_seed_plans()

# The agreed pricing table (your screenshot). This is the source of truth the
# seed must satisfy — prices in cents, ``None`` = unlimited.
EXPECTED = {
    "free": {
        "display_name": "Trial",
        "price_monthly_cents": 0,
        "price_annual_cents": 0,
        "max_projects_per_month": 3,
        "max_file_size_mb": 20,
    },
    "creator": {
        "display_name": "Creator",
        "price_monthly_cents": 1200,
        "price_annual_cents": 12000,
        "max_projects_per_month": 50,
        "max_file_size_mb": 100,
    },
    "pro": {
        "display_name": "Pro",
        "price_monthly_cents": 2900,
        "price_annual_cents": 29000,
        "max_projects_per_month": None,
        "max_file_size_mb": 300,
    },
    "studio": {
        "display_name": "Studio",
        "price_monthly_cents": 7900,
        "price_annual_cents": 79000,
        "max_projects_per_month": None,
        "max_file_size_mb": 500,
    },
}

# free -> studio, cheapest to most expensive.
TIER_ORDER = ["free", "creator", "pro", "studio"]


# --- Catalogue integrity ----------------------------------------------------


def test_all_expected_tiers_are_seeded():
    assert set(SEED) == set(EXPECTED)


@pytest.mark.parametrize("name", TIER_ORDER)
def test_tier_matches_pricing_table(name):
    """Every seeded tier equals the agreed table, field by field."""
    plan = SEED[name]
    for field, value in EXPECTED[name].items():
        assert plan[field] == value, f"{name}.{field} = {plan[field]!r}, expected {value!r}"


def test_tiers_are_all_distinct():
    """No two tiers collapse into the same offering."""
    names = [SEED[n]["name"] for n in TIER_ORDER]
    display_names = [SEED[n]["display_name"] for n in TIER_ORDER]
    price_pairs = [
        (SEED[n]["price_monthly_cents"], SEED[n]["price_annual_cents"]) for n in TIER_ORDER
    ]
    assert len(set(names)) == 4
    assert len(set(display_names)) == 4
    assert len(set(price_pairs)) == 4  # each tier has a unique price point


def test_prices_strictly_increase_up_the_ladder():
    monthly = [SEED[n]["price_monthly_cents"] for n in TIER_ORDER]
    annual = [SEED[n]["price_annual_cents"] for n in TIER_ORDER]
    assert monthly == sorted(monthly) and len(set(monthly)) == 4
    assert annual == sorted(annual) and len(set(annual)) == 4


def test_annual_is_cheaper_than_12x_monthly_for_paid_tiers():
    """Annual billing must be a discount, never a surcharge, on paid tiers."""
    for name in ["creator", "pro", "studio"]:
        plan = SEED[name]
        assert plan["price_annual_cents"] < plan["price_monthly_cents"] * 12


def test_planname_enum_matches_seeded_tiers():
    """The API enum the frontend binds to is exactly the seeded catalogue."""
    assert {member.value for member in PlanName} == set(EXPECTED)


def test_feature_flags_form_a_capability_ladder():
    """Pin the *intended* capability escalation across tiers.

    This documents the ladder in the seed's ``features`` JSON. It is a data
    contract only — none of these flags are enforced by app code yet.
    """
    feat = {n: SEED[n]["features"] for n in TIER_ORDER}

    # Enhanced master: off on free, on for every paid tier.
    assert feat["free"]["enhanced_master"] is False
    assert all(feat[n]["enhanced_master"] is True for n in ["creator", "pro", "studio"])

    # Priority processing: pro and studio only.
    assert feat["free"]["priority_processing"] is False
    assert feat["creator"]["priority_processing"] is False
    assert feat["pro"]["priority_processing"] is True
    assert feat["studio"]["priority_processing"] is True

    # Team seats + API access: studio only (the top tier).
    for n in ["free", "creator", "pro"]:
        assert feat[n]["team_seats"] is False
        assert feat[n]["api_access"] is False
    assert feat["studio"]["team_seats"] is True
    assert feat["studio"]["api_access"] is True

    # Every tier advertises a distinct feature set (serialize: values include lists).
    serialized = [json.dumps(feat[n], sort_keys=True) for n in TIER_ORDER]
    assert len(set(serialized)) == 4


# --- Per-tier quota enforcement (the one real behavioural difference) -------


def _quota_service(monkeypatch, *, subscription, plan, used):
    """A service running the real quota method with mocked plan/subscription repos."""
    svc = ProjectService()
    svc.repository = AsyncMock()
    svc.repository.count_for_user_since.return_value = used

    sub_repo = AsyncMock()
    sub_repo.get_for_user.return_value = subscription
    plan_repo = AsyncMock()
    plan_repo.get_by_name.return_value = plan
    monkeypatch.setattr(project_service_module, "subscription_repository", sub_repo)
    monkeypatch.setattr(project_service_module, "plan_repository", plan_repo)
    return svc


def _active_subscription(max_projects):
    return SimpleNamespace(
        status=SubscriptionStatus.ACTIVE,
        current_period_start=datetime.now(timezone.utc),
        plan=SimpleNamespace(max_projects_per_month=max_projects),
    )


# Tiers whose cap is a finite number the app enforces.
FINITE_TIERS = [(n, EXPECTED[n]["max_projects_per_month"]) for n in TIER_ORDER
                if EXPECTED[n]["max_projects_per_month"] is not None]
UNLIMITED_TIERS = [n for n in TIER_ORDER if EXPECTED[n]["max_projects_per_month"] is None]


@pytest.mark.parametrize("name,limit", FINITE_TIERS)
async def test_tier_blocks_at_its_project_limit(name, limit, fake_db, monkeypatch):
    """A finite tier rejects the (limit+1)-th project in the current window."""
    if name == "free":
        # Free tier has no subscription: enforcement falls back to the free plan.
        svc = _quota_service(
            monkeypatch,
            subscription=None,
            plan=SimpleNamespace(max_projects_per_month=limit),
            used=limit,
        )
    else:
        svc = _quota_service(
            monkeypatch, subscription=_active_subscription(limit), plan=None, used=limit
        )
    with pytest.raises(ValueError, match=rf"reached \({limit}\)"):
        await svc._enforce_project_quota(fake_db, uuid.uuid4())


@pytest.mark.parametrize("name,limit", FINITE_TIERS)
async def test_tier_allows_just_under_its_limit(name, limit, fake_db, monkeypatch):
    if name == "free":
        svc = _quota_service(
            monkeypatch,
            subscription=None,
            plan=SimpleNamespace(max_projects_per_month=limit),
            used=limit - 1,
        )
    else:
        svc = _quota_service(
            monkeypatch, subscription=_active_subscription(limit), plan=None, used=limit - 1
        )
    await svc._enforce_project_quota(fake_db, uuid.uuid4())  # no raise


@pytest.mark.parametrize("name", UNLIMITED_TIERS)
async def test_unlimited_tier_never_blocks(name, fake_db, monkeypatch):
    """pro / studio are unlimited: no cap, and the COUNT query is skipped entirely."""
    svc = _quota_service(
        monkeypatch, subscription=_active_subscription(None), plan=None, used=10_000
    )
    await svc._enforce_project_quota(fake_db, uuid.uuid4())  # no raise
    svc.repository.count_for_user_since.assert_not_called()


# --- Per-tier file-size enforcement ----------------------------------------

FILE_SIZE_TIERS = [(n, EXPECTED[n]["max_file_size_mb"]) for n in TIER_ORDER]


@pytest.mark.parametrize("name,limit_mb", FILE_SIZE_TIERS)
def test_tier_accepts_file_at_its_size_limit(name, limit_mb):
    """A file exactly at the tier's limit is allowed (inclusive boundary)."""
    from app.services.project_service import ProjectService

    plan = SimpleNamespace(max_file_size_mb=limit_mb)
    ProjectService()._enforce_file_size(plan, limit_mb * 1024 * 1024)  # no raise


@pytest.mark.parametrize("name,limit_mb", FILE_SIZE_TIERS)
def test_tier_rejects_file_over_its_size_limit(name, limit_mb):
    """One byte over the tier's limit is rejected with FileTooLargeError."""
    from app.services.project_service import FileTooLargeError, ProjectService

    plan = SimpleNamespace(max_file_size_mb=limit_mb)
    with pytest.raises(FileTooLargeError, match=rf"{limit_mb} MB"):
        ProjectService()._enforce_file_size(plan, limit_mb * 1024 * 1024 + 1)


def test_file_size_limits_are_distinct_across_tiers():
    """Each tier enforces a different file-size cap (real behavioural difference)."""
    caps = [EXPECTED[n]["max_file_size_mb"] for n in TIER_ORDER]
    assert caps == sorted(caps) and len(set(caps)) == 4
