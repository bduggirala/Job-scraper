"""Jobs whose identity lives in the query string must survive as distinct jobs.

Several enterprise ATS platforms put the requisition id in the query rather
than the path: UKG serves ``.../OpportunityDetail?opportunityId=<uuid>``,
Taleo ``.../jobdetail.ftl?job=<id>``, Infor ``.../shorturl.do?key=<id>``,
TEKsystems ``/v1/s?params=<blob>``. Every posting a company lists then shares
one path and differs only after the ``?``.

Both identity layers dropped the query wholesale, so all of them collapsed
into one. Measured against the raw output of a real full run (120,003 rows):
GameStop's 5,148 distinct UKG postings reduced to a single row, BAE Systems'
1,858 to one, and 8,423 distinct postings were destroyed across 18 companies.

The tracking-parameter defence that ``drop_query=True`` was reaching for is
already provided by ``normalize_url`` itself, which strips
``normalize.TRACKING_PARAMS`` and sorts what is left - so keeping the query
costs nothing and preserves the identity.
"""

import pytest

from deduplicate import deduplicate
from job_identity import extract_stable_job_id

UKG = ("https://gamestop.rec.pro.ukg.net/GAM1502GMSP/JobBoard/4a726edc/"
       "OpportunityDetail?opportunityId={}")
TALEO = "https://acme.taleo.net/careersection/2/jobdetail.ftl?job={}"
INFOR = "https://gen-childrens-prd.inforcloudsuite.com/hcm/xmlhttp/shorturl.do?key={}"


def _rows(template, ids, provider, company="Acme"):
    rows = []
    for value in ids:
        url = template.format(value)
        rows.append({
            "company": company, "title": f"Data Engineer {value}",
            "location": "Dallas, TX", "job_url": url, "ats_provider": provider,
            "job_id": extract_stable_job_id(url, provider, company),
        })
    return rows


# --- identity --------------------------------------------------------------

@pytest.mark.parametrize("template,provider,ids", [
    (UKG, "ukg", ["410aea4b-eb0c", "9f9b8e16-43f5", "4464171d-87da"]),
    (TALEO, "taleo", ["1001", "1002", "1003"]),
    (INFOR, "unknown", ["ZB0", "ZB7", "ZB8"]),
])
def test_each_posting_gets_its_own_job_id(template, provider, ids):
    """job_id is the jobs-table primary key: a shared id merges real jobs."""
    rows = _rows(template, ids, provider)
    assert len({r["job_id"] for r in rows}) == len(ids), (
        f"{provider}: {len(ids)} postings collapsed to "
        f"{len({r['job_id'] for r in rows})} id(s): {[r['job_id'] for r in rows]}"
    )


def test_an_opaque_identity_blob_still_separates_postings():
    """TEKsystems encodes the whole posting into one ``params`` value."""
    rows = _rows("https://apply.teksystems.com/v1/s?jdg=false&opco=TEK&params={}",
                 ["puJS0HIv8T8k", "gaDHQH2Q2u", "jPuyFoZpYV"], "unknown")
    assert len({r["job_id"] for r in rows}) == 3


# --- deduplication ---------------------------------------------------------

@pytest.mark.parametrize("template,provider,ids", [
    (UKG, "ukg", ["410aea4b-eb0c", "9f9b8e16-43f5", "4464171d-87da"]),
    (TALEO, "taleo", ["1001", "1002", "1003"]),
    (INFOR, "unknown", ["ZB0", "ZB7", "ZB8"]),
])
def test_distinct_postings_survive_deduplication(template, provider, ids):
    rows = _rows(template, ids, provider)
    result = deduplicate(rows)
    assert len(result["jobs"]) == len(ids), (
        f"{provider}: deduplication destroyed "
        f"{len(ids) - len(result['jobs'])} real posting(s)"
    )


# --- the behaviour dropping the query was meant to protect ------------------

def test_links_differing_only_by_tracking_parameters_still_collapse():
    """The reason the query was dropped at all - still has to hold."""
    base = "https://boards.greenhouse.io/acme/jobs/4001"
    rows = []
    for suffix in ("?gh_src=abc", "?utm_source=li&utm_campaign=x", ""):
        url = base + suffix
        rows.append({
            "company": "Acme", "title": "Data Engineer", "location": "Dallas, TX",
            "job_url": url, "ats_provider": "greenhouse",
            "job_id": extract_stable_job_id(url, "greenhouse", "Acme"),
        })

    assert len({r["job_id"] for r in rows}) == 1, "tracking params split one job"
    assert len(deduplicate(rows)["jobs"]) == 1


def test_one_requisition_reached_by_two_paths_still_collapses():
    """Slalom links the same jobId as JobDetail, Login and ApplicationMethods."""
    rows = []
    for path in ("JobDetail", "Login", "ApplicationMethods"):
        url = f"https://jobs.slalom.com/en_US/careersmarketplace/{path}?jobId=3402"
        rows.append({
            "company": "Slalom", "title": "Data Engineer", "location": "Dallas, TX",
            "job_url": url, "ats_provider": "unknown",
            "job_id": extract_stable_job_id(url, "unknown", "Slalom"),
        })

    assert len({r["job_id"] for r in rows}) == 1
    assert len(deduplicate(rows)["jobs"]) == 1


def test_the_same_posting_from_two_collectors_still_collapses():
    """API and browser reach one requisition by different URLs."""
    api = {"company": "Acme", "title": "Data Engineer", "location": "Dallas, TX",
           "job_url": "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1001",
           "ats_provider": "taleo", "scraping_method": "direct_api"}
    browser = {"company": "Acme", "title": "Data Engineer", "location": "Dallas, TX",
               "job_url": "https://careers.acme.com/apply?job=1001",
               "ats_provider": "unknown", "scraping_method": "browser"}
    for row in (api, browser):
        row["job_id"] = extract_stable_job_id(
            row["job_url"], row["ats_provider"], row["company"])

    result = deduplicate([api, browser])
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["scraping_method"] == "direct_api", (
        "the richer direct-API copy should win"
    )
