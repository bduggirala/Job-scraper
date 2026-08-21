from ats.html_utils import extract_job_links

HTML = """
<html><body>
  <ul>
    <li>
      <a href="/job/123">Data Engineer</a>
      <span class="jobLocation">Irving, TX</span>
      <span class="jobDate">Aug 18, 2026</span>
    </li>
    <li>
      <a href="/job/456">ETL Developer</a>
      <span class="jobLocation">Frisco, TX</span>
      <time datetime="2026-08-19">yesterday</time>
    </li>
  </ul>
</body></html>
"""

BASE = "https://tenant.example.com/search/"


def test_extracts_date_from_class_marker():
    rows = extract_job_links(HTML, BASE)
    row = next(r for r in rows if r["title"] == "Data Engineer")
    assert row["date_posted"] == "Aug 18, 2026"


def test_prefers_time_datetime_attribute():
    rows = extract_job_links(HTML, BASE)
    row = next(r for r in rows if r["title"] == "ETL Developer")
    assert row["date_posted"] == "2026-08-19"


def test_location_still_extracted():
    rows = extract_job_links(HTML, BASE)
    row = next(r for r in rows if r["title"] == "Data Engineer")
    assert row["location"] == "Irving, TX"


def test_date_is_none_when_absent():
    rows = extract_job_links('<a href="/job/9">Analytics Engineer</a>', BASE)
    assert rows[0]["date_posted"] is None
