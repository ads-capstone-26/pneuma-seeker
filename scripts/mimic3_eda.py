# scripts/mimic3_eda.py
"""
Short exploratory overview of MIMIC-III v1.4 for slides: what the data is and
how it is organised.

Reads only the shared, aggregate schema profile (docs/mimic3/mimic3_schema_profile.json,
written by `python scripts/prepare_mimic3.py publish`), so it needs no access to
the credentialed data and shows no patient-level values.

Outputs:
    docs/figures/mimic3_overview.png / .svg   one slide-sized (16:9) figure
    a short text summary on the console (for speaker notes)

Usage (from the project root; needs matplotlib):
    python scripts/mimic3_eda.py
"""
import json
from pathlib import Path

import matplotlib.pyplot as plt

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE = PROJECT_ROOT / "docs" / "mimic3" / "mimic3_schema_profile.json"
OUT = PROJECT_ROOT / "docs" / "figures" / "mimic3_overview"

# UChicago palette: Maroon, Lake, Goldenrod, Dark Greyhound, Light Greyhound
MAROON, LAKE, GOLDENROD, DARK_GREY, LIGHT_GREY = "#800000", "#155F83", "#FFA319", "#767676", "#D6D6CE"

GROUPS = [  # (group label, colour, [(table, plain-language label)])
    ("Core: patients, admissions, ICU stays", MAROON, [
        ("patients", "Patients"),
        ("admissions", "Hospital admissions"),
        ("icustays", "ICU stays"),
    ]),
    ("Hospital stay events", LAKE, [
        ("prescriptions", "Medication orders"),
        ("diagnoses_icd", "Diagnoses (ICD-9)"),
        ("microbiologyevents", "Microbiology cultures"),
        ("cptevents", "Billed services (CPT)"),
        ("transfers", "Unit transfers"),
        ("procedures_icd", "Procedures (ICD-9)"),
        ("drgcodes", "Billing groups (DRG)"),
        ("services", "Clinical services"),
        ("callout", "ICU discharge planning"),
    ]),
    ("ICU events", GOLDENROD, [
        ("inputevents_cv", "ICU fluids and drugs (2001–08)"),
        ("datetimeevents", "ICU date/time observations"),
        ("procedureevents_mv", "ICU procedures (2008–12)"),
    ]),
    ("Dictionaries (code lookups)", DARK_GREY, [
        ("d_icd_diagnoses", "Diagnosis codes"),
        ("d_items", "ICU item codes"),
        ("caregivers", "Care providers"),
        ("d_icd_procedures", "Procedure codes"),
        ("d_labitems", "Lab test codes"),
        ("d_cpt", "CPT code ranges"),
    ]),
]


def main() -> None:
    profile = json.loads(PROFILE.read_text())
    rows = profile["data_fingerprint"]["row_counts"]
    tables = profile["tables"]
    total = sum(rows.values())

    # One bar per category of tables: total rows, share of all rows, and the tables it contains
    names = ["Core", "Hospital stay events", "ICU events", "Dictionaries"]
    contents = [
        "Patients, hospital admissions, ICU stays",
        "Medications, diagnoses, procedures, microbiology, billing codes, transfers, services, discharge planning",
        "ICU fluids and drugs (2001–08), date/time observations, ICU procedures (2008–12)",
        "Diagnosis, procedure, ICU item, lab test and CPT codes; care providers",
    ]
    group_rows = [sum(rows[t] for t, _ in members) for _, _, members in GROUPS]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12})
    fig = plt.figure(figsize=(13.33, 7.5), dpi=200)
    ax = fig.add_axes([0.20, 0.13, 0.76, 0.62])
    y = [3, 2, 1, 0]
    ax.barh(y, group_rows, color=[c for _, c, _ in GROUPS], height=0.42)
    ax.set_xlim(0, max(group_rows) * 1.02)
    ax.set_ylim(-0.75, 3.75)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{n}\n{len(m)} tables" for n, (_, _, m) in zip(names, GROUPS)],
                       fontsize=13, color="#2B2B2B", linespacing=1.5)
    for label in ax.get_yticklabels():
        label.set_fontweight("bold")
    for yi, n, what in zip(y, group_rows, contents):
        share = n / total
        pct = f"{share:.0%}" if share >= 0.01 else "<1%"
        ax.text(0, yi + 0.36, f"{n:,} rows  ·  {pct} of all rows", va="center", fontsize=12.5,
                weight="bold", color="#2B2B2B")
        ax.text(0, yi - 0.36, what, va="center", fontsize=10.5, color=DARK_GREY)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", length=0, pad=14)
    ax.set_xticks([])

    # Headline and key numbers
    fig.text(0.04, 0.93, "MIMIC-III at a glance", fontsize=26, weight="bold", color=MAROON)
    fig.text(0.04, 0.885, f"De-identified ICU records, Beth Israel Deaconess Medical Center, Boston, 2001–2012 "
                          f"· {len(rows)} tables · {total / 1e6:.1f} million rows", fontsize=13, color="#2B2B2B")
    kpis = [(rows["patients"], "patients"), (rows["admissions"], "hospital admissions"),
            (rows["icustays"], "ICU stays"), (len(profile["links"]), "links between tables")]
    for i, (n, what) in enumerate(kpis):
        x = 0.04 + i * 0.235
        fig.text(x, 0.825, f"{n:,}", fontsize=22, weight="bold", color=MAROON)
        fig.text(x, 0.797, what, fontsize=11.5, color=DARK_GREY)

    fig.text(0.04, 0.018, "Each patient has one or more hospital admissions; each admission has zero or more ICU stays. "
                          "Not in the team's copy: lab results, charted vitals, notes, outputs, MetaVision inputs.\n"
                          "Source: MIMIC-III v1.4 (PhysioNet), aggregate row counts from the shared schema profile.",
             fontsize=9.5, color=DARK_GREY, linespacing=1.4)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{OUT}.png", facecolor="white")
    fig.savefig(f"{OUT}.svg", facecolor="white")

    # Short summary for speaker notes (aggregate facts only)
    adm_types = tables["admissions"]["columns"]["admission_type"].get("values", {})
    dbsource = tables["icustays"]["columns"]["dbsource"].get("values", {})
    roles: dict[str, int] = {}
    for info in tables.values():
        for col in info["columns"].values():
            roles[col["role"]] = roles.get(col["role"], 0) + 1
    print(f"Tables: {len(rows)}  Rows: {total:,}  Columns: {sum(roles.values())}  Links: {len(profile['links'])}")
    print(f"Patients {rows['patients']:,}, admissions {rows['admissions']:,}, ICU stays {rows['icustays']:,}")
    print(f"Largest table: inputevents_cv ({rows['inputevents_cv'] / total:.0%} of all rows)")
    print("Admission types: " + ", ".join(f"{k} {v:,}" for k, v in adm_types.items()))
    print("ICU documentation system: " + ", ".join(f"{k} {v:,}" for k, v in dbsource.items()))
    print("Column roles: " + ", ".join(f"{k} {v}" for k, v in sorted(roles.items(), key=lambda kv: -kv[1])))
    print(f"Figure -> {OUT}.png / .svg")


if __name__ == "__main__":
    main()
