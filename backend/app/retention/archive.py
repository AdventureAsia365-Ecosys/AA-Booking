"""AA-825 — archive a batch of rows to S3 before it is deleted (DM-7.3, data standards C5.4a).

Same format and key scheme as CIS ``services/retention/archive.py``: gzipped JSON Lines, one ``to_jsonb(row)`` per line
(every column, Postgres' own JSON for timestamps and jsonb). Not Parquet: pyarrow is not a dependency of this image
(nor of the CIS one); an Athena CTAS can convert the prefix later (L6).

Key: ``archive/booking/<table>/<yyyy>/<mm>/<job_id>-<run>-<part>-b<NNNNN>.jsonl.gz``. The runner never puts rows of two
UTC months in one object, so the yyyy/mm partition is exact. ``<run>`` is the UTC start of this attempt, so a re-run
never overwrites an object whose rows are already deleted; a duplicate in the archive is harmless, a lost row is not.
"""

from __future__ import annotations

import asyncio
import gzip
from datetime import UTC, datetime
from typing import Any

from app.retention.policy import ARCHIVE_BUCKET, ARCHIVE_PREFIX


def object_key(archive_name: str, month_of: datetime, job_id: str, run: str, part: str, batch_no: int) -> str:
    m = month_of.astimezone(UTC)
    return f"{ARCHIVE_PREFIX}/{archive_name}/{m:%Y}/{m:%m}/{job_id}-{run}-{part}-b{batch_no:05d}.jsonl.gz"


def encode(docs: list[str]) -> bytes:
    return gzip.compress(("\n".join(docs) + "\n").encode("utf-8"))


class S3Archiver:
    """Writes one object per batch. ``s3`` is a boto3 S3 client (or a test double with ``put_object``)."""

    def __init__(self, s3: Any, job_id: str, bucket: str = ARCHIVE_BUCKET, run: str | None = None) -> None:
        self.s3 = s3
        self.job_id = job_id
        self.bucket = bucket
        self.run = run or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    async def put(self, archive_name: str, part: str, batch_no: int, month_of: datetime, docs: list[str]) -> str:
        key = object_key(archive_name, month_of, self.job_id, self.run, part, batch_no)
        await asyncio.to_thread(
            self.s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=encode(docs),
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
            ServerSideEncryption="AES256",
            Metadata={"rows": str(len(docs)), "source": "aa-825-booking"},
        )
        return key
