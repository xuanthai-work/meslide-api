"""Idempotent backfill: upload legacy template JSON bundles to object storage.

PR3 cutover step 3-4 of the S3/MinIO plan (artifacts/s3-minio-object-storage-plan-2026-09-16.md):

    select legacy rows -> build bundle -> validate -> upload -> read-back
    verify checksum -> update DB reference in a transaction -> report

Deliberately NOT an Alembic migration: data migration runs separately from
schema migrations, must be resumable, and must never delete the source
JSON columns (they are only nulled in a later release, after the rollback
window).

Usage:

    python scripts/backfill_template_bundles.py [--dry-run]
        [--batch-size 50] [--template-id <id>] [--report report.json]
    python scripts/backfill_template_bundles.py --verify
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FASTAPI_ROOT = Path(__file__).resolve().parents[1]
if str(FASTAPI_ROOT) not in sys.path:
    sys.path.insert(0, str(FASTAPI_ROOT))

from sqlmodel import select  # noqa: E402

from models.sql.template_v2 import TemplateV2  # noqa: E402
from services.database import async_session_maker  # noqa: E402
from services.template_bundles.bundle import deserialize_bundle  # noqa: E402
from services.template_bundles.errors import (  # noqa: E402
    InvalidBundleError,
    TemplateBundleError,
)
from services.template_bundles.repository import TemplateBundleRepository  # noqa: E402
from services.object_storage.errors import ObjectStorageError  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50


@dataclass
class BackfillReport:
    scanned: int = 0
    would_backfill: int = 0
    backfilled: int = 0
    skipped_already_bundled: int = 0
    skipped_empty: int = 0
    verified: int = 0
    dry_run: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def print_summary(self) -> None:
        print(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))


def _has_legacy_payload(template: TemplateV2) -> bool:
    return any(
        (
            template.raw_layouts is not None,
            template.layouts is not None,
            template.merged_components is not None,
        )
    )


class TemplateBundleBackfiller:
    def __init__(
        self,
        session_factory=None,
        repository: TemplateBundleRepository | None = None,
    ):
        self._session_factory = session_factory or async_session_maker
        self._repository = repository or TemplateBundleRepository()

    async def run(
        self,
        *,
        dry_run: bool = False,
        batch_size: int = DEFAULT_BATCH_SIZE,
        template_id: str | None = None,
        report_path: str | Path | None = None,
    ) -> BackfillReport:
        """Upload bundles for rows that still keep their payload in the
        legacy JSON columns. Safe to re-run: bundled rows are skipped."""
        report = BackfillReport(dry_run=dry_run)
        async with self._session_factory() as session:
            templates = await self._load_templates(session, template_id)
            report.scanned = len(templates)
            for start in range(0, len(templates), max(1, batch_size)):
                for template in templates[start : start + max(1, batch_size)]:
                    await self._backfill_one(session, template, report)
        self._finish(report, report_path)
        return report

    async def verify(
        self,
        *,
        template_id: str | None = None,
        report_path: str | Path | None = None,
    ) -> BackfillReport:
        """Cutover step 4: every stored reference must match its object."""
        report = BackfillReport()
        async with self._session_factory() as session:
            templates = await self._load_templates(session, template_id)
            report.scanned = len(templates)
            for template in templates:
                if not template.bundle_object_key:
                    continue  # legacy row, still inside the migration window
                await self._verify_one(template, report)
        self._finish(report, report_path)
        return report

    async def _load_templates(self, session, template_id: str | None):
        if template_id:
            template = await session.get(TemplateV2, template_id)
            return [template] if template is not None else []
        result = await session.execute(select(TemplateV2))
        return list(result.scalars().all())

    async def _backfill_one(self, session, template, report: BackfillReport):
        if template.bundle_object_key:
            report.skipped_already_bundled += 1
            return
        if not _has_legacy_payload(template):
            report.skipped_empty += 1
            return

        if report.dry_run:
            report.would_backfill += 1
            return

        previous_reference = (
            template.bundle_object_key,
            template.bundle_sha256,
            template.bundle_size_bytes,
            template.bundle_schema_version,
            template.layout_count,
        )
        try:
            # Preserve the source JSON during the rollback window. Normal
            # application writes clear these columns, but a backfill must not
            # make rollback depend on object storage before cutover is proven.
            await self._repository.save(template, clear_legacy_payload=False)
            await self._read_back_verify(template)
        except InvalidBundleError as exc:
            report.failures.append(
                {"template_id": template.id, "stage": "validate", "error": str(exc)}
            )
            self._restore_reference(template, previous_reference)
            return
        except (ObjectStorageError, TemplateBundleError, ValueError) as exc:
            report.failures.append(
                {
                    "template_id": template.id,
                    "stage": "upload_or_verify",
                    "error": str(exc),
                }
            )
            self._restore_reference(template, previous_reference)
            return

        # DB reference update happens in its own transaction so a crash
        # mid-run never loses completed work (safe resume).
        session.add(template)
        await session.commit()
        report.backfilled += 1
        logger.info("backfilled template_id=%s key=%s", template.id, template.bundle_object_key)

    async def _read_back_verify(self, template) -> None:
        compressed = await self._repository.storage.get_bytes(template.bundle_object_key)
        actual = hashlib.sha256(compressed).hexdigest()
        if template.bundle_sha256 and actual != template.bundle_sha256:
            raise TemplateBundleError("read-back checksum mismatch")
        deserialize_bundle(compressed)

    async def _verify_one(self, template, report: BackfillReport):
        try:
            await self._read_back_verify(template)
        except (ObjectStorageError, TemplateBundleError, ValueError) as exc:
            report.failures.append(
                {
                    "template_id": template.id,
                    "stage": "verify",
                    "object_key": template.bundle_object_key,
                    "error": str(exc),
                }
            )
            return
        report.verified += 1

    @staticmethod
    def _restore_reference(template, previous_reference) -> None:
        (
            template.bundle_object_key,
            template.bundle_sha256,
            template.bundle_size_bytes,
            template.bundle_schema_version,
            template.layout_count,
        ) = previous_reference

    def _finish(self, report: BackfillReport, report_path) -> None:
        if report_path is not None:
            Path(report_path).write_text(
                json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        report.print_summary()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; upload nothing.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Rows processed per batch (default: %(default)s).",
    )
    parser.add_argument("--template-id", help="Backfill/verify a single template.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify stored references instead of backfilling.",
    )
    parser.add_argument("--report", help="Write the JSON report to this path.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    backfiller = TemplateBundleBackfiller()
    if args.verify:
        report = asyncio.run(
            backfiller.verify(template_id=args.template_id, report_path=args.report)
        )
    else:
        report = asyncio.run(
            backfiller.run(
                dry_run=args.dry_run,
                batch_size=args.batch_size,
                template_id=args.template_id,
                report_path=args.report,
            )
        )
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
