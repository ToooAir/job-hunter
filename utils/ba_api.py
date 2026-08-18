"""ba_api.py — Bundesagentur für Arbeit endpoint facts, shared.

phase1 ingests from this API and ats_scan probes it for liveness, so the host,
the key and the URL shapes live in one place: the source once died silently for
four months because the endpoint moved, and a second copy of these constants is
a second thing to forget.

The old host (api.arbeitsagentur.de/jobsuche/v2) is decommissioned — it answers
everything with an HTML maintenance page under HTTP 200. The live endpoint is
the one the public job search SPA itself calls, read off
https://www.arbeitsagentur.de/jobsuche/config/config.js. Note it is v6, and the
key matters: 'jobboerse-jobsuche' returns 200, 'jobboerse' and no key both 403.

Pure stdlib.
"""

import base64
import re

BA_API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
BA_SEARCH = f"{BA_API}/pc/v6/jobs"
BA_DETAIL = f"{BA_API}/pc/v4/jobdetails"
BA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "application/json",
    "X-API-Key": "jobboerse-jobsuche",
}

BA_PUBLIC_JOB = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
_BA_URL_RE = re.compile(r"arbeitsagentur\.de/jobsuche/jobdetail/([^/?#]+)", re.I)


def ba_job_url(refnr: str) -> str:
    """The public page for a Referenznummer — what we store as jobs.url."""
    return f"{BA_PUBLIC_JOB}{refnr}"


def ba_refnr_from_url(url: str | None) -> str | None:
    """The Referenznummer a public BA job URL carries, or None."""
    m = _BA_URL_RE.search(url or "")
    return m.group(1) if m else None


def ba_detail_url(refnr: str) -> str:
    """The detail endpoint keys on the BASE64 of the reference number — the raw
    form 404s, which reads exactly like a taken-down posting."""
    encoded = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
    return f"{BA_DETAIL}/{encoded}"
