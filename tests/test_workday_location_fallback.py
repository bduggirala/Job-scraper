"""Workday tenants that never send ``locationsText``.

The collector read ``locationsText`` or ``locations`` and nothing else. Some
tenants send neither, and for those every row came back with a blank location -
which the DFW filter then drops, so **not one job from those employers could
ever match**, wherever it actually was.

Measured on a 120,003-row full-run harvest: 4,635 rows had no location at all,
and the affected companies were blank at a rate of exactly 100% - Accenture
1,130 of 1,130, Thomson Reuters 468 of 468, Charles Schwab 309 of 309. Not a
scattering of incomplete postings; a field that is simply never read.

Probing six live tenants shows the shape precisely, and it is a clean split:

    Capital One      locationsText='McLean, VA'      bulletFields=['R999094']
    Bank of America  locationsText='4 Locations'     bulletFields=['26031220']
    McKesson         locationsText='USA, NJ, ...'    bulletFields=['JR0148744']
    Centene          locationsText='Remote-IN'       bulletFields=['1645097']
    Accenture        locationsText=None              bulletFields=['R00352812', 'Heredia']
    Thomson Reuters  locationsText=None              bulletFields=['Pyrmont',
                                                                  'New South Wales',
                                                                  'JREQ201133']

A tenant that sends ``locationsText`` puts only the requisition id in
``bulletFields``; a tenant that does not puts the location there as well. So the
fallback can only ever fire where there is nothing to lose, and the requisition
id it must discard is the one already spelled out at the end of
``externalPath`` (``..._R00352812``) - which makes the exclusion exact rather
than a guess about what a job id looks like.

``externalPath``'s own location segment is deliberately *not* used as a further
fallback. It is a slug, and hyphens separate words and administrative levels
alike ("Australia-Pyrmont-New-South-Wales"), so un-slugging it invents a
location rather than reading one. ``bulletFields`` covers both tenants that
were actually failing.
"""

import ats.workday as workday_module
from ats.workday import WorkdayCollector


def _collector():
    return WorkdayCollector("Accenture", {
        "provider": "workday", "host": "accenture.wd103.myworkdayjobs.com",
        "tenant": "accenture", "site": "AccentureCareers",
        "url": "https://accenture.wd103.myworkdayjobs.com/en-US/AccentureCareers/"})


def _run(monkeypatch, postings):
    def fake(url, payload, **kw):
        if payload.get("offset", 0):
            return {"total": len(postings), "jobPostings": []}
        return {"total": len(postings), "jobPostings": postings}

    monkeypatch.setattr(workday_module.http_client, "post_json", fake)
    return _collector().collect().jobs


def test_the_location_is_recovered_from_bullet_fields(monkeypatch):
    """The Accenture shape: requisition id first, location second."""
    jobs = _run(monkeypatch, [{
        "title": "Data Engineer",
        "externalPath": "/job/Heredia/Data-Engineer_R00352812",
        "postedOn": "Posted Today",
        "bulletFields": ["R00352812", "Heredia"],
    }])

    assert jobs[0]["location"] == "Heredia", (
        f"location was {jobs[0]['location']!r}; the whole tenant is unfilterable"
    )


def test_several_location_bullets_are_joined(monkeypatch):
    """The Thomson Reuters shape: city, region, then the requisition id."""
    jobs = _run(monkeypatch, [{
        "title": "Senior Data Engineer",
        "externalPath": "/job/Australia-Pyrmont-New-South-Wales/Senior_JREQ201133",
        "postedOn": "Posted Today",
        "bulletFields": ["Pyrmont", "New South Wales", "JREQ201133"],
    }])

    assert jobs[0]["location"] == "Pyrmont, New South Wales"


def test_the_requisition_id_never_becomes_the_location(monkeypatch):
    """A tenant that sends only the req id must yield no location, not 'R999094'."""
    jobs = _run(monkeypatch, [{
        "title": "Data Engineer",
        "externalPath": "/job/McLean-VA/Data-Engineer_R999094-2",
        "postedOn": "Posted Today",
        "bulletFields": ["R999094"],
    }])

    assert jobs[0]["location"] in (None, ""), (
        f"the requisition id leaked into the location as {jobs[0]['location']!r}"
    )


def test_locations_text_still_wins_when_the_tenant_sends_it(monkeypatch):
    """No behaviour change for the tenants that were already working."""
    jobs = _run(monkeypatch, [{
        "title": "Data Engineer",
        "externalPath": "/job/McLean-VA/Data-Engineer_R999094",
        "postedOn": "Posted Today",
        "locationsText": "McLean, VA",
        "bulletFields": ["R999094", "Somewhere Else"],
    }])

    assert jobs[0]["location"] == "McLean, VA"


def test_a_dfw_bullet_location_survives_into_a_filterable_row(monkeypatch):
    """The point of the fix, stated as the outcome that matters."""
    from filters import LocationMatcher

    jobs = _run(monkeypatch, [{
        "title": "Data Engineer",
        "externalPath": "/job/Irving/Data-Engineer_R00399999",
        "postedOn": "Posted Today",
        "bulletFields": ["R00399999", "Irving", "Texas"],
    }])

    matched, reason = LocationMatcher().matches(jobs[0])
    assert matched and reason == "dfw", (
        f"an Irving, Texas posting was still unfilterable: {jobs[0]['location']!r}"
    )


def test_bullet_fields_that_are_not_a_list_are_ignored(monkeypatch):
    jobs = _run(monkeypatch, [{
        "title": "Data Engineer",
        "externalPath": "/job/Heredia/Data-Engineer_R1",
        "postedOn": "Posted Today",
        "bulletFields": "R1",
    }])
    assert jobs, "a malformed bulletFields dropped the row entirely"
