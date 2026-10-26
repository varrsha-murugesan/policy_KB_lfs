"""
claim_requirements.py
=====================
Works out which claim fields an adjudicator would have to inspect in order to
determine whether a given Aetna billing/payment rule is satisfied.

Runs three passes:
  1. FACETS  - reads the assembled rule (text, conditions, exceptions, extracted
     codes, modifiers, billing class, section heading) to decide what the rule
     asserts: frequency, code comparison, member/age scope, provider/specialty,
     place of service, diagnosis requirement, units, etc.
  2. COMPOSE - maps those facets to canonical claim columns.
  3. INHERIT - an exception or child rule inherits its parent's requirements.

Vocabulary is canonical to match UHC reference schema.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Set

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

CPT_CODE = "cpt"
HCPCS_CODE = "hcpcs"
MODIFIER = "modifier"
UNITS = "unit"
DATE_OF_SERVICE = "dateofservice"
DATE_OF_JOINING = "dateofjoining"
DATE_OF_DISCHARGE = "dateofdischarge"
MEMBER_ID = "id"
PHYSICIAN_ID = "physicianid"
PROVIDER_ID = "providerid"
PROVIDER_SPECIALTY = "ProviderSpecialty"
PLACE_OF_SERVICE = "serviceplace"
FACILITY_ID = "Facilitid"
REVENUE_CODE = "revenue"
BILL_TYPE = "billtype"
CLAIM_TYPE = "claimtype"
CONDITION_CODE = "condition_code"
OCCURRENCE_CODE = "occurrence_code"
POA_INDICATOR = "poa_indicator"
ICD_CODE = "icd_code"

CANONICAL_ORDER = [
    CPT_CODE, HCPCS_CODE, MODIFIER, UNITS,
    DATE_OF_SERVICE, DATE_OF_JOINING, DATE_OF_DISCHARGE,
    MEMBER_ID, PHYSICIAN_ID, PROVIDER_ID, PROVIDER_SPECIALTY,
    PLACE_OF_SERVICE, FACILITY_ID, REVENUE_CODE, BILL_TYPE, CLAIM_TYPE,
    CONDITION_CODE, OCCURRENCE_CODE, POA_INDICATOR,
    ICD_CODE,
]

# ---------------------------------------------------------------------------
# Facet Detectors
# ---------------------------------------------------------------------------

RX = {
    # Procedure identification
    "procedure": re.compile(
        r"\b(cpt|hcpcs|procedure code|code(?:s)?|billed|reported|service(?:s)?|surgery|surgical)\b", re.I),

    # Modifiers
    "modifier": re.compile(
        r"\bmodifier|\bappend(?:ed|ing)?\b|\bmodifiers? will (?:not )?override\b|"
        r"\b25|59|24|26|tc|jw|qw|rt|lt|50|52|73|74|80|as\b", re.I),

    # Frequency ceilings / intervals
    "frequency": re.compile(
        r"\bonce\b|\bone (?:time|unit|reimbursement)\b|\bonly one\b|\bsingle\b|"
        r"\bper (?:day|date of service|week|month|calendar month|year|quarter|lifetime|episode)\b|"
        r"\bno more than\b|\bmore than once\b|\bmaximum (?:of|frequency|units?|number)\b|"
        r"\blimited to\b|\bfrequency\b|\btime span\b|\btimeframe\b|\b\d+-day (?:timeframe|period)\b|"
        r"\beight-week period\b|\bsix-month period\b|\bthree-month timeframe\b|\b1,095-day\b|"
        r"\btwo-month period\b|\bseven-day period\b|\bwithin (?:last )?(?:three years|\d+ days)\b", re.I),

    # Code-vs-code comparisons / bundling
    "comparison": re.compile(
        r"\binclusive\b|\bincidental\b|\bintegral\b|\bcomponent\b|\bcomprehensive\b|\bbundl|"
        r"\bseparately reimbursab|\bnot separately\b|\bmutually exclusive\b|\bduplicate\b|"
        r"\breported (?:together )?with\b|\bin addition to\b|\bwhen (?:billed|reported) with\b|"
        r"\bwith stereotactic\b|\balong with\b|\bnot reported with\b|\bnon-covered\b", re.I),

    # Units / dosage / quantity
    "units": re.compile(
        r"\bunits? of service\b|\bnumber of units\b|\bone unit\b|\bunits? (?:billed|reported|allowed|"
        r"submitted)\b|\bquantity\b|\bper unit\b|\bmaximum unit\b|\b120 units\b|\b501 ml\b|"
        r"\b500 ml\b|\bdosage administered\b|\bunit limits\b", re.I),

    # Member / Patient
    "member": re.compile(
        r"\bsame (?:member|patient|beneficiary|individual patient)\b|"
        r"\bper (?:member|patient|beneficiary)\b|\bmember'?s?\b|\bpatient'?s?\b|"
        r"\benrollee\b|\blifetime\b|\bage\b|\byears of age\b|\bpediatric\b|\bchildren\b|"
        r"\badults\b|\bpregnant\b", re.I),

    # Provider / TIN / Specialty
    "provider": re.compile(
        r"\bsame group\b|\bgroup practice\b|\bsame provider\b|\bfederal tax identification\b|"
        r"\bsame tax id\b|\bbilling provider\b|\brendering provider\b|\bany provider\b|"
        r"\bby any provider\b|\bby the same provider\b|\bconsulting provider\b|"
        r"\bsecond physician\b|\bassistant surgeon\b|\bnpp'?s?\b", re.I),

    "specialty": re.compile(
        r"\bsame specialt|\bdifferent specialt|\bspecialty designation\b|\bphysician specialty\b|"
        r"\bprovider specialty\b", re.I),

    # Diagnosis / ICD-10
    "diagnosis": re.compile(
        r"\bdiagnos\w+\b|\bicd-?10\b|\bicd-?9\b|\bicd code\b|\bcondition\(s\) listed\b|"
        r"\bmedical necessity\b|\bmedically necessary\b|\bcovered diagnosis\b|"
        r"\bnicotine dependence\b|\bvitamin d deficiency\b|\bseizures\b|\bfoot ulcers\b|"
        r"\bhighest level of specificity\b|\bsupporting diagnosis\b", re.I),

    # Place of Service / Setting
    "setting": re.compile(
        r"\bplace of service\b|\bpos\b|\boffice\b|\bpos 11\b|\bpos 21\b|\bpos 24\b|\bpos 12\b|"
        r"\bpos 53\b|\bfacility setting\b|\bnon-?facility\b|\boutpatient\b|\binpatient\b|"
        r"\bhome\b|\basc setting\b|\bcommunity mental health center\b", re.I),

    # Facility / Hospital fields
    "bill_type": re.compile(r"\btype of bill\b|\bbill type\b|\btob\b|\btob code 012x\b|\bub-?04\b", re.I),
    "revenue_code": re.compile(r"\brevenue code\b|\brev code\b", re.I),
    "facility": re.compile(r"\bfacility\b|\bhospital\b|\basc\b|\binpatient hospital\b", re.I),
    "claim_type": re.compile(r"\bcms-?1500\b|\bub-?04\b|\bprofessional\b|\bfacility\b", re.I),

    # Date of service / Episode
    "service_date": re.compile(
        r"\bdate of service\b|\bdates of service\b|\bsame (?:day|date)\b|\bservice date\b|"
        r"\bper date of service\b|\bper day\b|\bon the same date\b", re.I),
    "episode": re.compile(
        r"\bglobal (?:surgery|period|days?)\b|\bpost-?operative\b|\bpre-?operative\b|"
        r"\b30-day surgical\b|\bwithin 30 days\b|\bday before or within 30 days\b", re.I),
}


def _blob(rule) -> str:
    """Concatenates all text qualifiers for comprehensive facet detection."""
    parts: List[str] = [getattr(rule, "text", "") or ""]
    parts.extend(getattr(rule, "conditions", []) or [])
    parts.extend(getattr(rule, "exceptions", []) or [])
    parts.extend(getattr(rule, "section_path", []) or [])
    return " | ".join(parts)


def detect_facets(rule) -> Set[str]:
    """Detect all domain facets active on a rule."""
    blob = _blob(rule)
    facets = {name for name, rx in RX.items() if rx.search(blob)}

    if getattr(rule, "codes", None):
        facets.add("procedure")
    if getattr(rule, "modifiers", None):
        facets.add("modifier")

    if re.search(r"\breimburs\w+|\bpayable\b|\bpayment\b|\bdeny\b|\bdenied\b|\ballow\w*\b|\bcover\w*\b", blob, re.I):
        facets.add("procedure")

    if (getattr(rule, "billing_class", "") or "").lower() == "facility":
        facets.add("facility")

    return facets


def compose_columns(rule, facets: Set[str]) -> Set[str]:
    """Map detected facets to claim columns."""
    cols: Set[str] = set()

    # Procedure codes
    if "procedure" in facets or getattr(rule, "codes", None):
        codes = getattr(rule, "codes", []) or []
        has_hcpcs = any(c and c[0].isalpha() for c in codes) or "hcpcs" in _blob(rule).lower()
        cols.add(CPT_CODE)
        if has_hcpcs or not codes:
            cols.add(HCPCS_CODE)

    # Grouping logic
    grouping = ("frequency" in facets) or ("comparison" in facets)
    if grouping:
        cols.update({CPT_CODE, DATE_OF_SERVICE, MEMBER_ID, PROVIDER_ID})
        if getattr(rule, "modifiers", None) or "modifier" in facets:
            cols.add(MODIFIER)

    if "modifier" in facets or getattr(rule, "modifiers", None):
        cols.add(MODIFIER)

    if "units" in facets:
        cols.update({UNITS, CPT_CODE, DATE_OF_SERVICE})

    if "service_date" in facets or "episode" in facets:
        cols.add(DATE_OF_SERVICE)

    if "member" in facets:
        cols.add(MEMBER_ID)

    if "provider" in facets:
        cols.add(PROVIDER_ID)

    if "specialty" in facets:
        cols.update({PROVIDER_SPECIALTY, PROVIDER_ID})

    if "diagnosis" in facets:
        cols.add(ICD_CODE)

    if "setting" in facets:
        cols.add(PLACE_OF_SERVICE)

    if "bill_type" in facets:
        cols.update({BILL_TYPE, CLAIM_TYPE})

    if "revenue_code" in facets:
        cols.update({REVENUE_CODE, CLAIM_TYPE})

    if "facility" in facets:
        cols.add(FACILITY_ID)

    if "episode" in facets:
        cols.update({CPT_CODE, MEMBER_ID, DATE_OF_SERVICE})

    return cols


def order_columns(cols: Set[str]) -> List[str]:
    return [c for c in CANONICAL_ORDER if c in cols]


def assign_claim_columns(rules: Sequence) -> None:
    """Populate rule.claim_columns for every rule, with parent inheritance."""
    own: Dict[str, Set[str]] = {}
    for rule in rules:
        own[rule.rule_id] = compose_columns(rule, detect_facets(rule))

    by_id = {r.rule_id: r for r in rules}
    resolved: Dict[str, Set[str]] = {}

    def resolve(rule_id: str, seen: Set[str]) -> Set[str]:
        if rule_id in resolved:
            return resolved[rule_id]
        cols = set(own.get(rule_id, set()))
        rule = by_id.get(rule_id)
        parent = getattr(rule, "parent_id", "") if rule else ""
        if parent and parent in by_id and parent not in seen and \
                getattr(rule, "rule_type", "") in ("exception", "denial", "derived"):
            cols |= resolve(parent, seen | {rule_id})
        resolved[rule_id] = cols
        return cols

    for rule in rules:
        rule.claim_columns = order_columns(resolve(rule.rule_id, {rule.rule_id}))
