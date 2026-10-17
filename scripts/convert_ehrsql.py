# scripts/convert_ehrsql.py
"""
Converts EHRSQL (MIMIC-III; Lee et al., NeurIPS 2022 Datasets and Benchmarks,
https://github.com/glee4810/EHRSQL, CC BY 4.0) into the Figure 2 evaluation
format used by eval_intent.py, for our MIMIC-III v1.4 dataset "mimic_iii".

EHRSQL was built on a modified MIMIC-III schema, so each question is classified:

    A  answerable with our tables (after renaming transfers.careunit/wardid)
    B  answerable after mapping EHRSQL-only columns:
         admissions.age            -> patients.dob + admissions.admittime (age is derived)
         diagnoses_icd.charttime   -> admissions.admittime (MIMIC-III has no diagnosis time;
         procedures_icd.charttime  -> admissions.admittime  an approximation, see notes)
    C  needs tables that are not loaded yet (labevents, prescriptions, ...): skipped
    E  answerable in EHRSQL only through its invented diagnosis/procedure charttime:
       ordering two diagnoses/procedures within the same admission ("diagnosed with B
       in the same hospital visit after A"). MIMIC-III has no such timestamps, so these
       are expected to be unanswerable here
    D  unanswerable (EHRSQL is_impossible), minus templates that our extra tables
       (services, callout, procedureevents_mv, family-history V codes) might answer

Gold tables and columns come from the EHRSQL SQL after mapping. A patient lookup
through admissions ("hadm_id in (select hadm_id from admissions where subject_id = N)")
makes admissions optional, since the event tables carry subject_id themselves. A dictionary used
only to look up a code by name (e.g. "select icd9_code from d_icd_diagnoses where
short_title = '...'") is not required: Figure 2 may pick the codes directly.
The names are resolved to codes in our dictionaries ("gold_code_predicates"), and
lowercased EHRSQL text values are matched case-insensitively. No answers are
computed, so the output contains nothing derived from patient records.

Usage (from the project root):
    python scripts/convert_ehrsql.py --split valid --per-template 1 --limit 30
"""
import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "data_src" / "ehrsql" / "mimic_iii"
OUTPUT_DIR = PROJECT_ROOT / "data_src" / "ehrsql"
DB_PATH = PROJECT_ROOT / "src" / "pneuma_seeker" / "services" / "db" / "datasets" / "mimic_iii" / "mimic_iii.db"

EHRSQL_TABLES = {
    "patients", "admissions", "d_icd_diagnoses", "d_icd_procedures", "d_labitems", "d_items", "diagnoses_icd",
    "procedures_icd", "labevents", "prescriptions", "cost", "chartevents", "inputevents_cv", "outputevents",
    "microbiologyevents", "icustays", "transfers",
}
RENAMES = {"transfers.careunit": "transfers.curr_careunit", "transfers.wardid": "transfers.curr_wardid"}
DERIVED = {
    "admissions.age": ["patients.dob", "admissions.admittime"],
    "diagnoses_icd.charttime": ["admissions.admittime"],
    "procedures_icd.charttime": ["admissions.admittime"],
}
# Unanswerable in EHRSQL, but possibly answerable with our extra tables or codes
EXCLUDED_IMPOSSIBLE = {"family history", "procedure duration", "quarantine release"}

# "select <dict>.icd9_code from <dict> where <dict>.short_title = '<name>'" -> codes by name
_NAME_LOOKUP = re.compile(
    r"select (d_icd_diagnoses|d_icd_procedures)\.icd9_code from \1 where \1\.(short_title|long_title) = '([^']*)'"
)
# A patient lookup through admissions; the event tables have subject_id themselves
_PATIENT_LOOKUP = re.compile(r"select admissions\.hadm_id from admissions where admissions\.subject_id = \d+ \)")
# Counting patients through admissions: "count(distinct admissions.subject_id) from admissions
# where admissions.hadm_id in (select <event>.hadm_id ...)" equals counting <event>.subject_id
_PATIENT_COUNT = re.compile(
    r"count\( distinct admissions\.subject_id \) from admissions where admissions\.hadm_id in \(")
# Ordering two coded events within one admission needs their (EHRSQL-only) charttime
_SAME_VISIT_ORDER = re.compile(r"t1\.charttime < t2\.charttime and t1\.hadm_id = t2\.hadm_id")
_ICD_TIME = re.compile(r"(diagnoses_icd|procedures_icd)\.charttime")
_TEXT_FILTER = re.compile(r"microbiologyevents\.(spec_type_desc|org_name) = '([^']*)'")
_EVENT_FOR_DICT = {"d_icd_diagnoses": "diagnoses_icd", "d_icd_procedures": "procedures_icd"}


def tables_and_columns(sql: str) -> tuple[set[str], set[str]]:
    tables = {t for t in re.findall(r"\b(?:from|join)\s+([a-z_]+)", sql) if t in EHRSQL_TABLES}
    columns = {m for m in re.findall(r"\b([a-z_]+\.[a-z_0-9]+)\b", sql) if m.split(".")[0] in EHRSQL_TABLES}
    return tables | {c.split(".")[0] for c in columns}, columns


def convert(record: dict, loaded: set[str], con) -> dict | None:
    base = {
        "id": f"ehrsql-{record['split']}-{record['id']}",
        "query": record["question"],
        "answer": None,
        "ehrsql_template": record.get("q_tag") or record.get("template"),
        "status": "ehrsql",
    }
    if record["is_impossible"]:
        if record["template"] in EXCLUDED_IMPOSSIBLE:
            return None
        return {**base, "category": "D", "answer_type": "unanswerable", "data_sources": [], "gold_columns": [],
                "gold_sql": None, "notes": "EHRSQL is_impossible"}

    sql = " ".join(record["query"].lower().split())
    if _SAME_VISIT_ORDER.search(sql) and len(_ICD_TIME.findall(sql)) >= 2:
        return {**base, "category": "E", "answer_type": "unanswerable", "data_sources": [], "gold_columns": [],
                "gold_sql": record["query"],
                "notes": "Orders diagnoses/procedures within one admission; MIMIC-III has no diagnosis/procedure time"}
    # Name lookups become gold codes; the dictionary is then not required
    predicates: dict[str, list[str]] = defaultdict(list)
    for dictionary, column, name in _NAME_LOOKUP.findall(sql):
        codes = [row[0] for row in con.execute(
            f'SELECT icd9_code FROM "{dictionary}" WHERE LOWER({column}) = ?', [name]).fetchall()]
        if codes:
            predicates[f"{_EVENT_FOR_DICT[dictionary]}.icd9_code"].append(
                "v IN (" + ", ".join(f"'{code}'" for code in codes) + ")")
    for column, value in _TEXT_FILTER.findall(sql):
        predicates[f"microbiologyevents.{column}"].append(f"LOWER(v) = '{value}'")
    stripped = _NAME_LOOKUP.sub("select 1", sql)
    without_lookup = _PATIENT_COUNT.sub("count( distinct 1 ) where 1 in (", _PATIENT_LOOKUP.sub("select 1 )", stripped))
    optional = []
    if "admissions" not in tables_and_columns(without_lookup)[0] and tables_and_columns(without_lookup)[0]:
        optional, stripped = ["admissions"], without_lookup

    tables, columns = tables_and_columns(stripped)
    if tables - loaded:
        return None  # Category C
    mapped, notes, category = set(), [], "A"
    for column in columns:
        if column in DERIVED:
            mapped.update(DERIVED[column])
            notes.append(f"{column} -> {' + '.join(DERIVED[column])}")
            category = "B"
        else:
            mapped.add(RENAMES.get(column, column))
    mapped_tables = {c.split(".")[0] for c in mapped} | (tables - {"d_icd_diagnoses", "d_icd_procedures"})
    mapped_tables |= {c.split(".")[0] for c in mapped}
    gold_sql = record["query"]
    for old, new in RENAMES.items():
        gold_sql = gold_sql.replace(old, new)
    return {
        **base,
        "category": category,
        "answer_type": "ehrsql",
        "data_sources": sorted(f"{t}.csv" for t in mapped_tables),
        "gold_columns": sorted(mapped),
        "gold_code_predicates": dict(predicates),
        "optional_tables": optional,
        "gold_sql": gold_sql,
        "notes": "; ".join(sorted(set(notes))) or "",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="valid", choices=["valid", "test"])
    parser.add_argument("--per-template", type=int, default=1, help="Questions sampled per template")
    parser.add_argument("--limit", type=int, default=30, help="Total questions (spread over A, B, D)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--reuse-sample", help="Re-convert the questions of an earlier sample file (same ids)")
    args = parser.parse_args()

    records = json.loads((SOURCE_DIR / f"{args.split}.json").read_text())
    con = duckdb.connect(str(DB_PATH), read_only=True)
    loaded = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    converted = [item for item in (convert(r, loaded, con) for r in records) if item]
    con.close()

    if args.reuse_sample:
        ids = [item["id"] for item in json.loads(Path(args.reuse_sample).read_text())]
        by_id = {item["id"]: item for item in converted}
        sample = [by_id[i] for i in ids if i in by_id]
        out = Path(args.reuse_sample)
        out.write_text(json.dumps(sample, ensure_ascii=False, indent=1) + "\n")
        print(f"Re-converted {len(sample)}/{len(ids)} questions: "
              f"{dict((c, sum(1 for s in sample if s['category'] == c)) for c in 'ABDE')} -> {out}")
        return

    # Stratified sample: per category, up to N questions per template, templates in random order
    rng = random.Random(args.seed)
    by_category: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for item in converted:
        by_category[item["category"]][item["ehrsql_template"]].append(item)
    share = {"A": 0.4, "B": 0.3, "D": 0.2, "E": 0.1}
    sample = []
    for category, templates in sorted(by_category.items()):
        quota = round(args.limit * share[category])
        names = sorted(templates)
        rng.shuffle(names)
        picked = []
        for name in names:
            picked += rng.sample(templates[name], min(args.per_template, len(templates[name])))
        sample += picked[:quota]
    counts = {c: sum(len(v) for v in t.values()) for c, t in by_category.items()}
    out = OUTPUT_DIR / f"ehrsql_{args.split}_sample{len(sample)}.json"
    out.write_text(json.dumps(sample, ensure_ascii=False, indent=1) + "\n")
    print(f"Usable in {args.split}: {counts} (C skipped)")
    print(f"Sample: {dict((c, sum(1 for s in sample if s['category'] == c)) for c in 'ABDE')} -> {out}")


if __name__ == "__main__":
    main()
