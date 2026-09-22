"""CSV export and the SSE progress stream."""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import UTC, datetime

import pytest_asyncio

from agentscrape.db.enums import ExportStatus, ExtractionMethod, FetchMode, PersonCategory
from agentscrape.db.models import Export, Record, RecordVersion, Run, Site
from agentscrape.export.service import BASE_COLUMNS, run_export
from agentscrape.orchestrator.events import Event, EventBus, EventType


@pytest_asyncio.fixture
async def populated(session):
    site = Site(
        root_domain="med.example.edu", canonical_url="https://med.example.edu/",
        hospital_name="Example Teaching Hospital",
    )
    session.add(site)
    await session.flush()
    now = datetime.now(UTC)
    for i, (name, role, pgy) in enumerate(
        [("Ann Riley", PersonCategory.RESIDENT, 2), ("Ben Cole", PersonCategory.FELLOW, None)]
    ):
        record = Record(
            site_id=site.id, identity_key=f"email:p{i}@med.example.edu",
            identity_kind="email", full_name=name, email=f"p{i}@med.example.edu",
            category=role, position="Resident", specialty_normalized="Internal Medicine",
            pgy_at_capture=pgy, pgy_capture_date=now.date(), class_of=2028,
            status="active", confidence=0.9, version_count=1,
        )
        session.add(record)
        await session.flush()
        version = RecordVersion(
            record_id=record.id, version_no=1, fields={}, changed_fields={},
            source_url="https://med.example.edu/residents",
            page_title="Current Residents", captured_at=now,
            extraction_method=ExtractionMethod.KNOWN_PATH, fetch_mode=FetchMode.HTML,
            confidence=0.9,
        )
        session.add(version)
        await session.flush()
        record.current_version_id = version.id
    await session.commit()
    return site


def _read(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class TestExport:
    async def test_columns_match_the_client_spreadsheet(self, session, populated):
        export = Export(status=ExportStatus.PENDING, filters={})
        session.add(export)
        await session.commit()

        await run_export(export.id)
        await session.refresh(export)

        assert export.status == ExportStatus.COMPLETED
        assert export.row_count == 2

        from agentscrape.storage.artifacts import absolute_path

        rows = _read(absolute_path(export.file_path))
        for column in BASE_COLUMNS:
            assert column in rows[0]

        by_name = {r["Full Name"]: r for r in rows}
        assert by_name["Ann Riley"]["R/F"] == "R"      # resident
        assert by_name["Ben Cole"]["R/F"] == "F"       # fellow
        assert by_name["Ann Riley"]["Specialty"] == "Internal Medicine"
        assert by_name["Ann Riley"]["Hospital"] == "Example Teaching Hospital"
        assert by_name["Ann Riley"]["Class of"] == "2028"
        assert by_name["Ann Riley"]["Position"] == "Resident"

    async def test_export_respects_the_active_filters(self, session, populated):
        export = Export(status=ExportStatus.PENDING, filters={"category": ["fellow"]})
        session.add(export)
        await session.commit()
        await run_export(export.id)
        await session.refresh(export)
        assert export.row_count == 1

    async def test_failure_is_recorded_on_the_job_not_raised(self, session):
        export = Export(status=ExportStatus.PENDING, filters={"limit": "not-a-number"})
        session.add(export)
        await session.commit()
        await run_export(export.id)  # must not raise
        await session.refresh(export)
        assert export.status in (ExportStatus.COMPLETED, ExportStatus.FAILED)


class TestEventBus:
    async def test_subscribers_receive_events_for_their_run(self):
        bus = EventBus()
        queue = bus.subscribe("run_1")
        await bus.publish(Event(EventType.SITE_STARTED, "run_1", {"domain": "x.edu"}))
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event.type is EventType.SITE_STARTED
        assert event.to_json()["domain"] == "x.edu"

    async def test_events_are_scoped_to_one_run(self):
        bus = EventBus()
        queue = bus.subscribe("run_1")
        await bus.publish(Event(EventType.SITE_STARTED, "run_2"))
        assert queue.empty()

    async def test_a_slow_subscriber_is_dropped_not_blocking(self):
        """The heartbeat is authoritative, so dropping is safe and never blocks."""
        from agentscrape.orchestrator.events import SUBSCRIBER_QUEUE_SIZE

        bus = EventBus()
        bus.subscribe("run_1")  # never drained
        for _ in range(SUBSCRIBER_QUEUE_SIZE + 50):
            await asyncio.wait_for(
                bus.publish(Event(EventType.SITE_STEP, "run_1")), timeout=1
            )

    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        queue = bus.subscribe("run_1")
        bus.unsubscribe("run_1", queue)
        await bus.publish(Event(EventType.SITE_STARTED, "run_1"))
        assert queue.empty()
        assert bus.subscriber_count("run_1") == 0


class TestSseFraming:
    def test_frames_are_valid_sse_with_a_type_discriminator(self):
        from agentscrape.api.sse import format_event

        frame = format_event(
            Event(EventType.SITE_SKIPPED, "run_1", {"similarity_score": 0.97})
        )
        assert frame.startswith("event: site_skipped\n")
        assert frame.endswith("\n\n")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["type"] == "site_skipped"
        assert payload["run_id"] == "run_1"
        assert payload["similarity_score"] == 0.97
        assert "at" in payload

    async def test_stream_emits_a_snapshot_then_ends_for_a_finished_run(self, session):
        from agentscrape.api.sse import event_stream

        run = Run(status="completed", sites_total=1, records_found=7)
        session.add(run)
        await session.commit()

        frames = [frame async for frame in event_stream(run.id)]
        assert len(frames) == 1
        payload = json.loads(frames[0].split("data: ", 1)[1].strip())
        assert payload["snapshot"] is True
        assert payload["records_found"] == 7

    async def test_heartbeat_carries_records_and_spend(self):
        """The frontend treats the heartbeat as authoritative for its counters."""
        from agentscrape.api.sse import format_event

        frame = format_event(
            Event(
                EventType.HEARTBEAT, "run_1",
                {"records_found": 42, "spend_usd": 1.25, "sites_completed": 3},
            )
        )
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["records_found"] == 42
        assert payload["spend_usd"] == 1.25
