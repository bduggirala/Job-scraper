"""A problem company must not take healthy companies down with it.

The damage is real and was measured: PwC, CBRE and Slalom each ran past the
per-company limit within ten minutes of each other, all three Playwright
workers were then permanently held, throughput went to zero, and the fourteen
companies queued behind them were written off on the phase budget. Goldman
Sachs 820 jobs -> 0, IBM 1,016 -> 0, Jacobs 720 -> 0, Verizon 394 -> 0. Three
bad sites cost seventeen companies.

The obvious fix - give the pool spare threads so an abandoned company's slot
can be reused - was implemented, measured, and reverted. Playwright's sync API
is thread-affine, so no other thread can close a wedged worker's browser; the
replacement therefore starts a *second* Chromium beside the first. Six
concurrent instances against a measured ceiling of five turned a 43-minute run
with 3 failures into a 3h13m run with 19. The scarce resource is browsers, not
threads, and no amount of thread juggling creates more of them.

What does work costs nothing: run the known problem companies **last**. Then
whatever a wedged company blocks, it blocks companies that were already the
slowest or already timed out - and if the phase budget expires, it expires on
them rather than on healthy employers that merely queued behind them.
"""

from __future__ import annotations

import json

from ats.router import METHOD_API, METHOD_BROWSER, SOURCE_ATS_URL, RoutePlan
from pipeline import previous_costs, slowest_last


def _plan(name: str, method: str = METHOD_BROWSER) -> RoutePlan:
    return RoutePlan(
        company=name, url=f"https://{name}.example/jobs",
        provider="unknown", method=method, source=SOURCE_ATS_URL,
    )


def _report(tmp_path, rows):
    path = tmp_path / "last_run.json"
    path.write_text(json.dumps({"companies": rows}), encoding="utf-8")
    return path


# --- ordering --------------------------------------------------------------

def test_a_company_that_timed_out_runs_last():
    costs = {"omnicell": (True, 900.0), "acme": (False, 12.0), "globex": (False, 30.0)}
    plans = [_plan("omnicell"), _plan("acme"), _plan("globex")]

    order = [p.company for p in slowest_last(plans, costs)]

    assert order[-1] == "omnicell"
    assert order[:2] == ["acme", "globex"], "healthy companies keep duration order"


def test_every_timeout_runs_after_every_healthy_company():
    """Even a slow-but-successful company outranks a fast timeout."""
    costs = {
        "slow_ok": (False, 800.0),
        "fast_timeout": (True, 5.0),
        "quick": (False, 3.0),
    }
    order = [p.company for p in slowest_last(
        [_plan("fast_timeout"), _plan("slow_ok"), _plan("quick")], costs)]

    assert order == ["quick", "slow_ok", "fast_timeout"]


def test_the_slowest_healthy_company_runs_later_than_the_quickest():
    costs = {"a": (False, 5.0), "b": (False, 500.0), "c": (False, 50.0)}
    order = [p.company for p in slowest_last(
        [_plan("b"), _plan("a"), _plan("c")], costs)]
    assert order == ["a", "c", "b"]


def test_an_unknown_company_is_treated_as_healthy():
    """A company added to the workbook today must not be deprioritised."""
    costs = {"omnicell": (True, 900.0)}
    order = [p.company for p in slowest_last(
        [_plan("omnicell"), _plan("brand_new")], costs)]
    assert order == ["brand_new", "omnicell"]


def test_no_company_is_ever_dropped():
    """Deprioritised, never skipped - a site that recovers is still scraped."""
    costs = {"omnicell": (True, 900.0), "slalom": (True, 600.0)}
    plans = [_plan(n) for n in ("omnicell", "slalom", "acme", "globex")]

    order = slowest_last(plans, costs)

    assert len(order) == 4
    assert {p.company for p in order} == {"omnicell", "slalom", "acme", "globex"}


def test_ordering_is_stable_with_no_history():
    """A first run has no report; workbook order must survive."""
    plans = [_plan(n) for n in ("c", "a", "b")]
    assert [p.company for p in slowest_last(plans, {})] == ["c", "a", "b"]


# --- reading the previous run ---------------------------------------------

def test_costs_are_read_from_the_previous_report(tmp_path):
    path = _report(tmp_path, [
        {"company": "Omnicell", "error_type": "Timeout",
         "error_message": "Exceeded the 900s per-company limit",
         "duration_seconds": 900.4},
        {"company": "Acme", "error_type": None, "duration_seconds": 11.2},
    ])
    costs = previous_costs(path)

    assert costs["Omnicell"] == (True, 900.4)
    assert costs["Acme"] == (False, 11.2)


def test_a_missing_report_is_not_an_error(tmp_path):
    assert previous_costs(tmp_path / "absent.json") == {}


def test_a_corrupt_report_is_not_an_error(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text("{not json", encoding="utf-8")
    assert previous_costs(path) == {}


def test_a_row_without_a_duration_is_tolerated(tmp_path):
    path = _report(tmp_path, [
        {"company": "Acme", "error_type": "Timeout",
         "error_message": "Exceeded the 600s per-company limit"},
    ])
    assert previous_costs(path)["Acme"] == (True, 0.0)


def test_blocked_is_not_treated_as_a_timeout(tmp_path):
    """A bot challenge is fast and cheap; it costs no one anything."""
    path = _report(tmp_path, [
        {"company": "Infosys", "error_type": "AccessDenied", "duration_seconds": 8.0},
    ])
    assert previous_costs(path)["Infosys"] == (False, 8.0)


def test_api_and_browser_phases_are_ordered_independently():
    """The two pools have separate budgets, so each needs its own tail."""
    costs = {"api_bad": (True, 900.0), "browser_bad": (True, 600.0)}
    api = [_plan("api_bad", METHOD_API), _plan("api_ok", METHOD_API)]
    browser = [_plan("browser_bad"), _plan("browser_ok")]

    assert [p.company for p in slowest_last(api, costs)] == ["api_ok", "api_bad"]
    assert [p.company for p in slowest_last(browser, costs)] == ["browser_ok", "browser_bad"]


def test_a_phase_budget_victim_is_not_demoted(tmp_path):
    """The distinction that matters: offender vs collateral.

    Both arrive as ``error_type: "Timeout"``. One burned its own per-company
    limit; the other was simply queued behind it when the phase budget ran out.
    Demoting the second would push fourteen healthy employers to the back of
    the queue and leave the actual offender where it was.
    """
    path = _report(tmp_path, [
        {"company": "Omnicell", "error_type": "Timeout",
         "error_message": "Exceeded the 900s per-company limit",
         "duration_seconds": 900.0},
        {"company": "Verizon", "error_type": "Timeout",
         "error_message": "Exceeded the browser phase budget of 3000s",
         "duration_seconds": 0.0},
    ])
    costs = previous_costs(path)

    assert costs["Omnicell"][0] is True, "burned its own limit - a real offender"
    assert costs["Verizon"][0] is False, "collateral damage, not a problem company"

    order = [p.company for p in slowest_last(
        [_plan("Omnicell"), _plan("Verizon")], costs)]
    assert order == ["Verizon", "Omnicell"]
