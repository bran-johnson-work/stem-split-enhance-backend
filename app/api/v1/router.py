from fastapi import APIRouter

from app.api.v1.endpoints import admin, auth, billing, jobs, projects, restoration, users
from app.core.config import get_settings

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
# Processing-job routes hang off the same /projects prefix (…/{project_id}/process, …/jobs).
api_router.include_router(jobs.router, prefix="/projects", tags=["jobs"])
# V2/V3 audio restoration, proxied to a separate GPU service. Mounted at the root
# because it spans two shapes (/restoration/status and /projects/{id}/restoration).
api_router.include_router(restoration.router, tags=["restoration"])
# Admin read-only utility (SOW 1.G); every route guarded by require_role(ADMIN).
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
# Billing / subscriptions (Stripe). /billing/webhook is the source of truth.
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])

# Local-only developer utilities (billing simulation for Swagger testing without
# the Stripe CLI). Conditionally mounted so these routes never exist in prod.
if get_settings().is_local:
    from app.api.v1.endpoints import dev

    api_router.include_router(dev.router, prefix="/dev", tags=["dev (local only)"])
