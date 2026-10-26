"""
claim_requirements.py

Works out which claim fields an adjudicator would have to read in order to
decide whether a given rule is satisfied.

This is not a keyword lookup over the rule text. It runs in three passes:

  1. FACETS  - read the assembled rule (text, conditions, exceptions, extracted
     codes and modifiers, the linked-PDF attribute, billing class, section
     heading) and decide what the rule actually asserts: does it limit
     frequency, compare two codes, scope to a member, scope to a provider,
     depend on a setting, depend on an episode date, and so on.
  2. COMPOSE - map those facets to claim columns, including the columns that
     are implied rather than mentioned. A "once per 30 days for the same
     member" limit cannot be adjudicated without the line's code, modifier,
     service date and member - even though the sentence names only the period
     and the member.
  3. INHERIT - an exception or a code-list-derived rule is only meaningful
     against the rule it modifies, so it takes its parent's requirements too.

Column vocabulary is fixed (CANONICAL_ORDER) so the output is machine-usable.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Set

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

CPT_CODE = "cpt"
HCPCS_CODE = "hcpcs"
MODIFIER = "modifier" #Also check for proceduremodifier
UNITS = "unit"
DATE_OF_SERVICE = "dateofservice"
DATE_OF_JOINING = "dateofjoining"
DATE_OF_DISCHARGE = "dateofdischarge"
MEMBER_ID = "id"
PHYSICIAN_ID = "physicianid"
PROVIDER_ID = "providerid"
PROVIDER_SPECIALTY = "ProviderSpecialty" #we represent the specialty using taxonomy which is based on the payer'mappings
PLACE_OF_SERVICE = "serviceplace"
FACILITY_ID = "Facilitid" # Need to check whether it is explicity mentioned or they are identified using billtypecodes
REVENUE_CODE = "revenue"
BILL_TYPE = "billtype"
CLAIM_TYPE = "claimtype" #need to check the exact meaning
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
# Facet detectors
# ---------------------------------------------------------------------------

RX = {
    # the rule is about identifying a procedure at all
    "procedure": re.compile(
        r"\b(cpt|hcpcs|procedure code|code(?:s)?|billed|reported|service(?:s)?)\b", re.I),

    # explicit modifier logic
    "modifier": re.compile(
        r"\bmodifier|\bappend(?:ed|ing)?\b|\bmodifiers? will (?:not )?override\b", re.I),

    # a count / frequency ceiling over a period
    "frequency": re.compile(
        r"\bonce\b|\bone (?:time|unit|reimbursement)\b|\bonly one\b|\bsingle\b|"
        r"\bper (?:day|date of service|week|month|calendar month|year|quarter|lifetime|episode)\b|"
        r"\bmore than once\b|\bmaximum (?:of|frequency|units?|number)\b|\bmaximum frequency per day\b|"
        r"\blimited to\b|\bno more than\b|\bfrequency\b|\btime span\b|\btime period\b|"
        r"\bcalendar month\b|\bwithin \d+ (?:calendar )?days?\b|\b\d+ day period\b|"
        r"\bduring (?:that|the same) (?:time )?period\b|\bonce in a lifetime\b", re.I),

    # two or more codes weighed against each other on the same claim/period
    "comparison": re.compile(
        r"\binclusive\b|\bincidental\b|\bintegral\b|\bcomponent\b|\bcomprehensive\b|\bbundl|"
        r"\bseparately reimbursab|\bnot separately\b|\bmutually exclusive\b|\bduplicate\b|"
        r"\breported (?:together )?with\b|\bin addition to\b|\bwhen (?:billed|reported) with\b|"
        r"\bcolumn one\b|\bcolumn two\b|\bncci\b|\badd-on\b|\bglobal (?:package|surgical)", re.I),

    # units / quantity on the line
    "units": re.compile(
        r"\bunits? of service\b|\bnumber of units\b|\bone unit\b|\bunits? (?:billed|reported|allowed|"
        r"submitted)\b|\bmaximum frequency per day\b|\bmfd\b|\bquantity\b|\bper unit\b|"
        r"\bmaximum of (?:one|two|three|\d+) units?\b", re.I),

    # the patient dimension
    "member": re.compile(
        r"\bsame (?:member|patient|beneficiary|individual patient)\b|"
        r"\bper (?:member|patient|beneficiary)\b|\bmember'?s?\b|\bpatient'?s?\b|"
        r"\benrollee\b|\blifetime\b|\bper case\b", re.I),

    # the billing entity dimension (group / TIN / practice)
    "provider": re.compile(
        r"\bsame group\b|\bgroup practice\b|\bsame provider\b|\bfederal tax identification\b|"
        r"\bsame tax id\b|\bbilling provider\b|\brendering provider\b|\bsame (?:physician|individual)\b|"
        r"\bother qualified health care professional\b|\bsame qualified health care professional\b|"
        r"\bby the same\b.{0,40}\b(?:physician|professional|provider|group)\b", re.I),

    # the individual clinician as distinct from the group
    "physician": re.compile(
        r"\bsame individual\b|\bone individual\b|\bsame physician\b|\bindividual physician\b|"
        r"\bsurgeon\b|\bdifferent physicians\b|\bsame practitioner\b|\battending\b|"
        r"\bperforming (?:physician|provider)\b|\bphysician who\b", re.I),

    "specialty": re.compile(
        r"\bsame specialt|\bdifferent specialt|\bspecialty designation\b|\bphysician specialty\b|"
        r"\bsame taxonomy\b|\bof the same specialty\b", re.I),

    "diagnosis": re.compile(
        r"\bdiagnos\w+\b|\bicd-?10\b|\bicd-?9\b|\bicd code\b|\bcondition\(s\) listed\b|"
        r"\bmedical necessity\b|\bmedically necessary\b|\bcovered condition\b|"
        r"\bfor the treatment of\b", re.I),

    "setting": re.compile(
        r"\bplace of service\b|\bpos\b|\boffice setting\b|\bfacility setting\b|\bnon-?facility\b|"
        r"\boutpatient\b|\binpatient\b|\bhome health\b|\btelehealth\b|\boriginating site\b|"
        r"\bambulatory surgical\b|\basc\b|\bemergency (?:room|department)\b|\bsetting\b", re.I),

    "revenue_code": re.compile(r"\brevenue code\b|\brev(?:enue)? cd\b|\brev code\b|\b0[1-9]\d{2}\b", re.I),
    "bill_type": re.compile(r"\btype of bill\b|\bbill type\b|\btob\b|\bub-?04\b|\bub04\b|\b011[0-9]|012[0-9]|013[0-9]|083[0-9]\b", re.I),
    "facility": re.compile(
        r"\bfacility\b|\bhospital\b|\bskilled nursing\b|\bsnf\b|\basc\b|\binstitutional\b|\bemergency department\b|\bed\b|\boutpatient hospital\b|\binpatient\b", re.I),
    "claim_type": re.compile(
        r"\bcms-?1500\b|\bub-?04\b|\bub04\b|\bprofessional (?:claim|and facility)\b|\bfacility claim\b|"
        r"\bclaim type\b|\bprofessional or facility\b|\binstitutional claim\b", re.I),

    "condition_code": re.compile(r"\bcondition code\b|\bcondition\s*(?:44|21|20|g0)\b", re.I),
    "occurrence_code": re.compile(r"\boccurrence code\b|\boccurrence span\b|\boccurrence date\b", re.I),
    "poa": re.compile(r"\bpresent on admission\b|\bpoa\b|\bhospital acquired condition\b|\bhac\b", re.I),

    "discharge": re.compile(r"\bdischarg\w+\b|\bpost-?discharge\b|\btransitional care management\b|\btcm\b", re.I),
    "admission": re.compile(r"\badmission\b|\badmitted\b|\badmit\b|\binpatient stay\b|\bdate of joining\b", re.I),

    # an episode with a defined window (global period, post-op, follow-up)
    "episode": re.compile(
        r"\bglobal (?:period|days?|surgical|package)\b|\bpost-?operative\b|\bpre-?operative\b|"
        r"\bfollow-?up (?:days?|period)\b|\b\d+-day (?:global|post)\b|\bwithin \d+ (?:calendar )?days?\b|"
        r"\bpostoperative period\b", re.I),

    # any explicit reference to when the service happened
    "service_date": re.compile(
        r"\bdate of service\b|\bdates of service\b|\bsame (?:day|date)\b|\bservice date\b|"
        r"\bdate span\b|\bfrom date\b|\bthrough date\b|\bon the same claim\b|\bper day\b", re.I),
}


def _blob(rule) -> str:
    """Everything the rule asserts, in one string: statement + its qualifiers."""
    parts: List[str] = [rule.text or ""]
    parts.extend(rule.conditions or [])
    parts.extend(rule.exceptions or [])
    parts.extend(rule.section_path or [])
    if getattr(rule, "attachment", ""):
        parts.append(rule.attachment.replace("-", " ").replace(".pdf", ""))
    return " | ".join(parts)


def detect_facets(rule) -> Set[str]:
    """What does this rule actually assert?"""
    blob = _blob(rule)
    facets = {name for name, rx in RX.items() if rx.search(blob)}

    # structured evidence beats prose: we already extracted these
    if getattr(rule, "codes", None):
        facets.add("procedure")
    if getattr(rule, "modifiers", None):
        facets.add("modifier")

    # a payment decision - allow, deny, restrict - is always applied to a coded
    # line, so the code is required even when the sentence names the service in
    # clinical rather than coding terms ("will not reimburse viral culture testing")
    if re.search(r"\breimburs\w+|\bpayable\b|\bpayment\b|\bdeny\b|\bdenied\b|\bdenial\b|"
                 r"\ballow\w*\b|\bcover\w*\b|\bconsider reimbursement\b", blob, re.I):
        facets.add("procedure")

    # a code list keyed on a period is a frequency rule even if the sentence is generic
    attach_attr = " ".join(rule.conditions or [])
    if re.search(r"\btime span\b|\bglobal days?\b|\bmaximum frequency per day\b|\bonce in a lifetime\b",
                 attach_attr, re.I):
        facets.add("frequency")

    # facility-flavoured rules on a facility policy
    if (getattr(rule, "billing_class", "") or "").lower() == "facility":
        facets.add("facility")

    return facets


# ---------------------------------------------------------------------------
# Facets -> claim columns
# ---------------------------------------------------------------------------

def compose_columns(rule, facets: Set[str]) -> Set[str]:
    cols: Set[str] = set()

    if "procedure" in facets or getattr(rule, "codes", None):
        cols.add(CPT_CODE)
        cols.add(HCPCS_CODE)

    # A frequency ceiling or a code-vs-code comparison is adjudicated by
    # grouping claim lines. That needs the whole grouping key, not just the
    # dimension the sentence happens to name.
    grouping = ("frequency" in facets) or ("comparison" in facets)
    if grouping:
        cols.update({CPT_CODE, HCPCS_CODE, MODIFIER, DATE_OF_SERVICE, MEMBER_ID})
        # limits are scoped to a billing entity unless the text says otherwise
        cols.add(PROVIDER_ID)

    if "modifier" in facets:
        cols.add(MODIFIER)

    if "units" in facets:
        cols.update({UNITS, CPT_CODE, HCPCS_CODE, DATE_OF_SERVICE})

    if "service_date" in facets or "episode" in facets:
        cols.add(DATE_OF_SERVICE)

    if "member" in facets:
        cols.add(MEMBER_ID)

    if "provider" in facets:
        cols.add(PROVIDER_ID)
    if "physician" in facets:
        cols.update({PHYSICIAN_ID, PROVIDER_ID})
    if "specialty" in facets:
        cols.update({PROVIDER_SPECIALTY, PROVIDER_ID})

    if "diagnosis" in facets:
        cols.add(ICD_CODE)
    if "setting" in facets:
        cols.add(PLACE_OF_SERVICE)

    if "revenue_code" in facets:
        cols.update({REVENUE_CODE, CLAIM_TYPE})
    if "bill_type" in facets:
        cols.update({BILL_TYPE, CLAIM_TYPE})
    if "facility" in facets:
        cols.add(FACILITY_ID)
    if "claim_type" in facets:
        cols.add(CLAIM_TYPE)
    if "condition_code" in facets:
        cols.update({CONDITION_CODE, CLAIM_TYPE})
    if "occurrence_code" in facets:
        cols.update({OCCURRENCE_CODE, DATE_OF_SERVICE, CLAIM_TYPE})
    if "poa" in facets:
        cols.update({POA_INDICATOR, ICD_CODE, CLAIM_TYPE})

    # episode windows are measured from an admission / discharge event
    if "discharge" in facets:
        cols.update({DATE_OF_DISCHARGE, DATE_OF_SERVICE, MEMBER_ID})
    if "admission" in facets:
        cols.update({DATE_OF_JOINING, DATE_OF_SERVICE, MEMBER_ID})
    if "episode" in facets:
        cols.update({CPT_CODE, HCPCS_CODE, MEMBER_ID})

    return cols


def order_columns(cols: Set[str]) -> List[str]:
    return [c for c in CANONICAL_ORDER if c in cols]


def assign_claim_columns(rules: Sequence) -> None:
    """Populate rule.claim_columns for every rule, then inherit down the tree."""
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
        # an exception or a derived code-list rule only means something against
        # its parent, so validating it needs the parent's fields as well
        if parent and parent in by_id and parent not in seen and \
                getattr(rule, "rule_type", "") in ("exception", "derived"):
            cols |= resolve(parent, seen | {rule_id})
        resolved[rule_id] = cols
        return cols

    for rule in rules:
        rule.claim_columns = order_columns(resolve(rule.rule_id, {rule.rule_id}))
