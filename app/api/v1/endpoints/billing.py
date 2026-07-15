import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.core.stripe import SignatureVerificationError, StripeError
from app.models.user import User
from app.schemas.billing import (
    CancelRequest,
    CheckoutRequest,
    CheckoutResponse,
    PlanResponse,
    PortalResponse,
    SubscriptionResponse,
)
from app.services.billing_service import (
    BillingDisabledError,
    BillingRequestError,
    PlanNotFoundError,
    billing_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/plans", response_model=list[PlanResponse])
async def list_plans(db: AsyncSession = Depends(get_db)) -> Any:
    """Public: list active plans for the pricing page."""
    return await billing_service.list_plans(db)


@router.get("/subscription", response_model=SubscriptionResponse | None)
async def get_my_subscription(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Current user's effective subscription.

    Falls back to the free plan (``has_subscription=false``) when the user has no
    subscription row, so the client always receives a plan to display.
    """
    return await billing_service.get_subscription_view(db, current_user.id)


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    payload: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Start a Stripe Checkout Session; the client redirects to ``checkout_url``."""
    try:
        url = await billing_service.create_checkout_session(
            db,
            user=current_user,
            plan_name=payload.plan_name.value,
            interval=payload.interval.value,
        )
    except BillingDisabledError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing is not available.")
    except PlanNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except BillingRequestError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except StripeError:
        logger.exception("Stripe checkout failed for user %s", current_user.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payment provider error.")
    return CheckoutResponse(checkout_url=url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Open the Stripe Customer Portal to manage/cancel the subscription."""
    try:
        url = await billing_service.create_portal_session(db, user=current_user)
    except BillingDisabledError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing is not available.")
    except BillingRequestError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except StripeError:
        logger.exception("Stripe portal failed for user %s", current_user.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payment provider error.")
    return PortalResponse(portal_url=url)


@router.post("/cancel", response_model=SubscriptionResponse)
async def cancel_subscription(
    payload: CancelRequest = CancelRequest(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Cancel the caller's subscription (native in-app Cancel button).

    Defaults to end-of-period so the user keeps paid access until it lapses;
    ``{"immediate": true}`` cancels now. Returns the updated subscription.
    """
    try:
        subscription = await billing_service.cancel_subscription(
            db, user=current_user, immediate=payload.immediate
        )
    except BillingDisabledError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing is not available.")
    except BillingRequestError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except StripeError:
        logger.exception("Stripe cancel failed for user %s", current_user.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payment provider error.")
    return subscription


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Stripe webhook receiver — the source of truth for subscription state.

    Verifies the signature over the **raw** request body (parsing it first would
    break verification), so no auth dependency is used here.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        return await billing_service.handle_webhook(db, payload=payload, sig_header=sig_header)
    except SignatureVerificationError as exc:
        logger.warning("Rejected Stripe webhook: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature.")
    except BillingDisabledError:
        # Pretend the route doesn't exist when billing isn't configured.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
