"""Screen-reader field labels, read as if they were data.

iCIMS renders each job card with an accessibility label inside the anchor and
the location in a sibling column labelled the same way::

    <li class="iCIMS_JobCardItem">
      <div class="col-xs-6 header left">
        <span class="sr-only field-label">Job Locations</span>
        <span> US-TX-Westlake</span>
      </div>
      <div class="col-xs-12 title">
        <a class="iCIMS_Anchor" href="...">
          <span class="sr-only field-label">Job Posting Title</span>
          <h3>Senior Manager, Service Design</h3>
        </a>
      </div>
    </li>

``sr-only`` means "present for assistive technology, invisible on screen" - the
span is a field *name*, never its value. Two things went wrong with it:

* every extracted title carried the label as a prefix - "Job Posting Title
  Senior Manager, Service Design", "Title On Premise Manager", "Job Title
  Associate Specialist" across three different tenants;
* the location was never found at all, because ``_nearby_location`` searches for
  an element whose *class* names a location, and here the class is ``sr-only
  field-label`` while the word "Location" is in the label's *text*.

Measured on a 120,003-row harvest: 739 iCIMS rows had no location, at a rate of
100% for each affected tenant - Charles Schwab 309 of 309, Aerotek 223 of 223.
A blank location fails the DFW match, so every one of those postings was
dropped regardless of where it was. Schwab's are in ``US-TX-Westlake``, and
Westlake is on the configured DFW city list.

Both fixes are in the shared HTML helpers rather than the iCIMS collector:
``sr-only``/``visually-hidden``/``field-label`` is a standard accessibility
idiom, and every HTML-scraping tier reads these pages the same way.
"""

import pytest

from ats.html_utils import extract_job_links

BASE = "https://career-schwab.icims.com/jobs/search"

CARD = """
<ul class="container-fluid iCIMS_JobsTable">
  <li class="iCIMS_JobCardItem">
    <div class="row">
      <div class="col-xs-6 header left">
        <span class="sr-only field-label">Job Locations</span>
        <span> US-TX-Westlake</span>
      </div>
      <div class="col-xs-12 title">
        <a href="https://career-schwab.icims.com/jobs/125762/data-engineer/job"
           class="iCIMS_Anchor" title="125762 - Data Engineer">
          <span class="sr-only field-label">Job Posting Title</span>
          <h3> Senior Data Engineer</h3>
        </a>
      </div>
    </div>
  </li>
</ul>
"""


@pytest.fixture
def row():
    links = extract_job_links(CARD, BASE, selector="a.iCIMS_Anchor")
    assert len(links) == 1
    return links[0]


def test_the_accessibility_label_is_not_part_of_the_title(row):
    assert row["title"] == "Senior Data Engineer", (
        f"title came out as {row['title']!r}"
    )


def test_the_location_beside_its_label_is_found(row):
    assert row["location"] == "US-TX-Westlake", (
        f"location came out as {row['location']!r}; the row is unfilterable"
    )


def test_the_recovered_location_actually_matches_dfw(row):
    """The outcome that matters: Westlake, TX is a configured DFW city."""
    from filters import LocationMatcher

    matched, reason = LocationMatcher().matches(row)
    assert matched and reason == "dfw", (
        f"{row['location']!r} did not match as DFW"
    )


@pytest.mark.parametrize("label_class", [
    "sr-only field-label", "visually-hidden", "screen-reader-text",
    "sr-only", "a11y-hidden",
])
def test_every_common_screen_reader_class_is_stripped(label_class):
    html = f"""
    <a href="https://x.test/jobs/1" class="c">
      <span class="{label_class}">Job Title</span>
      <h3>Data Engineer</h3>
    </a>"""
    assert extract_job_links(html, "https://x.test", selector="a.c")[0]["title"] \
        == "Data Engineer"


def test_a_title_with_no_label_is_untouched():
    html = '<a href="https://x.test/jobs/1" class="c"><h3>Data Engineer</h3></a>'
    assert extract_job_links(html, "https://x.test", selector="a.c")[0]["title"] \
        == "Data Engineer"


def test_a_class_marked_location_still_wins_over_a_labelled_one():
    """The existing class-based lookup must keep working; it is more direct."""
    html = """
    <div class="card">
      <span class="job-location">Dallas, TX</span>
      <a href="https://x.test/jobs/1" class="c"><h3>Data Engineer</h3></a>
    </div>"""
    assert extract_job_links(html, "https://x.test", selector="a.c")[0]["location"] \
        == "Dallas, TX"


def test_a_label_naming_something_else_is_not_read_as_a_location():
    html = """
    <div class="card">
      <div><span class="sr-only field-label">Requisition ID</span><span>2026-125762</span></div>
      <a href="https://x.test/jobs/1" class="c"><h3>Data Engineer</h3></a>
    </div>"""
    row = extract_job_links(html, "https://x.test", selector="a.c")[0]
    assert row["location"] != "2026-125762", "a requisition id became the location"


def test_a_card_whose_only_text_is_a_label_yields_no_title():
    """Stripping must not turn a label-only anchor into an empty-titled row."""
    html = '<a href="https://x.test/jobs/1" class="c"><span class="sr-only">Job Title</span></a>'
    assert extract_job_links(html, "https://x.test", selector="a.c") == []
