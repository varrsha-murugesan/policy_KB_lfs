"""
ingest.py — Load and normalize billing rules from each payer's Excel workbook
into a single canonical DataFrame.

Each payer uses slightly different column names; this module normalises them
into a shared schema so every downstream module works with the same fields.

Canonical schema
----------------
payer               : str   UHC | Aetna | Humana
rule_id             : str   original rule identifier
policy_title        : str   policy name
policy_number       : str
line_of_business    : str
billing_class       : str
rule_category       : str   normalised category (see CATEGORY_MAP)
rule_statement      : str   verbatim text of the rule
cpt_codes           : list  list of CPT/HCPCS code strings
modifiers_required  : list
modifiers_prohibited: list
trigger_conditions  : str
exceptions          : str
audit_action        : str   e.g. DENY, FLAG, SUSPEND, ADJUST
source_pdf          : str
"""

from pathlib import Path
import pandas as pd
import ast

# ---------------------------------------------------------------------------
# Paths to source Excel files (relative to this script)
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent.parent

SOURCES = {
    "UHC":    BASE / "Uhc/billing_rules.xlsx",
    "Aetna":  BASE / "Aetna/billing_rules.xlsx",
    "Humana": BASE / "Humana/payment policy/billing_rules.xlsx",
}

# ---------------------------------------------------------------------------
# Category normalisation map — map every payer-specific category label to a
# shared canonical category so comparisons are apples-to-apples.
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    # Denial / prohibition
    "DENIAL_PROHIBITION":           "DENIAL",
    "DENIAL":                       "DENIAL",

    # Reimbursement criteria
    "REIMBURSEMENT_CRITERIA":       "REIMBURSEMENT_CRITERIA",
    "CONDITIONAL_REIMBURSEMENT":    "REIMBURSEMENT_CRITERIA",

    # Modifier rules
    "MODIFIER_RESTRICTION":         "MODIFIER_RULE",
    "MODIFIER_REQUIRED":            "MODIFIER_RULE",
    "MODIFIER_PROHIBITED":          "MODIFIER_RULE",

    # Bundling / inclusion
    "BUNDLING_INCLUSION":           "BUNDLING",

    # Frequency / time-span limits
    "FREQUENCY_LIMIT":              "FREQUENCY_LIMIT",
    "TIME_SPAN_LIMIT":              "FREQUENCY_LIMIT",
    "ONCE_IN_A_LIFETIME":           "FREQUENCY_LIMIT",

    # Place of service
    "PLACE_OF_SERVICE":             "PLACE_OF_SERVICE",
    "PLACE_OF_SERVICE_RESTRICTION": "PLACE_OF_SERVICE",

    # Age / gender scope
    "AGE_GENDER_SCOPE":             "AGE_RESTRICTION",
    "AGE_RESTRICTION":              "AGE_RESTRICTION",

    # Exceptions / overrides
    "EXCEPTION_OVERRIDE":           "EXCEPTION",

    # Documentation / policy requirements
    "POLICY_REQUIREMENT":           "POLICY_REQUIREMENT",
    "DOCUMENTATION_REQUIREMENT":    "POLICY_REQUIREMENT",

    # Bill type
    "BILL_TYPE_RESTRICTION":        "BILL_TYPE",

    # Prior auth
    "PRIOR_AUTHORIZATION":          "PRIOR_AUTHORIZATION",
}


def _parse_code_list(raw) -> list:
    """Return a deduplicated sorted list of code strings from a cell value."""
    if pd.isna(raw) or str(raw).strip() in ("", "nan", "None"):
        return []
    raw = str(raw).strip()
    # Try Python-literal parsing first (handles "[...]" strings)
    if raw.startswith("["):
        try:
            codes = ast.literal_eval(raw)
            return sorted(set(str(c).strip() for c in codes if str(c).strip()))
        except Exception:
            pass
    # Fall back: comma-separated
    return sorted(set(c.strip() for c in raw.split(",") if c.strip()))


def _parse_modifier_list(raw) -> list:
    return _parse_code_list(raw)


def _normalise_category(cat: str) -> str:
    if pd.isna(cat) or not cat:
        return "UNKNOWN"
    return CATEGORY_MAP.get(str(cat).strip().upper(), str(cat).strip().upper())


# ---------------------------------------------------------------------------
# Per-payer loaders
# ---------------------------------------------------------------------------

def _load_uhc() -> pd.DataFrame:
    df = pd.read_excel(SOURCES["UHC"], sheet_name="Rules", dtype=str)
    df = df.fillna("")
    return pd.DataFrame({
        "payer":                "UHC",
        "rule_id":              df["Rule_ID"],
        "policy_title":         df["Policy_Title"],
        "policy_number":        df["Policy_Number"],
        "line_of_business":     df["Line_of_Business"],
        "billing_class":        df["Billing_Class"],
        "rule_category":        df["Rule_Category"].map(_normalise_category),
        "rule_statement":       df["Policy_Rule_Text"],
        "cpt_codes":            df["Target_Codes"].map(_parse_code_list),
        "modifiers_required":   df["Target_Modifiers"].map(_parse_modifier_list),
        "modifiers_prohibited": [[] for _ in range(len(df))],
        "trigger_conditions":   df["Trigger_Keywords"],
        "exceptions":           df.get("Attachment_Values", pd.Series([""] * len(df))).fillna(""),
        "audit_action":         df.get("Incorrect_Billing_Condition", pd.Series([""] * len(df))).fillna(""),
        "source_pdf":           df["Policy_Number"],
    })


def _load_aetna() -> pd.DataFrame:
    df = pd.read_excel(SOURCES["Aetna"], sheet_name="Rules", dtype=str)
    df = df.fillna("")
    return pd.DataFrame({
        "payer":                "Aetna",
        "rule_id":              df["rule_id"],
        "policy_title":         df["policy_title"],
        "policy_number":        df["policy_number"],
        "line_of_business":     df["line_of_business"],
        "billing_class":        df["billing_class"],
        "rule_category":        df["rule_category"].map(_normalise_category),
        "rule_statement":       df["rule_statement"],
        "cpt_codes":            df["primary_cpt_codes"].map(_parse_code_list),
        "modifiers_required":   df["modifiers_required"].map(_parse_modifier_list),
        "modifiers_prohibited": df["modifiers_prohibited"].map(_parse_modifier_list),
        "trigger_conditions":   df["trigger_conditions"],
        "exceptions":           df["exceptions_overrides"],
        "audit_action":         df["audit_action"],
        "source_pdf":           df["source_pdf"],
    })


def _load_humana() -> pd.DataFrame:
    df = pd.read_excel(SOURCES["Humana"], sheet_name="Rules", dtype=str)
    df = df.fillna("")
    return pd.DataFrame({
        "payer":                "Humana",
        "rule_id":              df["rule_id"],
        "policy_title":         df["policy_title"],
        "policy_number":        df["policy_number"],
        "line_of_business":     df["line_of_business"],
        "billing_class":        df["billing_class"],
        "rule_category":        df["rule_category"].map(_normalise_category),
        "rule_statement":       df["rule_statement"],
        "cpt_codes":            df["primary_cpt_codes"].map(_parse_code_list),
        "modifiers_required":   df["modifiers_required"].map(_parse_modifier_list),
        "modifiers_prohibited": df["modifiers_prohibited"].map(_parse_modifier_list),
        "trigger_conditions":   df["trigger_conditions"],
        "exceptions":           df["exceptions_overrides"],
        "audit_action":         df["audit_action"],
        "source_pdf":           df["source_pdf"],
    })


def load_all() -> pd.DataFrame:
    """Return one canonical DataFrame with all three payers' rules."""
    frames = [_load_uhc(), _load_aetna(), _load_humana()]
    df = pd.concat(frames, ignore_index=True)
    return df


if __name__ == "__main__":
    df = load_all()
    print(f"Total rules loaded: {len(df)}")
    print(df.groupby("payer")["rule_id"].count())
    print("\nCanonical categories:")
    print(df.groupby(["payer", "rule_category"])["rule_id"].count().unstack(fill_value=0))

