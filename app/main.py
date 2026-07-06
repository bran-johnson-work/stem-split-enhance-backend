import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)

settings = get_settings()

configure_logging(settings.log_level)
# Refuse to boot with insecure config (e.g. missing SECRET_KEY) outside local.
settings.assert_production_ready()

# Surface the Resend sandbox limitation loudly at startup: with the sandbox
# sender, verification emails to any address other than the account owner are
# rejected (403) and — since sends are fire-and-forget — fail silently.
if settings.email_enabled and settings.email_uses_sandbox_sender:
    logging.getLogger(__name__).warning(
        "Email is enabled but EMAIL_FROM is Resend's sandbox sender "
        "(%s); Resend will ONLY deliver to the address that owns the API key. "
        "Verify a domain at resend.com/domains and set EMAIL_FROM to it to send "
        "verification emails to real users.",
        settings.email_from,
    )

app = FastAPI(
    title="AI Audio Stem API",
    docs_url="/docs" if settings.is_local else None,
    redoc_url="/redoc" if settings.is_local else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Safety net: turn an uncaught DB constraint violation into a 409, not a 500.

    Services translate the constraints they know about (e.g. duplicate email)
    into clean errors; this catches anything that slips through so the client
    gets a sane status and the detail (which can leak schema internals) is only
    logged, never returned.
    """
    logger.warning("Unhandled IntegrityError on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "The request conflicts with the current state of the resource."},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
