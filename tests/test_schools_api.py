"""School -> Program -> Person navigation, CSV submissions and admin scope.

These are the endpoints the frontend actually navigates, and the access split
that keeps clients out of the staff queue.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentscrape.db.enums import ExtractionMethod, FetchMode, PersonCategory
from agentscrape.db.models import Program, Record, RecordVersion, Site

API = "/api/v1"
CLIENT_PW = "change-me"
ADMIN_PW = "change-me-admin"


@pytest_asyncio.fixture
async def client(reset_global_engine):
    from agentscrape.api.main import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as c:
        yield c


async def _headers(client, password: str) -> dict:
    response = await client.post(f"{API}/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest_asyncio.fixture
async def seeded(session):
    site = Site(
        root_domain="med.example.edu",
        canonical_url="https://med.example.edu/",
        name="Example Teaching Hospital",
        location="Springfield, IL",
    )
    session.add(site)
    await session.flush()

    program = Program(
        site_id=site.id,
        specialty="Internal Medicine",
        name="Internal Medicine Residency",
        start_url="https://med.example.edu/im",
    )
    session.add(program)
    await session.flush()

    now = datetime.now(UTC)
    people = [
        ("Jane Doe", PersonCategory.RESIDENT, "Resident", 2),
        ("Alan Grant", PersonCategory.FACULTY, "Program Director", None),
        ("Ray Arnold", PersonCategory.STAFF, "Program Coordinator", None),
    ]
    for index, (name, category, position, pgy) in enumerate(people):
        record = Record(
            site_id=site.id,
            program_id=program.id,
            identity_key=f"email:p{index}@med.example.edu",
            identity_kind="email",
            full_name=name,
            email=f"p{index}@med.example.edu",
            category=category,
            position=position,
            specialty_normalized="Internal Medicine",
            pgy_at_capture=pgy,
            pgy_capture_date=now.date(),
            status="active",
            confidence=0.9,
            version_count=1,
        )
        session.add(record)
        await session.flush()
        version = RecordVersion(
            record_id=record.id,
            version_no=1,
            fields={},
            changed_fields={},
            source_url="https://med.example.edu/im/residents",
            page_title="Current Residents",
            captured_at=now,
            extraction_method=ExtractionMethod.DISCOVERY,
            fetch_mode=FetchMode.BOTH,
            confidence=0.9,
            screenshot_available=True,
            screenshot_path="screenshots/a/b.png",
            screenshot_width=1440,
            screenshot_height=3200,
            field_locations={"email": {"x": 10, "y": 20, "width": 100, "height": 16}},
        )
        session.add(version)
        await session.flush()
        record.current_version_id = version.id

    program.people_count = 3
    program.resident_count = 1
    await session.commit()
    return {"site": site, "program": program}


class TestNavigation:
    async def test_school_list_carries_name_and_location(self, client, seeded):
        headers = await _headers(client, CLIENT_PW)
        body = (await client.get(f"{API}/schools", headers=headers)).json()
        school = body["items"][0]
        assert school["name"] == "Example Teaching Hospital"
        assert school["location"] == "Springfield, IL"
        assert school["program_count"] == 1
        assert school["people_count"] == 3

    async def test_programs_for_a_school(self, client, seeded):
        headers = await _headers(client, CLIENT_PW)
        school_id = seeded["site"].id
        body = (
            await client.get(f"{API}/schools/{school_id}/programs", headers=headers)
        ).json()
        program = body["items"][0]
        assert program["name"] == "Internal Medicine Residency"
        assert program["school_id"] == school_id
        assert program["resident_count"] == 1
        assert program["start_url"] == "https://med.example.edu/im"

    async def test_people_for_a_program_include_everyone(self, client, seeded):
        headers = await _headers(client, CLIENT_PW)
        program_id = seeded["program"].id
        body = (
            await client.get(f"{API}/programs/{program_id}/people", headers=headers)
        ).json()
        by_name = {p["full_name"]: p for p in body["items"]}
        assert set(by_name) == {"Jane Doe", "Alan Grant", "Ray Arnold"}
        assert by_name["Alan Grant"]["category"] == "faculty"
        assert by_name["Alan Grant"]["position"] == "Program Director"
        assert by_name["Ray Arnold"]["category"] == "staff"

    async def test_people_can_be_filtered_by_category(self, client, seeded):
        headers = await _headers(client, CLIENT_PW)
        program_id = seeded["program"].id
        body = (
            await client.get(
                f"{API}/programs/{program_id}/people",
                params={"category": "resident"},
                headers=headers,
            )
        ).json()
        assert [p["full_name"] for p in body["items"]] == ["Jane Doe"]

    async def test_single_person(self, client, seeded, session):
        from sqlalchemy import select

        headers = await _headers(client, CLIENT_PW)
        record_id = (
            await session.execute(select(Record.id).where(Record.full_name == "Jane Doe"))
        ).scalar_one()
        body = (await client.get(f"{API}/people/{record_id}", headers=headers)).json()
        assert body["full_name"] == "Jane Doe"
        assert body["pgy"] == 2

    async def test_year_is_not_rolled_forward(self, client, seeded, session):
        """Captured as PGY-2 today, so it reads PGY-2 with its capture date."""
        from sqlalchemy import select

        headers = await _headers(client, CLIENT_PW)
        record_id = (
            await session.execute(select(Record.id).where(Record.full_name == "Jane Doe"))
        ).scalar_one()
        body = (await client.get(f"{API}/people/{record_id}", headers=headers)).json()
        assert body["pgy"] == 2
        assert body["pgy_capture_date"] is not None
        # No inference: the page never stated a class year.
        assert body["year"] is None


class TestScreenshotLinks:
    async def test_source_returns_a_signed_link_and_dimensions(
        self, client, seeded, session
    ):
        from sqlalchemy import select

        headers = await _headers(client, CLIENT_PW)
        record_id = (
            await session.execute(select(Record.id).where(Record.full_name == "Jane Doe"))
        ).scalar_one()
        body = (
            await client.get(f"{API}/people/{record_id}/source", headers=headers)
        ).json()
        # An <img> tag cannot send an Authorization header, so the URL is signed.
        assert "sig=" in body["screenshot_url"] and "exp=" in body["screenshot_url"]
        assert body["field_locations"]["email"]["x"] == 10

    async def test_a_signed_link_needs_no_token(self, client, seeded, session):
        from sqlalchemy import select

        headers = await _headers(client, CLIENT_PW)
        record_id = (
            await session.execute(select(Record.id).where(Record.full_name == "Jane Doe"))
        ).scalar_one()
        url = (
            await client.get(f"{API}/people/{record_id}/source", headers=headers)
        ).json()["screenshot_url"]

        # No auth header at all: 404 because the file is absent, not 401.
        response = await client.get(url)
        assert response.status_code == 404

    async def test_an_unsigned_artifact_still_requires_auth(self, client):
        response = await client.get(f"{API}/artifacts/screenshots/a/b.png")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"

    async def test_a_tampered_signature_is_rejected(self, client):
        response = await client.get(
            f"{API}/artifacts/screenshots/a/b.png",
            params={"exp": "99999999999", "sig": "0" * 32},
        )
        assert response.status_code == 401


class TestAdminScope:
    async def test_client_password_cannot_reach_the_staff_queue(self, client):
        headers = await _headers(client, CLIENT_PW)
        response = await client.get(f"{API}/admin/submissions", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"

    async def test_admin_password_can(self, client):
        headers = await _headers(client, ADMIN_PW)
        response = await client.get(f"{API}/admin/submissions", headers=headers)
        assert response.status_code == 200

    async def test_login_reports_the_scope(self, client):
        for password, scope in ((CLIENT_PW, "client"), (ADMIN_PW, "admin")):
            body = (
                await client.post(f"{API}/auth/login", json={"password": password})
            ).json()
            assert body["authenticated"] is True
            assert body["user"]["scope"] == scope

    async def test_session_survives_a_reload(self, client):
        headers = await _headers(client, ADMIN_PW)
        body = (await client.get(f"{API}/auth/session", headers=headers)).json()
        assert body["authenticated"] is True
        assert body["user"]["scope"] == "admin"


class TestSubmissions:
    CSV = b"url\nmed.example.edu\nbrand-new-hospital.edu\nnot a url\n"

    async def test_a_client_can_submit_but_not_run(self, client, seeded):
        headers = await _headers(client, CLIENT_PW)
        response = await client.post(
            f"{API}/submissions",
            files={"file": ("wanted.csv", self.CSV, "text/csv")},
            data={"note": "Q4 targets"},
            headers=headers,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["row_count"] == 3
        assert body["valid_count"] == 2
        assert body["note"] == "Q4 targets"

        # Running it is staff-only.
        forbidden = await client.post(
            f"{API}/admin/submissions/{body['id']}/run",
            json={"max_spend_usd": 5},
            headers=headers,
        )
        assert forbidden.status_code == 403

    async def test_staff_see_the_queue_with_the_row_preview(self, client, seeded):
        client_headers = await _headers(client, CLIENT_PW)
        await client.post(
            f"{API}/submissions",
            files={"file": ("wanted.csv", self.CSV, "text/csv")},
            headers=client_headers,
        )

        admin_headers = await _headers(client, ADMIN_PW)
        body = (
            await client.get(f"{API}/admin/submissions", headers=admin_headers)
        ).json()
        assert len(body["items"]) == 1
        submission = body["items"][0]
        assert submission["filename"] == "wanted.csv"
        rows = {r["input"]: r for r in submission["rows"]}
        # The preview says which schools are already known before anything is spent.
        assert rows["med.example.edu"]["known_site"] is True
        assert rows["not a url"]["valid"] is False
