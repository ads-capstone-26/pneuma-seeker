# scripts/make_mimic3_questions.py
"""
Builds a DRAFT Figure 2 evaluation set for the team's MIMIC-III dataset
("mimic_iii", see build_mimic3.py), in the format of
data_src/<dataset>/<dataset>_tabular.json plus gold_columns, gold_sql, notes,
and status ("draft" until reviewed by the team).

Questions only use the tables that are loaded (no LABEVENTS, PRESCRIPTIONS,
CHARTEVENTS, INPUT/OUTPUTEVENTS yet); questions that need those tables are
"unanswerable" on purpose.

Answers are computed locally by gold_sql and written to the git-ignored
data_src/mimic_iii/ only. Nothing derived from the data is printed: they are
credentialed (PhysioNet DUA) and must not be shared.

Usage (from the project root):
    python scripts/make_mimic3_questions.py
"""
import json
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "src" / "pneuma_seeker" / "services" / "db" / "datasets" / "mimic_iii" / "mimic_iii.db"
OUTPUT_PATH = PROJECT_ROOT / "data_src" / "mimic_iii" / "mimic_iii_tabular.json"

AGE = "(EXTRACT(EPOCH FROM a.admittime) - EXTRACT(EPOCH FROM p.dob)) / 31557600.0"

QUESTIONS = [
    # ---------------- easy: one table ----------------
    {
        "id": "mimic3-easy-1",
        "query": "How many patients are in the database?",
        "answer_type": "numeric_exact",
        "gold_columns": ["patients.subject_id"],
        "gold_sql": "SELECT COUNT(DISTINCT subject_id) FROM patients",
    },
    {
        "id": "mimic3-easy-2",
        "query": "How many ICU stays are recorded?",
        "answer_type": "numeric_exact",
        "gold_columns": ["icustays.icustay_id"],
        "gold_sql": "SELECT COUNT(DISTINCT icustay_id) FROM icustays",
    },
    {
        "id": "mimic3-easy-3",
        "query": "What is the most common admission type?",
        "answer_type": "string_exact",
        "gold_columns": ["admissions.admission_type"],
        "gold_sql": "SELECT admission_type FROM admissions GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
    },
    {
        "id": "mimic3-easy-4",
        "query": "How many patients died in the hospital?",
        "answer_type": "numeric_exact",
        "gold_columns": ["admissions.subject_id", "admissions.hospital_expire_flag"],
        "gold_sql": "SELECT COUNT(DISTINCT subject_id) FROM admissions WHERE hospital_expire_flag = 1",
        "notes": "In-hospital death = admissions.hospital_expire_flag, not patients.expire_flag (any time).",
    },
    {
        "id": "mimic3-easy-5",
        "query": "What is the average ICU length of stay in days?",
        "answer_type": "numeric_approximate",
        "gold_columns": ["icustays.los"],
        "gold_sql": "SELECT ROUND(AVG(los), 2) FROM icustays",
    },
    {
        "id": "mimic3-easy-6",
        "query": "Which type of ICU has the most stays, by first care unit?",
        "answer_type": "string_exact",
        "gold_columns": ["icustays.first_careunit"],
        "gold_sql": "SELECT first_careunit FROM icustays GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
    },
    # ---------------- medium: codes or two tables ----------------
    {
        "id": "mimic3-medium-1",
        "query": "How many patients were diagnosed with sepsis?",
        "answer_type": "numeric_exact",
        "gold_columns": ["diagnoses_icd.subject_id", "diagnoses_icd.icd9_code"],
        "gold_sql": "SELECT COUNT(DISTINCT subject_id) FROM diagnoses_icd WHERE icd9_code IN ('99591', '99592', '78552')",
        "notes": "Sepsis = ICD-9 995.91 (sepsis), 995.92 (severe sepsis), 785.52 (septic shock). Septicemia (038.x) is a "
                 "related but separate concept; review whether it should count.",
    },
    {
        "id": "mimic3-medium-2",
        "query": "How many hospital admissions had acute myocardial infarction as the primary diagnosis?",
        "answer_type": "numeric_exact",
        "gold_columns": ["diagnoses_icd.hadm_id", "diagnoses_icd.icd9_code", "diagnoses_icd.seq_num"],
        "gold_sql": "SELECT COUNT(DISTINCT hadm_id) FROM diagnoses_icd WHERE seq_num = 1 AND icd9_code LIKE '410%'",
        "notes": "Primary = seq_num 1. AMI = ICD-9 410.x.",
    },
    {
        "id": "mimic3-medium-3",
        "query": "How many patients underwent coronary artery bypass graft surgery?",
        "answer_type": "numeric_exact",
        "gold_columns": ["procedures_icd.subject_id", "procedures_icd.icd9_code"],
        "gold_sql": "SELECT COUNT(DISTINCT subject_id) FROM procedures_icd WHERE icd9_code LIKE '361%'",
        "notes": "CABG = ICD-9 procedure 36.10-36.19.",
    },
    {
        "id": "mimic3-medium-4",
        "query": "What is the in-hospital mortality rate of emergency admissions, as a percentage?",
        "answer_type": "numeric_approximate",
        "gold_columns": ["admissions.admission_type", "admissions.hospital_expire_flag"],
        "gold_sql": "SELECT ROUND(100.0 * AVG(hospital_expire_flag), 2) FROM admissions WHERE admission_type = 'EMERGENCY'",
    },
    {
        "id": "mimic3-medium-5",
        "query": "How many ICU stays started in the medical ICU for patients covered by Medicare?",
        "answer_type": "numeric_exact",
        "gold_columns": ["icustays.icustay_id", "icustays.first_careunit", "icustays.hadm_id",
                         "admissions.hadm_id", "admissions.insurance"],
        "gold_sql": """SELECT COUNT(DISTINCT i.icustay_id) FROM icustays i JOIN admissions a ON a.hadm_id = i.hadm_id
                       WHERE i.first_careunit = 'MICU' AND a.insurance = 'Medicare'""",
        "notes": "Insurance is recorded per admission.",
    },
    {
        "id": "mimic3-medium-6",
        "query": "How many patients had Staphylococcus aureus grown in a blood culture?",
        "answer_type": "numeric_exact",
        "gold_columns": ["microbiologyevents.subject_id", "microbiologyevents.spec_type_desc",
                         "microbiologyevents.org_name"],
        "gold_sql": """SELECT COUNT(DISTINCT subject_id) FROM microbiologyevents
                       WHERE spec_type_desc = 'BLOOD CULTURE' AND org_name = 'STAPH AUREUS COAG +'""",
        "notes": "Organism name as stored: 'STAPH AUREUS COAG +'. Other blood specimen types (e.g. 'BLOOD CULTURE ( MYCO/F LYTIC BOTTLE)') are excluded.",
    },
    {
        "id": "mimic3-medium-7",
        "query": "How many hospital admissions were under the cardiac surgery service at some point?",
        "answer_type": "numeric_exact",
        "gold_columns": ["services.hadm_id", "services.curr_service"],
        "gold_sql": "SELECT COUNT(DISTINCT hadm_id) FROM services WHERE curr_service = 'CSURG'",
    },
    {
        "id": "mimic3-medium-8",
        "query": "How many female patients have been diagnosed with heart failure?",
        "answer_type": "numeric_exact",
        "gold_columns": ["patients.subject_id", "patients.gender", "diagnoses_icd.subject_id", "diagnoses_icd.icd9_code"],
        "gold_sql": """SELECT COUNT(DISTINCT p.subject_id) FROM patients p JOIN diagnoses_icd d ON d.subject_id = p.subject_id
                       WHERE p.gender = 'F' AND d.icd9_code LIKE '428%'""",
        "notes": "Heart failure = ICD-9 428.x (other codes such as 402.x1 hypertensive heart disease with heart failure are excluded).",
    },
    # ---------------- hard: several tables, derived quantities ----------------
    {
        "id": "mimic3-hard-1",
        "query": "Among hospital admissions with a heart failure diagnosis, what percentage ended in death during that admission?",
        "answer_type": "numeric_approximate",
        "gold_columns": ["diagnoses_icd.hadm_id", "diagnoses_icd.icd9_code", "admissions.hadm_id",
                         "admissions.hospital_expire_flag"],
        "gold_sql": """SELECT ROUND(100.0 * AVG(a.hospital_expire_flag), 2) FROM admissions a
                       WHERE a.hadm_id IN (SELECT hadm_id FROM diagnoses_icd WHERE icd9_code LIKE '428%')""",
    },
    {
        "id": "mimic3-hard-2",
        "query": "How many ICU stays with invasive mechanical ventilation lasted more than 7 days in the ICU?",
        "answer_type": "numeric_exact",
        "gold_columns": ["procedureevents_mv.icustay_id", "procedureevents_mv.itemid", "icustays.icustay_id", "icustays.los"],
        "gold_sql": """SELECT COUNT(DISTINCT i.icustay_id) FROM icustays i
                       JOIN procedureevents_mv v ON v.icustay_id = i.icustay_id
                       WHERE v.itemid = 225792 AND i.los > 7""",
        "notes": "Invasive ventilation = d_items 225792 in procedureevents_mv (MetaVision only, 2008-2012). CareVue "
                 "ventilation would need CHARTEVENTS, which is not loaded.",
    },
    {
        "id": "mimic3-hard-3",
        "query": "What percentage of Staphylococcus aureus isolates tested against oxacillin were resistant?",
        "answer_type": "numeric_approximate",
        "gold_columns": ["microbiologyevents.org_name", "microbiologyevents.ab_name", "microbiologyevents.interpretation"],
        "gold_sql": """SELECT ROUND(100.0 * AVG(CASE WHEN interpretation = 'R' THEN 1 ELSE 0 END), 2)
                       FROM microbiologyevents
                       WHERE org_name = 'STAPH AUREUS COAG +' AND ab_name = 'OXACILLIN' AND interpretation IS NOT NULL""",
        "notes": "Isolate test = one row with an interpretation (S/R/I/P).",
    },
    {
        "id": "mimic3-hard-4",
        "query": "What is the average age at admission of patients with type 2 diabetes, excluding patients older than 89?",
        "answer_type": "numeric_approximate",
        "gold_columns": ["patients.subject_id", "patients.dob", "admissions.subject_id", "admissions.hadm_id",
                         "admissions.admittime", "diagnoses_icd.hadm_id", "diagnoses_icd.icd9_code"],
        "gold_sql": f"""SELECT ROUND(AVG(age), 1) FROM (
                          SELECT {AGE} AS age FROM admissions a JOIN patients p ON p.subject_id = a.subject_id
                          WHERE a.hadm_id IN (SELECT hadm_id FROM diagnoses_icd
                                              WHERE icd9_code LIKE '250_0' OR icd9_code LIKE '250_2'))
                        WHERE age <= 89""",
        "notes": "Per admission. Type 2 = ICD-9 250.x0 / 250.x2 (5th digit 0 or 2). Patients over 89 have shifted DOBs (~300 years).",
    },
    {
        "id": "mimic3-hard-5",
        "query": "How many hospital admissions were followed by another admission of the same patient within 30 days of discharge?",
        "answer_type": "numeric_exact",
        "gold_columns": ["admissions.subject_id", "admissions.hadm_id", "admissions.admittime", "admissions.dischtime"],
        "gold_sql": """SELECT COUNT(*) FROM (
                          SELECT dischtime, LEAD(admittime) OVER (PARTITION BY subject_id ORDER BY admittime) AS next_admit
                          FROM admissions)
                        WHERE next_admit IS NOT NULL AND next_admit - dischtime <= INTERVAL 30 DAY""",
        "notes": "Counts index admissions. Does not exclude deaths or newborns; review whether elective readmissions count.",
    },
    {
        "id": "mimic3-hard-6",
        "notes": "drg_type is stored as 'APR ' with a trailing space.",
        "query": "What is the average APR-DRG severity of illness for admissions with a sepsis diagnosis?",
        "answer_type": "numeric_approximate",
        "gold_columns": ["drgcodes.hadm_id", "drgcodes.drg_type", "drgcodes.drg_severity",
                         "diagnoses_icd.hadm_id", "diagnoses_icd.icd9_code"],
        "gold_sql": """SELECT ROUND(AVG(drg_severity), 2) FROM drgcodes
                       WHERE TRIM(drg_type) = 'APR' AND hadm_id IN
                         (SELECT hadm_id FROM diagnoses_icd WHERE icd9_code IN ('99591', '99592', '78552'))""",
    },
    # ---------------- unanswerable with the loaded tables ----------------
    {
        "id": "mimic3-unanswerable-1",
        "query": "What is the average HbA1c value of patients with diabetes?",
        "answer_type": "unanswerable",
        "gold_columns": [],
        "gold_sql": None,
        "notes": "Needs LABEVENTS (not loaded); D_LABITEMS alone has no results.",
    },
    {
        "id": "mimic3-unanswerable-2",
        "query": "How many patients were prescribed vancomycin?",
        "answer_type": "unanswerable",
        "gold_columns": [],
        "gold_sql": None,
        "notes": "Needs PRESCRIPTIONS (not loaded). microbiologyevents.ab_name = 'VANCOMYCIN' is a sensitivity test, not a prescription.",
    },
    {
        "id": "mimic3-unanswerable-3",
        "query": "What is the average heart rate during the first 24 hours of ICU stays?",
        "answer_type": "unanswerable",
        "gold_columns": [],
        "gold_sql": None,
        "notes": "Needs CHARTEVENTS (not loaded).",
    },
    {
        "id": "mimic3-unanswerable-4",
        "query": "What is the average household income of ICU patients?",
        "answer_type": "unanswerable",
        "gold_columns": [],
        "gold_sql": None,
        "notes": "Not recorded in MIMIC-III.",
    },
]


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    items, failed = [], []
    for question in QUESTIONS:
        sql = question["gold_sql"]
        answer = None
        if sql is not None:
            try:
                rows = con.execute(sql).fetchall()
                answer = rows[0][0] if rows else None
                answer = float(answer) if hasattr(answer, "as_tuple") else answer
            except Exception as exc:
                failed.append(f"{question['id']}: {type(exc).__name__}")
            if answer is None or answer == 0:
                failed.append(f"{question['id']}: empty or zero result")
        tables = sorted({column.split(".")[0] for column in question["gold_columns"]})
        items.append({
            "id": question["id"],
            "query": question["query"],
            "answer": answer,
            "answer_type": question["answer_type"],
            "data_sources": [f"{table}.csv" for table in tables],
            "gold_columns": question["gold_columns"],
            "gold_sql": " ".join(sql.split()) if sql else None,
            "notes": question.get("notes", ""),
            "status": "draft",
        })
    con.close()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n")
    # Answers are credentialed-data aggregates: report only whether each gold SQL produced one.
    print(f"{len(items)} questions -> {OUTPUT_PATH} (answers not printed)")
    print("Gold SQL problems:", failed or "none")


if __name__ == "__main__":
    main()
