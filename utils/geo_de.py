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
]

# Veto list for is_germany_location(). Country names seen in the location
# column plus city-level conflicts: places whose location string can also
# contain a GERMANY_PATTERNS hit or a 5-digit zip ("Halle, Belgium",
# "94104 San Francisco") and Austrian/Swiss cities that would otherwise
# rely on the country name being present.
_NON_DE_RE = re.compile(
    r"\b(united states|u\.s|usa|united kingdom|uk|england|scotland|wales|"
    r"ireland|spain|españa|france|italy|italia|portugal|netherlands|belgium|"
    r"belgië|belgique|austria|österreich|oesterreich|switzerland|schweiz|"
    r"suisse|poland|polska|czech|slovakia|hungary|romania|bulgaria|greece|"
    r"denmark|sweden|norway|finland|luxembourg|turkey|ukraine|russia|"
    r"canada|brazil|mexico|argentina|india|china|japan|korea|singapore|"
    r"australia|new zealand|israel|egypt|ägypten|south africa|"
    r"vienna|wien|zurich|zürich|geneva|genève|basel|graz|innsbruck|"
    r"salzburg|linz|san francisco|new york|london|"
    # "Lisbonne" contains the pattern "bonn" — veto Lisbon spellings explicitly
    r"lisbon|lisbonne|lissabon)\b",
    re.I,
)

_DE_POSTAL_RE = re.compile(r"\b\d{5}\b")
# ", DE" as a comma-delimited token — e.g. "Walldorf, DE, 69190"
_DE_TOKEN_RE = re.compile(r",\s*de\s*(?=,|$)", re.I)


def is_germany_location(location: str | None) -> bool:
    """Precise check: does this location string place the job in Germany?

    Any non-DE marker vetoes the whole string, so mixed strings stay out and
    a human decides. Meant for write-back relabeling, not for search recall.
    """
    loc = (location or "").strip()
    if not loc:
        return False
    low = loc.lower()
    if _NON_DE_RE.search(low):
        return False
    if _DE_TOKEN_RE.search(low) or _DE_POSTAL_RE.search(low):
        return True
    return any(p in low for p in GERMANY_PATTERNS if p != DE_POSTAL_SENTINEL)
