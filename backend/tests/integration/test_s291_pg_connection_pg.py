"""S291 (C4.3 review, AA-838) on PostgreSQL as the ``booking`` login: the API pool (:class:`app.db.Database`) hands
out the depth-named savepoint connection class, and those savepoints roll back exactly what asyncpg's unique names
did (an outer block that fails after a caught inner rollback undoes its own work too; a failed statement two levels
down leaves the outer block usable). Temporary table only; nothing in schema booking is written."""

from __future__ import annotations

import pytest

from app.db import Database
from app.pg_connection import StableSavepointConnection
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_depth_named_savepoints_roll_back_what_asyncpg_did(migrated_booking_dsn: str) -> None:
    database = Database(Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test"))
    try:
        pool = await database.pool()
        async with pool.acquire() as c:
            assert isinstance(c, StableSavepointConnection)
            sent: list[str] = []
            c.add_query_logger(lambda record: sent.append(str(record.query)))
            async with c.transaction():
                await c.execute("CREATE TEMP TABLE zz_sp (v int) ON COMMIT DROP")
                await c.execute("INSERT INTO zz_sp VALUES (1)")
                with pytest.raises(KeyError):
                    async with c.transaction():
                        await c.execute("INSERT INTO zz_sp VALUES (2)")
                        with pytest.raises(ValueError):
                            async with c.transaction():
                                await c.execute("INSERT INTO zz_sp VALUES (3)")
                                raise ValueError
                        await c.execute("INSERT INTO zz_sp VALUES (4)")
                        async with c.transaction():
                            await c.execute("INSERT INTO zz_sp VALUES (5)")
                        raise KeyError
                async with c.transaction():
                    await c.execute("INSERT INTO zz_sp VALUES (6)")
                rows = [r["v"] for r in await c.fetch("SELECT v FROM zz_sp ORDER BY v")]
                # A failed statement inside a depth-2 block leaves the outer block usable.
                with pytest.raises(Exception, match="division by zero"):
                    async with c.transaction():
                        async with c.transaction():
                            await c.execute("SELECT 1 / 0")
                assert await c.fetchval("SELECT count(*) AS n FROM zz_sp") == 2
        assert rows == [1, 6]
        savepoints = {q for q in sent if "SAVEPOINT" in q or q.startswith("ROLLBACK TO")}
        assert savepoints and all("bk_sp_" in q for q in savepoints)
        assert savepoints <= {
            "SAVEPOINT bk_sp_1;", "SAVEPOINT bk_sp_2;", "RELEASE SAVEPOINT bk_sp_1;", "RELEASE SAVEPOINT bk_sp_2;",
            "ROLLBACK TO bk_sp_1;", "ROLLBACK TO bk_sp_2;",
        }  # fmt: skip
    finally:
        await database.close()
