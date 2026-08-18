"""staffing.py — is this "employer" an agency fronting other people's roles?

One question, two consumers, so it lives in one stdlib-only module:

  * the queue's dedup gate asks it to EXEMPT such a company from the
    one-live-application-per-company block — a second posting at Hays is a
    different end client, not a re-application;
  * the queue's ranking asks it to DEMOTE those postings behind direct
    employers.

Demotion is a preference, not an evidence-backed filter. Measured over every
application sent so far: 49 of 601 went to an agency and none reached an
interview, against 1.63% for direct employers — but under the direct rate,
seeing 0 in 49 has probability 0.45, and the 95% upper bound on the agency rate
(6.1%) contains it. The data cannot tell them apart, so these are ranked back,
never dropped: when the queue runs dry the shots are still there.

Name matching is the only option for most of the corpus (the queue has no JD
text loaded), so the phrases are high-precision: the bare word "consult" is
never used alone, or every product company that "consults stakeholders" would
match. Bundesagentur is the one source that states it outright — its
istArbeitnehmerUeberlassung / istPrivateArbeitsvermittlung flags are persisted
at ingest into jobs.staffing and OR'd in here. Measured on 45 BA postings the
two halves disagree in both directions (the name test missed plusYou / hyrUP /
AERO HighProfessionals; the flags missed Franklin Fitch, a UK recruiter with no
German registration), so neither alone is enough.
"""

import re

# Recruitment agencies / consultancies post many distinct end-client roles under
# one legal name. Shares intent with salary_estimator._CONSULTANCY_MARKERS.
_STAFFING_RE = re.compile(
    r"recruit|personaldienst|personalvermittl|personalberatung|zeitarbeit|"
    r"arbeitnehmer(ü|ue)berlassung|staffing|"
    r"consulting|consultanc|unternehmensberatung|it-beratung|"
    r"professional (solutions|services)|"
    # Brands whose names carry no structural giveaway. Only unambiguous ones —
    # each is a staffing firm and nothing else — so no product company can
    # collide with them. "Hays" alone matched none of the terms above.
    r"\bhays\b|\badecco\b|\brandstad\b|\bmanpower\b|\bferchau\b|"
    r"\bakkodis\b|\bbrunel\b|\bexpertum\b|\bgulp\b|\borizon\b|"
    r"\bexperis\b|\bhofmann personal\b|\bmichael page\b|\brobert half\b",
    re.I,
)


def is_staffing_employer(company: str | None) -> bool:
    """True when the company NAME reads as a recruitment/consultancy/staffing
    firm — one legal entity fronting many different end clients."""
    return bool(_STAFFING_RE.search(company or ""))


def is_staffing(company: str | None, flagged: object = None) -> bool:
    """is_staffing_employer, plus a source-supplied flag (bundesagentur's
    Arbeitnehmerüberlassung / private-Vermittlung markers, persisted on the
    row). Either one is enough."""
    if flagged in (1, True, "1"):
        return True
    return is_staffing_employer(company)
