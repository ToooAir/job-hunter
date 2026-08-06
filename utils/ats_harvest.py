"""ats_harvest.py — grow the direct-ATS scraper seed lists from the corpus.

The direct-ATS scrapers (greenhouse/ashby/lever/personio/workable) each take a
hand-curated list of tenant slugs. But ats_scan already detects, across the
whole corpus, hundreds of companies that sit on exactly these ATSes — surfaced
by the aggregators (wearedevelopers/heise/wttj/arbeitnow). Harvesting their
tenant slug from the resolved apply_url turns each into a direct, 100%-reachable
source: the direct scraper then pulls that company's FULL current openings, not
just the one job that leaked through an aggregator.

Self-reinforcing: the aggregators discover companies, ats_scan tags the ATS,
this harvest feeds them back as seeds, the direct scraper deep-scrapes them.

Pure stdlib (+ a lazy utils.geo_de import for the geo gate): phase1 imports it
without pulling in requests/bs4.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ATSes that (a) have a wired direct scraper in phase1 and (b) encode the tenant
# in the apply_url host/path.
WIRED_ATS = ("greenhouse", "ashby", "lever", "personio", "workable")

SEEDS_PATH = str(Path(__file__).resolve().parents[1] / "data" / "ats_seeds.json")

# A plausible tenant slug: alnum start, then alnum/dot/dash/underscore. Ashby
# tenants can carry a domain suffix ("taxfix.com"), hence the dot.
_SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,60}$")
# path segments that are the ATS's own routing, never a tenant
_NON_TENANT = frozenset({"embed", "job", "jobs", "o", "careers", "search", "api"})


def extract_ats_slug(ats: str, apply_url: str | None, url: str | None = None) -> str | None:
    """The tenant slug for a structured-ATS job, or None. Reads apply_url first
    (the real ATS link ats_scan resolved), falling back to url."""
    p = urlparse(apply_url or url or "")
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]
    seg0 = segs[0] if segs else ""
    slug = ""
    if ats == "greenhouse" and "greenhouse.io" in host:
        # job-boards[.eu].greenhouse.io/<slug>/..., boards.greenhouse.io/<slug>,
        # or the embed form boards.greenhouse.io/embed/job_board/js?for=<slug>
        slug = (parse_qs(p.query).get("for") or [""])[0] if seg0 == "embed" else seg0
    elif ats == "ashby" and "ashbyhq.com" in host:
        slug = seg0
    elif ats == "lever" and "lever.co" in host:
        slug = seg0
    elif ats == "workable" and "workable.com" in host:
        slug = seg0
    elif ats == "personio" and ".jobs.personio." in host:
        slug = host.split(".jobs.personio.")[0]
    slug = slug.strip()
    if not slug or slug.lower() in _NON_TENANT or slug.lower() == ats:
        return None
    return slug if _SLUG_OK.match(slug) else None


def harvest_ats_seeds(conn, geo_gate: bool = True) -> dict[str, list[str]]:
    """{ats: [tenant slugs]} mined from the corpus for the wired ATSes.

    geo_gate (default on): keep a tenant only if at least one of its corpus jobs
    is not an outright non-German location — so a US-only Greenhouse company
    surfaced by an aggregator does not pull its whole global board into a
    Germany-focused pipeline. Slugs the config already lists are NOT filtered
    here; merged_companies dedups at wiring time.
    """
    from utils.geo_de import has_non_de_marker  # lazy: keep import graph light

    placeholders = ",".join("?" for _ in WIRED_ATS)
    # ats -> {slug_lower: [slug, any_job_could_be_german]}
    seen: dict[str, dict[str, list]] = {a: {} for a in WIRED_ATS}
    for ats, location, apply_url, url in conn.execute(
        f"SELECT ats, location, apply_url, url FROM jobs WHERE ats IN ({placeholders})",
        WIRED_ATS,
    ):
        slug = extract_ats_slug(ats, apply_url, url)
        if not slug:
            continue
        could_de = not has_non_de_marker(location)
        bucket = seen[ats]
        key = slug.lower()
        if key not in bucket:
            bucket[key] = [slug, could_de]
        elif could_de:
            bucket[key][1] = True

    out: dict[str, list[str]] = {}
    for ats, bucket in seen.items():
        slugs = sorted(slug for slug, could_de in bucket.values()
                       if could_de or not geo_gate)
        if slugs:
            out[ats] = slugs
    return out


def persist_seeds(conn, path: str = SEEDS_PATH) -> dict[str, list[str]]:
    """Harvest and write data/ats_seeds.json; returns the harvested map."""
    seeds = harvest_ats_seeds(conn)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seeds, indent=2, ensure_ascii=False), encoding="utf-8")
    return seeds


def load_seeds(path: str = SEEDS_PATH) -> dict[str, list[str]]:
    """The last harvested seed map, or {} if none/unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def merged_companies(config_list, ats: str, seeds: dict | None = None) -> list:
    """config tenant slugs first (order preserved), then harvested ones not
    already present (case-insensitive dedup)."""
    seeds = seeds if seeds is not None else load_seeds()
    config_list = list(config_list or [])
    have = {str(c).lower() for c in config_list}
    extra = [s for s in seeds.get(ats, []) if s.lower() not in have]
    return config_list + extra
