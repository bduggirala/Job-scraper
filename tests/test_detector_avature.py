"""Detection tests for self-hosted Avature portals.

Some Avature customers (e.g. Deloitte at apply.deloitte.com) run the portal on
their own domain, so the URL carries no ``avature.net`` host and the resolver
must fall back to the HTML fingerprint. Self-hosted Avature SPAs bootstrap a
global ``avature.portal`` config object, which is the signal we key on.
"""

from ats.detector import AVATURE, UNKNOWN, detect_from_html

# Trimmed to the marker the fingerprint reads - the branded page never mentions
# avature.net, only the client-side portal config globals.
DELOITTE_HTML = (
    "<html><head><script>"
    "window.avature = window.avature || {};"
    "avature.portal = {id: 'deloitte', lang: 'en', urlPath: '/careers'};"
    "</script></head><body><h1>Deloitte Careers</h1></body></html>"
)


def test_self_hosted_avature_detected_from_portal_global():
    assert detect_from_html(DELOITTE_HTML, final_url="https://apply.deloitte.com/") == AVATURE


def test_plain_marketing_page_is_not_avature():
    html = "<html><body><h1>Careers</h1><p>join our portal team</p></body></html>"
    assert detect_from_html(html, final_url="https://acme.com/careers/") == UNKNOWN
