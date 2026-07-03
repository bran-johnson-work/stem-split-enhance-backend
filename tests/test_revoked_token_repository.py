from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.repositories.revoked_token_repository import revoked_token_repository


def _first_result(value):
    result = MagicMock()
    result.first.return_value = value
    return result


async def test_is_revoked_true_when_row_exists(fake_db):
    fake_db.execute.return_value = _first_result(("some-id",))
    assert await revoked_token_repository.is_revoked(fake_db, "jti") is True


async def test_is_revoked_false_when_absent(fake_db):
    fake_db.execute.return_value = _first_result(None)
    assert await revoked_token_repository.is_revoked(fake_db, "jti") is False


async def test_add_inserts_when_absent(fake_db):
    fake_db.execute.return_value = _first_result(None)
    await revoked_token_repository.add(fake_db, "jti", datetime.now(timezone.utc))
    fake_db.add.assert_called_once()
    fake_db.flush.assert_awaited_once()


async def test_add_is_noop_when_already_revoked(fake_db):
    fake_db.execute.return_value = _first_result(("some-id",))
    await revoked_token_repository.add(fake_db, "jti", datetime.now(timezone.utc))
    fake_db.add.assert_not_called()
    fake_db.flush.assert_not_awaited()
