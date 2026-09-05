"""Germany location matching — the single source of truth.

Two consumers with different precision needs share the data here:

* phase3_dashboard: GERMANY_PATTERNS / DE_POSTAL_SENTINEL for the location
  search box ("germany" alias expansion). Recall-oriented — a false positive
  is one extra row in a human-browsed table.
* remote_geo_triage: is_germany_location() for relabeling scored jobs so the
  short apply_queue.GERMANY_KEYWORDS / ats_scan.GERMANY_LIKE lists match them.
  Precision-oriented — a false positive becomes a queue entry and a generated
  draft, so every pattern hit is vetoed first by _NON_DE_RE (locations like
  "Halle, Belgium", "Munster, United States" or a US zip such as
  "94104 San Francisco" contain a pattern/postal hit AND a non-DE marker).

Pure stdlib on purpose: apply_queue and the pipeline stages must be able to
import this without pulling in streamlit/requests.
"""

import re
from urllib.parse import urlparse

# Sentinel in GERMANY_PATTERNS: the dashboard expands it to a 5-digit postal
# code GLOB instead of a LIKE.
DE_POSTAL_SENTINEL = "__de_postal__"

# City alias expansion: handles English/German name variants and common misspellings
GERMANY_PATTERNS = [
    DE_POSTAL_SENTINEL,  # matches "74076 Heilbronn", "07743 Jena", etc.
    # country-level markers
    "germany", "deutschland", "bundesweit",
    # "(DE)" suffix — e.g. "Hamburg (DE)"
    "(de)",
    # German federal states
    "nordrhein", "westfalen", "rheinland", "pfalz",
    "sachsen", "thüringen", "thueringen",
    "schleswig", "holstein", "mecklenburg",
    "niedersachsen", "hessen", "saarland",
    "bayern", "bavaria", "brandenbur",
    # major cities (English + German + Anglicised spellings)
    "hamburg", "berlin",
    "munich", "münchen", "muenchen",
    "cologne", "köln", "koeln",
    "frankfurt",
    "düsseldorf", "dusseldorf",
    "stuttgart",
    "nuremberg", "nürnberg", "nuernberg",
    "leipzig",
    "hannover", "hanover",
    "bremen",
    "dresden",
    "essen", "dortmund", "bochum",
    "karlsruhe", "mannheim", "heidelberg",
    "augsburg", "freiburg",
    "wiesbaden", "mainz", "bonn",
    "kiel", "rostock", "lübeck", "luebeck",
    "konstanz", "ulm", "regensburg",
    # additional cities present in DB
    "potsdam", "jena", "halle",
    "magdeburg", "erfurt", "schwerin",
    "oldenburg", "bremerhaven", "neumünster", "neumuenster",
    "paderborn", "bielefeld", "münster", "muenster",
    "osnabrück", "osnabrueck",
    "aachen", "göttingen", "goettingen",
    "wolfsburg", "braunschweig", "brunswick",
    "kassel", "darmstadt", "offenbach",
    "saarbrücken", "saarbruecken",
    "koblenz", "trier",
    # small towns present in DB (often HQ towns: Walldorf=SAP, Renningen=Bosch)
    "fulda", "stralsund", "walldorf", "heilbronn", "renningen",
    "verl", "tholey", "gräfelfing", "graefelfing", "aschheim",
    "taufkirchen", "glinde", "schenefeld", "prüm", "pruem",
    "ditzingen", "barsinghausen",
]

# Veto list for is_germany_location(). Country names seen in the location
# column plus city-level conflicts: places whose location string can also
# contain a GERMANY_PATTERNS hit or a 5-digit zip ("Halle, Belgium",
# "94104 San Francisco") and Austrian/Swiss cities that would otherwise
# rely on the country name being present.
#
# A mixed string ("Essen (Ruhr), Jakarta (Indonesien)") is vetoed as a whole
# by design — see is_germany_location. Growing this list therefore also moves
# a few mixed German+foreign strings out of the pool; the 2026-09-05 additions
# moved 6 such strings, all of them already expired or skipped.
_NON_DE_RE = re.compile(
    r"\b(united states|u\.s|usa|united kingdom|uk|england|scotland|wales|"
    r"ireland|spain|españa|france|italy|italia|portugal|netherlands|belgium|"
    r"belgië|belgique|austria|österreich|oesterreich|switzerland|schweiz|"
    r"suisse|poland|polska|czech|slovakia|hungary|romania|bulgaria|greece|"
    r"denmark|sweden|norway|finland|luxembourg|turkey|ukraine|russia|"
    r"canada|brazil|mexico|argentina|india|china|japan|korea|singapore|"
    r"australia|new zealand|israel|egypt|ägypten|south africa|"
    r"vienna|wien|zurich|zürich|geneva|genève|basel|graz|innsbruck|"
    r"salzburg|linz|san francisco|new[- ]york|london|"
    # major EU cities that appear without a country name — a Spanish/French/…
    # 5-digit zip ("28046 Madrid") would otherwise pass as a German postal hit
    r"madrid|barcelona|valencia|sevilla|seville|"
    r"paris|lyon|marseille|toulouse|"
    r"rome|roma|milan|milano|turin|torino|"
    r"amsterdam|rotterdam|utrecht|eindhoven|"
    r"brussels|bruxelles|antwerp|antwerpen|"
    r"warsaw|warszawa|krakow|kraków|wroclaw|wrocław|gdansk|gdańsk|"
    r"prague|praha|brno|bratislava|budapest|bucharest|sofia|athens|"
    r"dublin|cork|stockholm|gothenburg|göteborg|malmö|malmo|"
    r"copenhagen|københavn|oslo|helsinki|tallinn|riga|vilnius|"
    r"porto|zagreb|belgrade|kyiv|kiev|istanbul|"
    # "Lisbonne" contains the pattern "bonn" — veto Lisbon spellings explicitly
    r"lisbon|lisbonne|lissabon|"
    # ── Added 2026-09-05 from measured leakage ──────────────────────────────
    # 516 already-scored rows (49 A / 72 B) carried a location this list did
    # not know, so they were LLM-scored and then polluted the queue's supply.
    # Bare "US" was 399 of them; \b keeps it off Neuss/Husum (verified against
    # all 7,684 distinct locations in the DB).
    r"us|u\.s\.a|"
    # Gulf + Middle East
    r"united arab emirates|uae|saudi arabia|qatar|kuwait|bahrain|oman|"
    # Latin America
    r"chile|colombia|uruguay|ecuador|peru|venezuela|bolivia|paraguay|"
    # Africa
    r"nigeria|kenya|ghana|morocco|tunisia|"
    # Asia-Pacific
    r"philippines|malaysia|indonesia|thailand|vietnam|taiwan|hong kong|"
    r"pakistan|bangladesh|sri lanka|nepal|"
    # Europe the original list missed. "czechia" is separate on purpose:
    # \bczech\b above does not match it.
    r"estonia|latvia|lithuania|croatia|slovenia|serbia|bosnia|montenegro|"
    r"albania|moldova|belarus|czechia|cyprus|malta|iceland|"
    # German-language country names — bundesagentur writes these, sometimes
    # SHOUTED with an underscore ("Mladá Boleslav, TSCHECHISCHE_REPUBLIK")
    r"spanien|norwegen|luxemburg|italien|frankreich|niederlande|belgien|"
    r"schweden|d(?:ä|ae)nemark|finnland|polen|tschechien|tschechische_republik|"
    r"ungarn|rum(?:ä|ae)nien|bulgarien|griechenland|irland|kroatien|slowenien|"
    r"slowakei|estland|lettland|litauen|t(?:ü|ue)rkei|vereinigte_staaten|"
    r"gro(?:ß|ss)britannien|"
    # Regions that exclude Germany by definition (EMEA does NOT — it includes it)
    r"latam|apac|"
    # Bare city names with no country: "Los Angeles", "Remote / Ottawa".
    # Observed in the leak plus the global tech hubs of the same shape.
    r"los angeles|san mateo|palo alto|mountain view|santa clara|san jose|"
    r"san diego|seattle|portland|denver|dallas|houston|atlanta|chicago|miami|"
    r"philadelphia|nashville|austin|boston|ann arbor|honolulu|st\.? louis|"
    r"reston|concord|toronto|ottawa|vancouver|montreal|calgary|phoenix|"
    r"s(?:ã|a)o paulo|buenos aires|bogot(?:á|a)|santiago|lima|montevideo|quito|"
    r"bangalore|bengaluru|mumbai|hyderabad|chennai|pune|gurgaon|noida|"
    r"manila|jakarta|bangkok|taipei|seoul|tokyo|osaka|shanghai|beijing|"
    r"shenzhen|kuala lumpur|ho chi minh|hanoi|"
    r"dubai|abu dhabi|doha|riyadh|jeddah|tel aviv|cairo|lagos|nairobi|casablanca|"
    r"melbourne|sydney|brisbane|perth|auckland|wellington|"
    r"kaunas|tirana|nantes|bordeaux|lille|strasbourg|stra(?:ß|ss)burg|"
    r"orl(?:é|e)ans|londres|swindon|manchester|birmingham|edinburgh|glasgow|"
    r"leeds|bristol|cambridge|oxford)\b",
    re.I,
)

# "Reston, VA", "Nashville, TN", "Remote / Santa Clara, CA" — a US state code
# as a comma-delimited token. Deliberately excludes DE (Delaware): ", DE" is
# how several sources write Germany, and _DE_TOKEN_RE below relies on it.
_US_STATE_SUFFIX_RE = re.compile(
    r",\s*(?:a[klrz]|c[aot]|dc|fl|ga|hi|i[adln]|k[sy]|la|m[adeinost]|"
    r"n[cdehjmvy]|o[hkr]|pa|ri|s[cd]|tn|tx|ut|v[at]|w[aivy])\b",
    re.I,
)


def _text_is_non_de(location_low: str) -> bool:
    """Both textual veto rules — a country/city name, or a US state suffix."""
    return bool(_NON_DE_RE.search(location_low)
                or _US_STATE_SUFFIX_RE.search(location_low))

_DE_POSTAL_RE = re.compile(r"\b\d{5}\b")
# ", DE" as a comma-delimited token — e.g. "Walldorf, DE, 69190"
_DE_TOKEN_RE = re.compile(r",\s*de\s*(?=,|$)", re.I)


def _indeed_cc(url: str | None) -> str | None:
    """The country subdomain of an Indeed URL — 'es' for es.indeed.com,
    'de' for de.indeed.com — or None when the host is not a
    '<cc>.indeed.com' (bare indeed.com, www., smartapply., non-Indeed).

    Indeed is the only source in our scraper set that carries the posting's
    market in the hostname; "es.indeed.com/viewjob?jk=..." is a Spanish-market
    posting that no location string or JD text may reveal. The leftmost label
    is taken as the country code only when it is a bare two-letter alpha, so
    'www'/'smartapply'/'de-de' never parse as a country.
    """
    host = (urlparse(url or "").hostname or "").lower()
    labels = host.split(".")
    if labels[-2:] == ["indeed", "com"] and len(labels) >= 3:
        cc = labels[0]
        if len(cc) == 2 and cc.isalpha():
            return cc
    return None


def url_is_non_de(url: str | None) -> bool:
    """True when the source URL alone places the posting outside Germany.

    Currently just Indeed's country subdomain (es./fr./us./…). 'de' is the
    only code that is NOT non-DE; an unrecognised host returns False so it
    still flows through the normal location/JD checks.
    """
    cc = _indeed_cc(url)
    return cc is not None and cc != "de"


def has_non_de_marker(location: str | None, url: str | None = None) -> bool:
    """True when the location — or the source URL's country subdomain —
    outright places the job outside Germany.

    Used by phase2_scorer as a scoring veto: a job whose location says
    "Municipality of Madrid, Spain", or whose URL is es.indeed.com, can never
    enter the Germany-only apply queue, so LLM-scoring it is pure spend.
    Bare/ambiguous locations ("Remote", "Schlieren") with no URL signal
    return False — absence of a marker is not evidence of Germany, so they
    still get scored.
    """
    return _text_is_non_de((location or "").lower()) or url_is_non_de(url)


def is_germany_location(location: str | None, url: str | None = None) -> bool:
    """Precise check: does this location string place the job in Germany?

    Any non-DE marker — in the location string or the source URL's country
    subdomain — vetoes the whole string, so mixed strings stay out and a
    human decides. Meant for write-back relabeling, not for search recall.
    """
    loc = (location or "").strip()
    if not loc:
        return False
    if url_is_non_de(url):
        return False
    low = loc.lower()
    if _text_is_non_de(low):
        return False
    if _DE_TOKEN_RE.search(low) or _DE_POSTAL_RE.search(low):
        return True
    return any(p in low for p in GERMANY_PATTERNS if p != DE_POSTAL_SENTINEL)
