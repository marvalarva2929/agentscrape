"""API contract tests: auth, error envelope, pagination, filters, provenance.

The frontend is built against this contract, so these assert the response shape
as much as the behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentscrape.db.enums import (
    ExtractionMethod,
    FetchMode,
    PersonCategory,
    RecordStatus,
)
from agentscrape.db.models import Record, RecordVersion, Run, Site, SiteRun

API = "/api/v1"


@pytest_asyncio.fixture
async def client(reset_global_engine):
    """Depends on the engine reset: the app uses the module-level engine, which
    is bound to whichever event loop first created it."""
    from agentscrape.api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def token(client):
    response = await client.post(f"{API}/auth/login", json={"password": "change-me"})
    assert response.status_code == 200
    return response.json()["token"]


@pytest_asyncio.fixture
async def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def seeded(session):
    """One site, one run, three records, one with two versions."""
    site = Site(
        root_domain="med.example.edu", canonical_url="https://med.example.edu/",
        hospital_name="Example Teaching Hospital", dominant_specialty="Radiation Oncology",
    )
    session.add(site)
    await session.flush()

    run = Run(status="completed", sites_total=1, config={"concurrency": 4})
    session.add(run)
    await session.flush()
    session.add(SiteRun(run_id=run.id, site_id=site.id, status="completed"))

    now = datetime.now(UTC)
    made = []
    people = [
        ("Naomi Goldrich", "naomi@med.example.edu", PersonCategory.RESIDENT, 5, 2027),
        ("Chad Caraway", "chad@med.example.edu", PersonCategory.RESIDENT, 2, 2030),
        ("Mira Patel", "mira@med.example.edu", PersonCategory.FELLOW, None, 2028),
    ]
    for name, email, role, pgy, class_of in people:
        record = Record(
            site_id=site.id, identity_key=f"email:{email}", identity_kind="email",
            full_name=name, email=email, category=role, position="Resident",
            specialty_normalized="Radiation Oncology", specialty_raw="Radiation Oncology",
            pgy_at_capture=pgy, pgy_capture_date=now.date(),
            class_of=class_of,
            status=RecordStatus.ACTIVE, confidence=0.9, version_count=1,
            last_run_id=run.id, last_changed_at=now,
        )
        session.add(record)
        await session.flush()
        version = RecordVersion(
            record_id=record.id, version_no=1,
            fields={"full_name": name, "email": email},
            changed_fields={}, source_url="https://med.example.edu/residents",
            page_title="Current Residents",
            captured_at=now, extraction_method=ExtractionMethod.DISCOVERY,
            fetch_mode=FetchMode.BOTH, confidence=0.9,
            run_id=run.id,
        )
        session.add(version)
        await session.flush()
        record.current_version_id = version.id
        made.append(record)

    await session.commit()
    return {"site": site, "run": run, "records": made}


class TestAuth:
    async def test_health_needs_no_credential(self, client):
        response = await client.get(f"{API}/health")
        assert response.status_code == 200
        assert response.json()["status"] in ("ok", "degraded")

    async def test_missing_credential_is_401_with_a_branchable_code(self, client):
        response = await client.get(f"{API}/people")
        assert response.status_code == 401
        # The frontend shows the login screen on this code, not an error toast.
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"

    async def test_bad_password_is_distinguishable(self, client):
        response = await client.post(f"{API}/auth/login", json={"password": "nope"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_INVALID_PASSWORD"

    async def test_garbage_token_is_rejected(self, client):
        response = await client.get(
            f"{API}/people", headers={"Authorization": "Bearer nonsense"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_INVALID_TOKEN"

    async def test_valid_token_is_accepted(self, client, auth):
        assert (await client.get(f"{API}/people", headers=auth)).status_code == 200

    async def test_sse_accepts_a_query_token(self, client, token, seeded):
        # EventSource cannot set headers, so this endpoint takes ?token=.
        run_id = seeded["run"].id
        response = await client.get(f"{API}/runs/{run_id}/sites", params={"token": token})
        assert response.status_code == 200


class TestErrorEnvelope:
    async def test_not_found_shape(self, client, auth):
        response = await client.get(f"{API}/people/rec_missing/versions", headers=auth)
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "NOT_FOUND"
        assert isinstance(error["message"], str) and error["message"]

    async def test_validation_error_carries_details(self, client):
        login = await client.post(f"{API}/auth/login", json={"password": "change-me-admin"})
        admin_auth = {"Authorization": f"Bearer {login.json()['token']}"}
        response = await client.post(f"{API}/runs", json={"sites": []}, headers=admin_auth)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_bad_cursor_is_a_clean_code(self, client, auth):
        response = await client.get(
            f"{API}/people", params={"cursor": "!!!bad!!!"}, headers=auth
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_CURSOR"


class TestRecordsQuery:
    async def test_lists_records_with_contract_field_names(self, client, auth, seeded):
        response = await client.get(f"{API}/people", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 3
        row = body["items"][0]
        # `area` is the normalized specialty and `year` the class-of year.
        for field in ("id", "area", "year", "pgy", "category", "position",
                      "hospital", "status", "confidence"):
            assert field in row
        assert row["area"] == "Radiation Oncology"
        assert row["hospital"] == "Example Teaching Hospital"

    async def test_filter_by_area_and_year(self, client, auth, seeded):
        response = await client.get(
            f"{API}/people", params={"area": "Radiation Oncology", "year": 2027},
            headers=auth,
        )
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["year"] == 2027

    async def test_filter_by_category(self, client, auth, seeded):
        response = await client.get(f"{API}/people", params={"category": "fellow"}, headers=auth)
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["category"] == "fellow"

    async def test_free_text_search(self, client, auth, seeded):
        response = await client.get(f"{API}/people", params={"q": "Goldrich"}, headers=auth)
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["full_name"] == "Naomi Goldrich"

    async def test_pgy_filter_uses_the_derived_current_level(self, client, auth, seeded):
        # Seeded today, so current PGY equals the captured PGY.
        response = await client.get(f"{API}/people", params={"pgy": 5}, headers=auth)
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["pgy"] == 5

    async def test_cursor_pagination_walks_without_repeats(self, client, auth, seeded):
        seen: list[str] = []
        cursor = None
        for _ in range(5):
            params = {"limit": 1}
            if cursor:
                params["cursor"] = cursor
            body = (await client.get(f"{API}/people", params=params, headers=auth)).json()
            seen.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                break
        assert len(seen) == 3 and len(set(seen)) == 3


class TestStats:
    async def test_aggregates_so_the_frontend_never_computes_them(
        self, client, auth, seeded
    ):
        response = await client.get(f"{API}/people/stats", headers=auth)
        body = response.json()
        assert body["total"] == 3
        assert body["by_category"]["resident"] == 2
        assert body["by_area"]["Radiation Oncology"] == 3
        assert body["sites_covered"] == 1
        assert body["with_email"] == 3

    async def test_stats_honour_the_same_filters_as_the_list(self, client, auth, seeded):
        response = await client.get(
            f"{API}/people/stats", params={"category": "fellow"}, headers=auth
        )
        assert response.json()["total"] == 1


class TestProvenance:
    async def test_source_returns_full_provenance(self, client, auth, seeded):
        record_id = seeded["records"][0].id
        body = (await client.get(f"{API}/people/{record_id}/source", headers=auth)).json()
        assert body["source_url"] == "https://med.example.edu/residents"
        assert body["page_title"] == "Current Residents"
        assert body["extraction_method"] == "discovery"
        assert body["captured_at"]
        assert body["extraction_method"] == "discovery"

    async def test_versions_expose_a_precomputed_diff(self, client, auth, seeded, session):
        record = seeded["records"][0]
        session.add(
            RecordVersion(
                record_id=record.id, version_no=2,
                fields={"full_name": "Naomi Goldrich-Smith"},
                changed_fields={
                    "full_name": {"from": "Naomi Goldrich", "to": "Naomi Goldrich-Smith"}
                },
                source_url="https://med.example.edu/residents",
                page_title="Current Residents",
                captured_at=datetime.now(UTC),
                extraction_method=ExtractionMethod.KNOWN_PATH,
                fetch_mode=FetchMode.HTML, confidence=0.9,
            )
        )
        await session.commit()

        body = (
            await client.get(f"{API}/people/{record.id}/versions", headers=auth)
        ).json()
        assert len(body) == 2
        latest = body[0]
        assert latest["version_no"] == 2
        change = latest["changed_fields"][0]
        assert change["field"] == "full_name"
        assert change["previous"] == "Naomi Goldrich"
        assert change["current"] == "Naomi Goldrich-Smith"


class TestSitesAndMeta:
    async def test_site_list_and_detail(self, client, auth, seeded):
        listed = (await client.get(f"{API}/sites", headers=auth)).json()
        assert listed["items"][0]["root_domain"] == "med.example.edu"
        assert listed["items"][0]["record_count"] == 3

        site_id = seeded["site"].id
        detail = (await client.get(f"{API}/sites/{site_id}", headers=auth)).json()
        assert detail["hospital"] == "Example Teaching Hospital"
        assert "known_paths" in detail and "recent_runs" in detail

    async def test_meta_backs_the_filter_dropdowns(self, client, auth, seeded):
        areas = (await client.get(f"{API}/meta/areas", headers=auth)).json()["values"]
        years = (await client.get(f"{API}/meta/years", headers=auth)).json()["values"]
        assert areas == ["Radiation Oncology"]
        assert sorted(years) == [2027, 2028, 2030]


class TestRunsAndAdmin:
    async def test_run_detail_and_site_snapshot(self, client, auth, seeded):
        run_id = seeded["run"].id
        detail = (await client.get(f"{API}/runs/{run_id}", headers=auth)).json()
        assert detail["id"] == run_id
        assert "spend_usd" in detail and "records_found" in detail

        # This is the SSE resync path.
        snapshot = (await client.get(f"{API}/runs/{run_id}/sites", headers=auth)).json()
        assert len(snapshot["items"]) == 1
        assert snapshot["items"][0]["domain"] == "med.example.edu"

    async def test_admin_stats_reports_cache_effectiveness(self, client, auth, seeded):
        body = (await client.get(f"{API}/admin/stats", headers=auth)).json()
        # These two show whether the caching is earning its keep.
        assert "skip_rate" in body and "known_path_hit_rate" in body
        assert body["records_total"] == 3
        assert body["sites_total"] == 1

    async def test_admin_site_table_has_success_rates(self, client, auth, seeded):
        row = (await client.get(f"{API}/admin/sites", headers=auth)).json()["items"][0]
        assert "success_rate" in row and "skip_rate" in row
        assert row["total_site_runs"] == 1

    async def test_run_list_pages_past_the_first_page(self, client, auth, session):
        now = datetime.now(UTC)
        for minutes in range(5):
            session.add(Run(status="completed", config={}, created_at=now - timedelta(minutes=minutes)))
        await session.commit()

        seen, cursor = [], None
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            response = await client.get(f"{API}/runs", params=params, headers=auth)
            assert response.status_code == 200, response.text
            body = response.json()
            seen += [r["id"] for r in body["items"]]
            cursor = body["next_cursor"]
            if not body["has_more"]:
                break
        assert len(seen) == len(set(seen)) == 5

    async def test_cancelling_a_finished_run_succeeds_unchanged(self, client, seeded):
        """Stopping something already stopped is what the caller asked for.

        It used to be a 409, which surfaced as "the crawl could not be stopped"
        whenever a run finished between the stop button being drawn and pressed.
        """
        login = await client.post(f"{API}/auth/login", json={"password": "change-me-admin"})
        admin_auth = {"Authorization": f"Bearer {login.json()['token']}"}
        before = seeded["run"].status

        response = await client.post(
            f"{API}/runs/{seeded['run'].id}/cancel", headers=admin_auth
        )

        assert response.status_code == 200
        assert response.json()["status"] == before  # not rewritten to cancelled

    async def test_cancelling_is_repeatable(self, client, seeded):
        login = await client.post(f"{API}/auth/login", json={"password": "change-me-admin"})
        admin_auth = {"Authorization": f"Bearer {login.json()['token']}"}
        for _ in range(3):
            response = await client.post(
                f"{API}/runs/{seeded['run'].id}/cancel", headers=admin_auth
            )
            assert response.status_code == 200


class TestRemovedCsvValidate:
    async def test_csv_preview_upload_endpoint_is_removed(self, client, auth, seeded):
        response = await client.post(
            f"{API}/runs/validate",
            files={"file": ("sites.csv", b"url\nmed.example.edu\n", "text/csv")},
            headers=auth,
        )
        assert response.status_code in (404, 405)
