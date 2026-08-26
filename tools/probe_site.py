"""Diagnostic probe: what does a failing career page actually contain?

Not part of the pipeline - a development tool for deciding how to extend the
scraper. For each URL it reports the signals the scraper could key on:
redirect target, ATS fingerprints, job-like link shapes, search inputs, and
whether an embedded JSON blob carries the job list.

    python tools/probe_site.py "https://careers.example.com/"
    python tools/probe_site.py --from-failures 12
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats.detector import detect_ats, detect_from_html  # noqa: E402
from logger import setup_logging  # noqa: E402

# Any href that plausibly points at a single posting, far broader than the
# scraper's current selectors - the point is to discover what shapes exist.
HREF_HINTS = (
    "/job/", "/jobs/", "/career/", "/careers/", "/position", "/posting",
    "/opening", "/vacanc", "/requisition", "/req/", "/opportunit",
    "jobid=", "jobId=", "reqid=", "requisitionid=", "id=", "/apply",
    "/detail", "/role/",
)

PROBE_JS = """
() => {
  const anchors = [...document.querySelectorAll('a[href]')];
  const hrefShapes = {};
  for (const a of anchors) {
    const href = a.getAttribute('href') || '';
    const text = (a.innerText || '').trim();
    if (!text || text.length < 3) continue;
    // Normalise the href into a coarse "shape" so patterns are visible.
    const shape = href
      .replace(/https?:\\/\\/[^/]+/, '')
      .replace(/\\d{3,}/g, 'N')
      .replace(/[a-f0-9]{8}-[a-f0-9-]{20,}/gi, 'UUID')
      .split('?')[0]
      .split('/').slice(0, 4).join('/');
    if (!hrefShapes[shape]) hrefShapes[shape] = { count: 0, sample: '', text: '' };
    hrefShapes[shape].count++;
    if (!hrefShapes[shape].sample) {
      hrefShapes[shape].sample = href.slice(0, 120);
      hrefShapes[shape].text = text.slice(0, 60);
    }
  }
  const inputs = [...document.querySelectorAll('input')].map(el => ({
    type: el.type, placeholder: el.placeholder, name: el.name,
    id: el.id, ariaLabel: el.getAttribute('aria-label'),
  })).slice(0, 12);
  const iframes = [...document.querySelectorAll('iframe')].map(f => f.src).slice(0, 8);
  return {
    anchorCount: anchors.length,
    hrefShapes,
    inputs,
    iframes,
    title: document.title,
    bodyLen: (document.body ? document.body.innerText.length : 0),
  };
}
"""


def probe(company: str, url: str) -> dict:
    from browser.playwright_scraper import _dismiss_cookie_banner, _get_browser

    report: dict = {"company": company, "url": url}
    browser = _get_browser()
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, ignore_https_errors=True
    )
    page = context.new_page()
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            report["nav_error"] = str(exc)[:160]
            return report

        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        _dismiss_cookie_banner(page)
        page.wait_for_timeout(800)

        report["final_url"] = page.url
        report["redirect_detect"] = detect_ats(page.url)["provider"]

        try:
            html = page.content()
        except Exception:
            html = ""
        report["html_fingerprint"] = detect_from_html(html, final_url=page.url)
        report["html_len"] = len(html)

        # Does the page ship its jobs as embedded JSON?
        for marker in ("phApp.ddo", "__NEXT_DATA__", "window.__INITIAL_STATE__",
                       "application/ld+json", "jobPostings", '"jobs":['):
            if marker.lower() in html.lower():
                report.setdefault("json_markers", []).append(marker)

        data = page.evaluate(PROBE_JS)
        report["title"] = data["title"]
        report["anchor_count"] = data["anchorCount"]
        report["body_len"] = data["bodyLen"]
        report["iframes"] = [i for i in data["iframes"] if i]
        report["inputs"] = [
            i for i in data["inputs"]
            if (i.get("placeholder") or i.get("name") or i.get("ariaLabel"))
        ]

        shapes = data["hrefShapes"]
        joblike = {
            shape: info for shape, info in shapes.items()
            if any(h.lower() in shape.lower() for h in HREF_HINTS)
        }
        report["joblike_shapes"] = sorted(
            joblike.items(), key=lambda kv: -kv[1]["count"]
        )[:8]
        report["top_shapes"] = sorted(
            shapes.items(), key=lambda kv: -kv[1]["count"]
        )[:6]
        return report
    finally:
        for c in (page, context):
            try:
                c.close()
            except Exception:
                pass


def render(report: dict) -> None:
    print("=" * 100)
    print(f"{report['company']}  <-  {report['url']}")
    if "nav_error" in report:
        print(f"  NAV FAILED: {report['nav_error']}")
        return
    print(f"  final_url : {report.get('final_url')}")
    print(f"  title     : {report.get('title')}")
    print(f"  detect    : redirect={report.get('redirect_detect')} "
          f"html={report.get('html_fingerprint')}")
    print(f"  size      : html={report.get('html_len')} body_text={report.get('body_len')} "
          f"anchors={report.get('anchor_count')}")
    if report.get("json_markers"):
        print(f"  json      : {report['json_markers']}")
    if report.get("iframes"):
        for f in report["iframes"]:
            print(f"  iframe    : {f[:110]}")
    for i in report.get("inputs", []):
        print(f"  input     : ph={i.get('placeholder')!r} name={i.get('name')!r} "
              f"aria={i.get('ariaLabel')!r}")
    if report.get("joblike_shapes"):
        print("  JOB-LIKE LINK SHAPES:")
        for shape, info in report["joblike_shapes"]:
            print(f"    {info['count']:>4}x  {shape:<45} e.g. {info['sample'][:60]}")
    else:
        print("  JOB-LIKE LINK SHAPES: none")
        print("  top shapes:")
        for shape, info in report.get("top_shapes", []):
            print(f"    {info['count']:>4}x  {shape:<45} {info['text'][:40]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--from-failures", type=int, default=0)
    parser.add_argument("--provider", default="unknown")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    setup_logging("logs/probe.log", "WARNING", quiet=True)

    targets: list[tuple[str, str]] = [(u, u) for u in args.urls]
    if args.from_failures:
        import pandas as pd
        f = pd.read_csv("output/scraper_failures.csv")
        f = f[f.ats_provider == args.provider]
        f = f.drop_duplicates(subset=["company"])
        targets = [(r.company, r.url) for _, r in f.head(args.from_failures).iterrows()]

    reports = []
    try:
        for company, url in targets:
            try:
                rep = probe(company, url)
            except Exception as exc:
                rep = {"company": company, "url": url, "nav_error": f"probe crash: {exc}"}
            reports.append(rep)
            render(rep)
    finally:
        from browser.playwright_scraper import shutdown_browsers
        shutdown_browsers()

    if args.out:
        Path(args.out).write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
