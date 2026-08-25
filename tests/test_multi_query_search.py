"""Multi-query search on career sites whose jobs only exist behind a search.

One configured term was the only view the pipeline ever got of the ~89
browser-routed companies. Roles that never contain the word "data" - Snowflake
Engineer, Databricks Engineer, ETL Developer, Analytics Engineer - were
therefore invisible on every search-driven site, and those are explicitly on
the target list.

Results from each query must merge and de-duplicate, and each row must record
which query found it so provenance survives into the output.
"""

import pytest

from browser.playwright_scraper import _search_fallback


@pytest.fixture
def browser_ctx():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    yield ctx
    browser.close()
    pw.stop()


# A search box that renders results matching the typed keyword. "data" finds
# two rows; "snowflake" finds a different one that "data" never would.
SEARCH_PAGE = """
<html><body>
  <input id="q" name="keyword" placeholder="Search jobs by title or keyword">
  <button onclick="run()">Search</button>
  <div id="results"></div>
  <script>
    var DB = [
      {t: 'Senior Data Engineer', u: '/jobs/1', k: 'data'},
      {t: 'Data Platform Engineer', u: '/jobs/2', k: 'data'},
      {t: 'Snowflake Engineer', u: '/jobs/3', k: 'snowflake'},
      {t: 'ETL Developer', u: '/jobs/4', k: 'etl'}
    ];
    function run() {
      var q = document.getElementById('q').value.toLowerCase();
      var out = '';
      for (var i = 0; i < DB.length; i++) {
        if (DB[i].k.indexOf(q) >= 0 || DB[i].t.toLowerCase().indexOf(q) >= 0) {
          out += '<a href="' + DB[i].u + '">' + DB[i].t + '</a>';
        }
      }
      document.getElementById('results').innerHTML = out;
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') run();
    });
  </script>
</body></html>
"""


def _page(ctx, html=SEARCH_PAGE):
    ctx.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body=html))
    page = ctx.new_page()
    page.goto("https://example.test/")
    return page


def test_multiple_queries_find_roles_a_single_term_would_miss(browser_ctx, monkeypatch):
    """The headline gap: "Snowflake Engineer" contains no "data"."""
    monkeypatch.setenv("PYTEST_SEARCH_TERMS", "1")
    page = _page(browser_ctx)

    result = _search_fallback(
        "Acme", page, timeout_ms=5000,
        search_terms=["Data", "Snowflake", "ETL"],
    )

    titles = {row["title"] for row in result.jobs}
    assert "Senior Data Engineer" in titles
    assert "Snowflake Engineer" in titles, "a single 'Data' query would miss this"
    assert "ETL Developer" in titles


def test_results_from_several_queries_are_deduplicated(browser_ctx):
    """"data" and "engineer" both return the Data Engineer rows."""
    page = _page(browser_ctx)

    result = _search_fallback(
        "Acme", page, timeout_ms=5000,
        search_terms=["Data", "Data", "Data"],
    )

    urls = [row["job_url"] for row in result.jobs]
    assert len(urls) == len(set(urls)), "the same job was returned more than once"


def test_each_row_records_the_query_that_found_it(browser_ctx):
    page = _page(browser_ctx)

    result = _search_fallback(
        "Acme", page, timeout_ms=5000,
        search_terms=["Data", "Snowflake"],
    )

    by_title = {row["title"]: row.get("source_query") for row in result.jobs}
    assert by_title.get("Snowflake Engineer") == "Snowflake"
    assert by_title.get("Senior Data Engineer") == "Data"


def test_the_query_list_is_bounded(browser_ctx):
    """A long list must not run unbounded against one company."""
    page = _page(browser_ctx)

    result = _search_fallback(
        "Acme", page, timeout_ms=5000,
        search_terms=["Data", "Snowflake", "ETL", "Databricks", "Analytics"],
        max_queries=2,
    )

    assert len(result.queries_run) == 2


def test_a_page_with_no_search_input_returns_nothing_without_erroring(browser_ctx):
    page = _page(browser_ctx, "<html><body><p>No search here</p></body></html>")

    result = _search_fallback("Acme", page, timeout_ms=5000, search_terms=["Data"])

    assert result.jobs == []
