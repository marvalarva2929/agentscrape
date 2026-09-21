from agentscrape.school_catalog import EXPECTED_SCHOOLS, load_catalog


def test_shipped_catalog_has_exactly_23_unique_valid_schools():
    schools = load_catalog()
    assert len(schools) == EXPECTED_SCHOOLS
    assert len({school.domain for school in schools}) == EXPECTED_SCHOOLS
    assert all(school.name and school.website.startswith("https://") and school.directory_url.startswith("https://") for school in schools)
