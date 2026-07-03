import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.services.auth_service import auth_service


async def test_logout_records_revocation_and_commits(fake_db, monkeypatch):
    add = AsyncMock()
    monkeypatch.setattr(auth_service.revoked_tokens, "add", add)

    exp = int(time.time()) + 60
    await auth_service.logout(fake_db, jti="jti-123", exp_timestamp=exp)

    add.assert_awaited_once()
    args, kwargs = add.call_args
    assert args[1] == "jti-123"
    assert isinstance(args[2], datetime)
    assert args[2] == datetime.fromtimestamp(exp, tz=timezone.utc)
    fake_db.commit.assert_awaited_once()
