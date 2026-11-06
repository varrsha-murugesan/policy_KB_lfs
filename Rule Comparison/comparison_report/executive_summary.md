# Payer Billing Rule Comparison — Executive Summary

**Generated:** 2026-11-06 17:24

---

## 1. Rule Inventory

| Payer | Rules |
|-------|-------|
| UHC | 1116 |
| Aetna | 212 |
| Humana | 1120 |
| **Total** | **2448** |

---

## 2. Match Analysis

| Tier | Pairs |
|------|-------|
| STRONG (composite ≥ 0.70) | 1112 |
| POSSIBLE (composite ≥ 0.50) | 527 |
| WEAK (composite ≥ 0.40) | 907 |
| **Total candidate pairs** | **2546** |

---

## 3. Comparison Findings

| Finding Type | Count |
|--------------|-------|
| ✅ EQUIVALENT | 14 |
| ⚠️ DIFFERING | 2525 |
| 🚨 CONFLICTING | 7 |
| ❓ NOT_ASSESSABLE | 0 |

### Top Conflicting Rule Pairs

| Rule A | Rule B | Detail |
|--------|--------|--------|
| [Aetna] Aetna Better Health of Illinois will deny claims when this policy outlines the c… | [Humana] In addition to the policy, claims payments are subject to other plan requirement… | Aetna denies and Humana denies the same clinical scenario. |
| [Aetna] Aetna Better Health of Illinois will deny claims when this policy outlines the c… | [Humana] Dental services are generally not covered by the Medicare Program's basic benefi… | Aetna denies and Humana denies the same clinical scenario. |
| [Aetna] Aetna Better Health of Illinois will deny claims when this policy outlines the c… | [Humana] Claims that were denied as violating this pass-through billing policy when they … | Aetna denies and Humana denies the same clinical scenario. |
| [Aetna] Aetna Better Health of Illinois will deny claims when this policy outlines the c… | [Humana] Pass-through billing is generally prohibited, with limited exceptions, for Medic… | Aetna denies and Humana denies the same clinical scenario. |
| [Aetna] Aetna Better Health of Illinois will deny claims when this policy outlines the c… | [Humana] In addition to the policy, claims payments are subject to other plan requirement… | Aetna denies and Humana allows the same clinical scenario. |
| [UHC] These services are not covered by Medicare.… | [Humana] Dental services are generally not covered by the Medicare Program's basic benefi… | UHC denies and Humana denies the same clinical scenario. |
| [UHC] UnitedHealthcare considers only an Ambulance Supplier as eligible for reimbursem… | [Humana] covered. However, if providers report the mileage charge, it must be billed with… | UHC allows and Humana denies the same clinical scenario. |

---

## 4. Coverage Gaps

| Severity | Count |
|----------|-------|
| 🔴 HIGH | 180 |
| 🟡 MEDIUM | 1359 |
| 🟢 LOW | 257 |
| **Total unmatched rules** | **1796** |

### Top HIGH-Severity Gaps

| Payer | Policy | Category | Missing In | Rule (excerpt) |
|-------|--------|----------|-----------|----------------|
| Aetna | Esketamine Services | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims when reported using an inappropriate office or outp… |
| Aetna | Esketamine Services | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims when reported using an inappropriate prolonged serv… |
| Aetna | Esketamine Services | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims when appropriate HCPCS Level II Codes G2082 Office … |
| Aetna | Esketamine Services | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims for non-covered or inappropriate procedure G2082, G… |
| Aetna | Vitamin D Testing | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims when cPT Codes 82306 Vitamin D; 25 hydroxy, include… |
| Aetna | Vitamin D Testing | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims for non-covered or inappropriate procedure 82306: C… |
| Aetna | Allergy Testing and Immunotherapy | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims when cPT Codes 95004 Percutaneous tests (scratch, p… |
| Aetna | Drugs and Biologicals | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims when the inappropriate HCPCS code and corresponding… |
| Aetna | Holter Monitors | DENIAL | UHC, Humana | Aetna Better Health of Illinois will only reimburse for the appropriate reporting of Holter monitori… |
| Aetna | Chiropractic Services | DENIAL | UHC, Humana | Aetna Better Health of Illinois will deny claims for non-covered or inappropriate procedure 98940, 9… |

---

## 5. Next Steps

1. **Review CONFLICTING rules first** — these represent the highest risk of inconsistent claim adjudication.
2. **Address HIGH-severity coverage gaps** — missing denial or frequency rules may cause over-payment.
3. **Harmonise DIFFERING rules** — align frequency limits, modifier requirements, and code sets.
4. **Validate EQUIVALENT rules** — confirm they truly have the same clinical intent before treating as aligned.

Full details in: `comparison_report.xlsx`