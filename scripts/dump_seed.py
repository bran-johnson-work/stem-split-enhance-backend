"""Dump the current database's data to a portable, idempotent ``seed.sql`` file.

This is a **data-only** migration tool. The schema is owned by Alembic, so the
target database is expected to already be at ``alembic upgrade head``; this script
only clones the *rows*, e.g. from a local Supabase Postgres into AWS RDS.

    # points at whatever .env resolves to (your local Supabase by default)
    python -m scripts.dump_seed                 # writes ./seed.sql
    python -m scripts.dump_seed -o /tmp/seed.sql
    python -m scripts.dump_seed --exclude refresh_tokens revoked_tokens

Then load it into the new database (schema already migrated):

    psql "$TARGET_DATABASE_URL" -f seed.sql
    # or: PGPASSWORD=... psql -h <rds-host> -U <user> -d <db> -f seed.sql

Design notes:

* Tables are emitted **parents-first** (see ``TABLE_ORDER``) so a plain load
  mostly satisfies foreign keys on its own.
* The whole load is wrapped in ``SET session_replication_role = replica`` which
  disables FK/uniqueness *triggers* for the session. This is what makes the
  self-referential ``refresh_tokens.replaced_by_id`` and any cross-table ordering
  edge cases load cleanly. On AWS RDS the master user has ``rds_superuser`` and is
  allowed to set it; if your loading role is *not* permitted, drop those two lines
  from the generated file — the parent-first ordering covers everything except the
  refresh-token self reference.
* Every INSERT uses ``ON CONFLICT DO NOTHING`` so re-running the load is safe and
  never duplicates a row.
* Rows are read with raw ``text()`` so the asyncpg driver returns native values
  (enums as plain strings, JSON as valid JSON text, ``timestamptz`` as ISO-8601),
  which serialize to correct SQL literals without any ORM type coercion.
* There are no serial/identity sequences to resynchronise — every primary key is
  an application-generated UUID.
"""

import argparse
import asyncio
import json
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

# Parents before children: a straight top-to-bottom load satisfies foreign keys.
# (FK triggers are also disabled during load, so this ordering is belt-and-braces.)
TABLE_ORDER = [
    "plans",
    "users",
    "revoked_tokens",
    "subscriptions",
    "refresh_tokens",  # self-referential via replaced_by_id
    "projects",
    "billing_events",
    "project_files",
    "processing_jobs",
    "output_files",
]

# Rows batched per INSERT statement to keep individual statements a sane size.
BATCH_SIZE = 500


def _quote(s: str) -> str:
    """Single-quote a string literal, doubling embedded quotes."""
    return "'" + s.replace("'", "''") + "'"


def sql_literal(value: object) -> str:
    """Render a Python/asyncpg value as a Postgres SQL literal.

    ``bool`` is checked before ``int`` (bool is a subclass of int). JSON columns
    arrive as already-valid JSON text from asyncpg, so quoting them yields a
    literal that coerces straight into a json/jsonb column.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, uuid.UUID):
        return _quote(str(value))
    if isinstance(value, datetime):
        return _quote(value.isoformat())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _quote("\\x" + bytes(value).hex())  # bytea hex format
    if isinstance(value, (dict, list)):
        return _quote(json.dumps(value))
    return _quote(str(value))


async def dump_table(conn, table: str) -> list[str]:
    """Return the SQL lines (INSERTs) for one table, or a comment if empty."""
    result = await conn.execute(text(f'SELECT * FROM "{table}"'))
    rows = result.mappings().all()
    if not rows:
        return [f"-- {table}: no rows\n"]

    columns = list(rows[0].keys())
    col_list = ", ".join(f'"{c}"' for c in columns)
    lines = [f"-- {table}: {len(rows)} row(s)"]

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        values = [
            "  (" + ", ".join(sql_literal(row[c]) for c in columns) + ")" for row in batch
        ]
        lines.append(
            f'INSERT INTO "{table}" ({col_list}) VALUES\n'
            + ",\n".join(values)
            + "\nON CONFLICT DO NOTHING;"
        )
    lines.append("")
    return lines


async def build_seed(output_path: str, exclude: set[str]) -> None:
    settings = get_settings()
    source = settings.active_database_url
    # Read-only workload; a small dedicated engine keeps this decoupled from the app's.
    engine = create_async_engine(
        source, connect_args=settings.asyncpg_connect_args(), echo=False
    )

    tables = [t for t in TABLE_ORDER if t not in exclude]
    print(f"[info] dumping {len(tables)} table(s) from {settings.postgres_host or source!r}")

    body: list[str] = []
    async with engine.connect() as conn:
        for table in tables:
            table_lines = await dump_table(conn, table)
            count_line = table_lines[0]
            print(f"[ok] {count_line[3:]}")
            body.extend(table_lines)
    await engine.dispose()

    header = [
        "-- Data-only seed generated by scripts/dump_seed.py",
        f"-- generated at {datetime.now().astimezone().isoformat()}",
        "-- Load into a database already migrated to `alembic upgrade head`:",
        "--   psql \"$TARGET_DATABASE_URL\" -f seed.sql",
        "",
        "BEGIN;",
        "-- Disable FK/constraint triggers for the load (handles table ordering and the",
        "-- refresh_tokens self-reference). Requires a privileged role (RDS master user).",
        "SET session_replication_role = replica;",
        "",
    ]
    footer = [
        "SET session_replication_role = origin;",
        "COMMIT;",
        "",
    ]

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(header))
        fh.write("\n".join(body))
        fh.write("\n".join(footer))

    print(f"[done] wrote {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump database rows to a seed.sql file.")
    parser.add_argument("-o", "--output", default="seed.sql", help="output file (default seed.sql)")
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        metavar="TABLE",
        help="tables to skip (e.g. refresh_tokens revoked_tokens for ephemeral session state)",
    )
    args = parser.parse_args()
    asyncio.run(build_seed(args.output, set(args.exclude)))


if __name__ == "__main__":
    main()
