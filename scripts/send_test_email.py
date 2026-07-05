"""Send a real email through Resend to verify the email configuration end-to-end.

Reads the Resend settings from ``.env`` (via ``app.core.config``) and sends one
message, printing the provider message id on success. This is a live network
call — unlike the test suite, which never touches the network.

    # Send to the address that owns your Resend account (works with the sandbox
    # sender onboarding@resend.dev even before you verify a domain):
    python -m scripts.send_test_email you@example.com

Exit code is non-zero if email is not configured or the send fails.
"""

import asyncio
import sys

from app.core.config import get_settings
from app.core.email import EmailError, send_email


async def _run(recipient: str) -> int:
    settings = get_settings()
    if not settings.email_enabled:
        print("[error] RESEND_API_KEY is not set in .env — email is disabled.")
        return 1

    print(f"[info] sending test email from {settings.email_sender!r} to {recipient!r} ...")
    try:
        message_id = await send_email(
            to=recipient,
            subject="AI Audio Stem — email configuration test",
            html=(
                "<h2>It works! ✅</h2>"
                "<p>Your Resend integration is configured correctly. "
                "Transactional emails (like account verification) will now be "
                "delivered.</p>"
            ),
            text="It works! Your Resend integration is configured correctly.",
        )
    except EmailError as exc:
        print(f"[error] send failed: {exc}")
        return 1

    print(f"[ok] email accepted by Resend (message id: {message_id})")
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.send_test_email <recipient-email>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_run(sys.argv[1])))


if __name__ == "__main__":
    main()
