"""Record reconciliation and storage.

Rules (Section 3.5 of the brief):
  * identical match      -> touch last_seen_at, write no new version
  * match with changes   -> new RecordVersion carrying the diff and its provenance
  * no match             -> new Record
  * previously present,
    now absent           -> mark missing. Records are NEVER deleted.

Two decisions worth knowing about:

  * Reconciliation for one site is serialized with a Postgres advisory lock, so
    sites run in parallel but two agents can never interleave writes for the same
    site. The lock is transaction-scoped and released on commit or rollback.
  * A record is only marked missing when the page it came from was successfully
    revisited in this run. A discovery miss or a 500 must not look like someone
    leaving the program.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.confidence import score_record
from ...domain.matching import IdentityKind, build_identity, diff_fields
from ...domain.specialty import infer_specialty
from ...extraction.person import ExtractedPerson
from ...urls import host_of, url_hash
from ..enums import ExtractionMethod, FetchMode, PersonCategory, RecordStatus
from ..ids import version_id as new_version_id
from ..models import Record, RecordVersion

log = logging.getLogger("agentscrape.reconcile")


@dataclass
class ExtractionContext:
    """Everything provenance needs about where a batch of people came from."""

    site_id: str
    site_host: str
    source_url: str
    page_title: str | None
    extraction_method: ExtractionMethod
    fetch_mode: FetchMode
    page_score: float = 0.0
    run_id: str | None = None
    site_run_id: str | None = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    site_dominant_specialty: str | None = None


@dataclass
class ReconcileResult:
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    skipped: int = 0
    record_ids: list[str] = field(default_factory=list)

    @property
    def total_seen(self) -> int:
        return self.new + self.changed + self.unchanged


async def lock_site(session: AsyncSession, site_id: str) -> None:
    """Serialize reconciliation for one site.

    Transaction-scoped, so it is released automatically on commit or rollback and
    a crashed agent cannot strand the lock.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": site_id}
    )


def _build_fields(
    person: ExtractedPerson, context: ExtractionContext
) -> dict[str, object] | None:
    """Normalize one extracted person into the stored field shape."""
    identity = build_identity(email=person.email, full_name=person.full_name)
    if identity is None:
        return None

    specialty = infer_specialty(
        explicit=person.specialty_raw,
        page_title=context.page_title,
        url=context.source_url,
    )
    canonical = specialty.canonical or context.site_dominant_specialty

    capture_day: date = context.captured_at.date()

    return {
        "identity_key": identity.key,
        "identity_kind": str(identity.kind),
        "role_account": identity.role_account,
        "full_name": person.full_name,
        "email": person.email,
        "category": str(person.category),
        "position": person.position,
        "specialty_normalized": canonical,
        "specialty_raw": person.specialty_raw or specialty.raw or None,
        # Exactly what the page stated. Nothing is inferred or rolled forward.
        "pgy_at_capture": person.pgy,
        "pgy_capture_date": capture_day,
        "class_of": person.class_of,
        "confidence": score_record(
            person,
            site_host=context.site_host,
            extraction_method=context.extraction_method,
            fetch_mode=context.fetch_mode,
            page_score=context.page_score,
        ),
    }


def _version_payload(fields: dict[str, object]) -> dict[str, object]:
    """The subset of fields that a version snapshot records."""
    from ...domain.matching import VERSIONED_FIELDS

    return {key: fields.get(key) for key in VERSIONED_FIELDS}


async def _attach_program(
    session: AsyncSession, site_id: str, fields: dict[str, object]
) -> None:
    """Give the record a programme, creating it on first sight."""
    from .programs import ensure_program

    specialty = fields.get("specialty_normalized")
    if specialty:
        fields["program_id"] = await ensure_program(
            session, site_id=site_id, specialty=str(specialty)
        )


async def reconcile_people(
    session: AsyncSession,
    people: list[ExtractedPerson],
    context: ExtractionContext,
) -> ReconcileResult:
    """Match a page's people against stored records and write the differences.

    The caller must already hold the site lock (see `lock_site`).
    """
    result = ReconcileResult()
    if not people:
        return result

    prepared: dict[str, tuple[ExtractedPerson, dict[str, object]]] = {}
    for person in people:
        fields = _build_fields(person, context)
        if fields is None:
            result.skipped += 1
            continue
        # The same person can appear twice on one page; keep the better copy.
        key = str(fields["identity_key"])
        existing = prepared.get(key)
        if existing is None or float(fields["confidence"]) > float(existing[1]["confidence"]):
            prepared[key] = (person, fields)

    if not prepared:
        return result

    # Look up the name-based key alongside the real one, so a page that finally
    # publishes an address adopts the record built from a page that did not.
    name_keys = {
        _name_key(fields): key
        for key, (_, fields) in prepared.items()
        if _name_key(fields) and _name_key(fields) != key
    }
    existing_rows = (
        await session.execute(
            select(Record).where(
                Record.site_id == context.site_id,
                Record.identity_key.in_([*prepared, *name_keys]),
            )
        )
    ).scalars().all()
    by_identity = {row.identity_key: row for row in existing_rows}

    for identity_key, (person, fields) in prepared.items():
        await _attach_program(session, context.site_id, fields)
        record = by_identity.get(identity_key) or _adopt_name_record(
            by_identity, fields, identity_key
        )
        if record is None:
            record = await _create_record(session, fields, person, context)
            result.new += 1
        else:
            changed = await _update_record(session, record, fields, person, context)
            # A person this run created and then learned more about on a later
            # page is already counted as new; counting the enrichment as a
            # change too would bill one person to both totals.
            if changed and record.status != RecordStatus.NEW:
                result.changed += 1
            else:
                result.unchanged += 1
        result.record_ids.append(record.id)

    return result


def _name_key(fields: dict[str, object]) -> str | None:
    """The name-based identity this person would have had without an address."""
    from ...domain.matching import normalize_name

    normalized = normalize_name(str(fields.get("full_name") or "")) or None
    return f"name:{normalized}" if normalized else None


def _adopt_name_record(
    by_identity: dict[str, Record],
    fields: dict[str, object],
    identity_key: str,
) -> Record | None:
    """Reuse the record built before this person's address was known.

    Rosters and directories disagree about addresses: a departmental roster names
    its residents with no address, while the institution-wide directory has the
    address but prints one combined "Resident/Fellow" term for everyone. Keyed
    separately, one person became two records — the correct category on one, the
    address on the other. On Arizona that was 238 people.

    Promoting the existing record to the address keeps a single row carrying
    both, and `_update_record` then applies its usual field precedence.
    """
    if not str(identity_key).startswith("email:"):
        return None
    name_key = _name_key(fields)
    if name_key is None:
        return None
    record = by_identity.get(name_key)
    if record is None or record.email:
        return None
    # The address is new information about a known person, so the record takes
    # the stronger identity rather than a second row being created beside it.
    record.identity_key = identity_key
    record.identity_kind = str(IdentityKind.EMAIL)
    record.role_account = False
    by_identity[identity_key] = record
    return record


async def _create_record(
    session: AsyncSession,
    fields: dict[str, object],
    person: ExtractedPerson,
    context: ExtractionContext,
) -> Record:
    record = Record(
        site_id=context.site_id,
        status=RecordStatus.NEW,
        first_seen_at=context.captured_at,
        last_seen_at=context.captured_at,
        last_changed_at=context.captured_at,
        last_run_id=context.run_id,
        version_count=1,
        **fields,
    )
    session.add(record)
    await session.flush()

    version = _make_version(
        record_id=record.id, version_no=1, fields=fields,
        changed_fields={}, context=context,
    )
    session.add(version)
    await session.flush()
    record.current_version_id = version.id
    return record


async def _update_record(
    session: AsyncSession,
    record: Record,
    fields: dict[str, object],
    person: ExtractedPerson,
    context: ExtractionContext,
) -> bool:
    """Returns True when a new version was written."""
    previous = {
        "full_name": record.full_name,
        "email": record.email,
        "category": record.category,
        "position": record.position,
        "specialty_normalized": record.specialty_normalized,
        "specialty_raw": record.specialty_raw,
        # Compare the captured PGY, never the derived current one: the July
        # rollover is not a change in the underlying data.
        "pgy_at_capture": record.pgy_at_capture,
        "class_of": record.class_of,
    }
    # "unknown" is the absence of a category, not a competing claim about one.
    # An institution-wide directory prints one combined "Resident/Fellow" term
    # and so yields `unknown` for people its departmental rosters identify
    # exactly; whichever page happened to be fetched last was overwriting the
    # other, and the directory is usually last because it ranks lower.
    if fields.get("category") == str(PersonCategory.UNKNOWN) and record.category != str(
        PersonCategory.UNKNOWN
    ):
        fields = {**fields, "category": record.category}
    # Within one run, a program roster naming someone a resident or fellow
    # outranks a generic directory page that files them otherwise: the trainee
    # label is the product, and whichever page is read last used to decide it.
    # Across runs the category may still change (graduation).
    #
    # "alumni" is the one exception: since `coerce_person` now requires
    # grounded evidence for every resident/fellow label, an alumni reading is
    # not a vaguer competing guess, it is the current-vs-former distinction
    # this same evidence system is meant to catch (a later page correctly
    # reading "Resident Alumni" for someone an earlier page misread). Letting
    # it through here is the smallest change consistent with that: a fuller
    # comparison for the general faculty/staff/student case would need those
    # categories to carry the same kind of grounded evidence resident/fellow
    # now does, which is a larger change than this task covers.
    #
    # Whether this run has already recorded a sighting of this person. Read it
    # before `last_run_id` is reassigned below.
    seen_earlier_in_run = (
        record.last_run_id is not None and record.last_run_id == context.run_id
    )

    trainees = (str(PersonCategory.RESIDENT), str(PersonCategory.FELLOW))
    if (
        record.category in trainees
        and fields.get("category") not in trainees
        and fields.get("category") != str(PersonCategory.ALUMNI)
        and seen_earlier_in_run
    ):
        fields = {**fields, "category": record.category}

    changes = diff_fields(previous, _version_payload(fields))

    # Repeated identical sightings within a run add no durable information.
    # Avoid another row/index/WAL write merely because a second page lists the
    # same person. Still refresh on each new run and every real field change.
    if not seen_earlier_in_run or changes:
        record.last_seen_at = context.captured_at
        record.last_run_id = context.run_id
        record.missing_since = None
    if float(fields["confidence"]) > record.confidence:
        record.confidence = float(fields["confidence"])

    if not changes:
        # Identical: no new version, and a previously-missing record is alive
        # again. The status answers "what did this run find?", so it is only
        # settled on the run's first sighting: a person listed on a roster and
        # again on their profile page used to be demoted from new to active by
        # the second page of the same crawl.
        if not seen_earlier_in_run:
            record.status = RecordStatus.ACTIVE
        return False

    for key, value in fields.items():
        # Never overwrite a known value with a null; see diff_fields.
        if value is None and getattr(record, key, None) is not None:
            continue
        setattr(record, key, value)

    # Someone this run met for the first time stays new however many pages they
    # turn up on. "Changed" describes a difference from an earlier crawl, not
    # the order in which two pages of this one were read.
    if not (seen_earlier_in_run and record.status == RecordStatus.NEW):
        record.status = RecordStatus.CHANGED
    record.last_changed_at = context.captured_at
    record.version_count += 1

    version = _make_version(
        record_id=record.id, version_no=record.version_count, fields=fields,
        changed_fields=changes, context=context,
    )
    session.add(version)
    await session.flush()
    record.current_version_id = version.id
    return True


def _make_version(
    *,
    record_id: str,
    version_no: int,
    fields: dict[str, object],
    changed_fields: dict[str, object],
    context: ExtractionContext,
) -> RecordVersion:
    return RecordVersion(
        id=new_version_id(),
        record_id=record_id,
        version_no=version_no,
        fields=_version_payload(fields),
        changed_fields=changed_fields,
        source_url=context.source_url,
        page_title=context.page_title,
        captured_at=context.captured_at,
        extraction_method=context.extraction_method,
        fetch_mode=context.fetch_mode,
        confidence=float(fields["confidence"]),
        run_id=context.run_id,
        site_run_id=context.site_run_id,
    )


async def fill_record_blanks(
    session: AsyncSession,
    record: Record,
    found: ExtractedPerson,
    context: ExtractionContext,
) -> list[str]:
    """Fill only the fields `record` lacks from `found`; never overwrite.

    Used by directory search: a roster is the better source for anything it
    printed, so a directory only supplies what the roster left blank. Writes one
    new version carrying the filled fields. Returns the names of the fields
    filled (empty when there was nothing to add).
    """
    filled: dict[str, object] = {}
    if not record.email and found.email:
        conflict = await session.scalar(
            select(Record.id).where(
                Record.site_id == record.site_id,
                Record.id != record.id,
                Record.email == found.email,
            )
        )
        # The address already belongs to another record at this site: two
        # rows for one person is a merge decision, not a blank to fill.
        if conflict is None:
            filled["email"] = found.email
    if record.pgy_at_capture is None and found.pgy is not None:
        filled["pgy"] = found.pgy
    if record.class_of is None and found.class_of is not None:
        filled["class_of"] = found.class_of
    if not record.position and found.position:
        filled["position"] = found.position
    if not filled:
        return []

    category = record.category if record.category in {c.value for c in PersonCategory} else "unknown"
    person = ExtractedPerson(
        full_name=record.full_name,
        email=filled.get("email", record.email),  # type: ignore[arg-type]
        category=PersonCategory(category),
        position=filled.get("position", record.position),  # type: ignore[arg-type]
        pgy=filled.get("pgy", record.pgy_at_capture),  # type: ignore[arg-type]
        class_of=filled.get("class_of", record.class_of),  # type: ignore[arg-type]
        specialty_raw=record.specialty_raw,
        confidence=found.confidence,
    )
    fields = _build_fields(person, context)
    if fields is None:
        return []
    # The directory page's title says nothing about the person's specialty,
    # and a PGY that was not filled here keeps the date it was read.
    fields["specialty_normalized"] = record.specialty_normalized or fields["specialty_normalized"]
    fields["specialty_raw"] = record.specialty_raw or fields["specialty_raw"]
    if "pgy" not in filled:
        fields["pgy_capture_date"] = record.pgy_capture_date
    if "email" not in filled:
        fields["identity_key"] = record.identity_key
        fields["identity_kind"] = record.identity_kind
        fields["role_account"] = record.role_account
    await _update_record(session, record, fields, person, context)
    return list(filled)


async def mark_missing_records(
    session: AsyncSession,
    *,
    site_id: str,
    seen_record_ids: set[str],
    visited_url_hashes: set[str],
    now: datetime | None = None,
) -> int:
    """Mark records absent from this run as missing — but only when the page they
    came from was actually revisited successfully.

    Without that guard a discovery miss, a timeout or a 500 would be
    indistinguishable from someone leaving the program.
    """
    now = now or datetime.now(UTC)
    if not visited_url_hashes:
        return 0

    # The page a person was last *published* on: a directory lookup adds a
    # version too, but the directory is never revisited by a crawl, so judging
    # by it would make anyone ever looked up impossible to mark missing.
    latest_page = (
        select(RecordVersion.record_id, func.max(RecordVersion.version_no).label("version_no"))
        .where(RecordVersion.extraction_method != str(ExtractionMethod.DIRECTORY))
        .group_by(RecordVersion.record_id)
        .subquery()
    )
    candidates = (
        await session.execute(
            select(Record.id, RecordVersion.source_url)
            .join(latest_page, latest_page.c.record_id == Record.id)
            .join(
                RecordVersion,
                (RecordVersion.record_id == Record.id)
                & (RecordVersion.version_no == latest_page.c.version_no),
            )
            .where(
                Record.site_id == site_id,
                Record.status != RecordStatus.MISSING,
                Record.id.notin_(seen_record_ids) if seen_record_ids else True,
            )
        )
    ).all()

    to_mark = [
        record_id
        for record_id, source_url in candidates
        if source_url and url_hash(source_url) in visited_url_hashes
    ]
    if not to_mark:
        return 0

    await session.execute(
        update(Record)
        .where(Record.id.in_(to_mark))
        .values(status=RecordStatus.MISSING, missing_since=now)
    )
    log.info("marked %d records missing for site %s", len(to_mark), site_id)
    return len(to_mark)


async def run_counts(
    session: AsyncSession, site_id: str, run_id: str | None
) -> tuple[int, int, int]:
    """(people, residents and fellows, people with an email) this run has
    seen at a site: unique records, not sightings."""
    if run_id is None:
        return 0, 0, 0
    trainees = (str(PersonCategory.RESIDENT), str(PersonCategory.FELLOW))
    row = (
        await session.execute(
            select(
                func.count(Record.id),
                func.count(Record.id).filter(Record.category.in_(trainees)),
                func.count(Record.id).filter(Record.email.isnot(None)),
            ).where(Record.site_id == site_id, Record.last_run_id == run_id)
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def stored_identity_keys(session: AsyncSession, site_id: str) -> set[str]:
    """Identity keys of a site's live records — the skip check's comparison set."""
    rows = await session.execute(
        select(Record.identity_key).where(
            Record.site_id == site_id, Record.status != RecordStatus.MISSING
        )
    )
    return set(rows.scalars().all())


async def site_record_count(session: AsyncSession, site_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count(Record.id)).where(Record.site_id == site_id)
        ) or 0
    )


def host_for(url: str) -> str:
    return host_of(url)
