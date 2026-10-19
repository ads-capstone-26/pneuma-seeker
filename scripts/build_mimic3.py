# scripts/build_mimic3.py
"""
Loads the team's MIMIC-III v1.4 CSVs (data_src/MIMIC_III/) into a local DuckDB
dataset "mimic_iii" for Figure 2, without any LLM or embedding calls.

MIMIC-III is credentialed data (PhysioNet DUA): this script only reads local
files and writes local files, and never prints row contents. The standard
IndexingService is NOT used, because it sends sample rows and LLM-generated
column summaries to OpenAI. Figure 2 instead treats every loaded table as a
candidate ("catalog" mode in mimic_profile) and takes table and column
descriptions from the public MIMIC-III documentation.

Steps:
1. Links the MIMIC-III tables (see TABLES) that exist in data_src/MIMIC_III/
   into data_src/mimic_iii/dataset/
2. Writes data_src/mimic_iii/metadata.csv (public table descriptions)
3. Ingests them with the project's DatasetManager (same as POST /api/index
   without indexing), then re-reads ICD-9 code columns as text so that
   codes such as '0066' keep their leading zeros

Usage (from the project root, with the pneuma_seeker env active):
    python scripts/build_mimic3.py
"""
import csv
import os
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pneuma_seeker.services.core.intent.mimic_profile import MIMIC_III_TABLE_DOCS  # noqa: E402

DATASET_NAME = "mimic_iii"
SOURCE_DIR = PROJECT_ROOT / "data_src" / "MIMIC_III"
BASE_DIR = PROJECT_ROOT / "data_src" / DATASET_NAME
DATASET_DIR = BASE_DIR / "dataset"
METADATA_PATH = BASE_DIR / "metadata.csv"

# All 26 MIMIC-III v1.4 tables; the ones present in data_src/MIMIC_III/ are loaded.
TABLES = [
    "PATIENTS", "ADMISSIONS", "ICUSTAYS", "SERVICES", "TRANSFERS", "CALLOUT", "CAREGIVERS",
    "DIAGNOSES_ICD", "D_ICD_DIAGNOSES", "PROCEDURES_ICD", "D_ICD_PROCEDURES", "DRGCODES",
    "CPTEVENTS", "D_CPT", "LABEVENTS", "D_LABITEMS", "MICROBIOLOGYEVENTS", "PRESCRIPTIONS",
    "NOTEEVENTS", "CHARTEVENTS", "DATETIMEEVENTS", "INPUTEVENTS_CV", "INPUTEVENTS_MV",
    "OUTPUTEVENTS", "PROCEDUREEVENTS_MV", "D_ITEMS",
]
TEXT_COLUMNS = {"icd9_code", "drg_code", "loinc_code", "cpt_cd", "ndc", "gsn", "formulary_drug_cd"}  # Codes that look numeric but are text


def link_tables() -> tuple[list[str], list[str]]:
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    present, missing = [], []
    for table in TABLES:
        source = SOURCE_DIR / f"{table}.csv"
        target = DATASET_DIR / f"{table}.csv"
        if not source.exists():
            missing.append(table)
            if target.is_symlink():
                target.unlink()
            continue
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(os.path.relpath(source, target.parent))  # Relative: survives moving the project
        present.append(table)
    return present, missing


def write_metadata(tables: list[str]) -> None:
    with open(METADATA_PATH, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["table_name", "description"])
        for table in tables:
            writer.writerow([table.lower(), MIMIC_III_TABLE_DOCS[table.lower()][0]])


def fix_text_codes(db_path: Path, tables: list[str]) -> list[str]:
    """Re-reads code columns that DuckDB inferred as numbers, as VARCHAR from the CSV."""
    fixed = []
    con = duckdb.connect(str(db_path))
    try:
        for table in tables:
            name = table.lower()
            types = dict(con.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?", [name]
            ).fetchall())
            bad = [col for col in TEXT_COLUMNS if col in types and types[col] != "VARCHAR"]
            if not bad:
                continue
            path = (DATASET_DIR / f"{table}.csv").resolve().as_posix()
            header = next(csv.reader(open(path, encoding="utf-8-sig")))
            overrides = {col: "VARCHAR" for col in header if col.lower() in bad}
            select = ", ".join(f'"{col}" AS "{col.lower()}"' for col in header)
            con.execute(f"""
                CREATE OR REPLACE TABLE "{name}" AS
                SELECT {select} FROM read_csv('{path}', header=true, types={overrides!r}, sample_size=100000)
            """)
            fixed.append(f"{name}.{','.join(bad)}")
    finally:
        con.close()
    return fixed


def main() -> None:
    from pneuma_seeker.services.core.api.db import DBAPI
    from pneuma_seeker.shared.config import Config
    from pneuma_seeker.shared.logger import setup_logger

    present, missing = link_tables()
    write_metadata(present)
    print(f"Tables found: {len(present)}; not in {SOURCE_DIR.name}/: {', '.join(missing) or 'none'}")

    config = Config(str(PROJECT_ROOT / ".env"))
    db_api = DBAPI(config, setup_logger("MIMIC-III ingest"))
    db_api.ingest_dataset(DATASET_NAME, str(DATASET_DIR), str(METADATA_PATH), overwrite=True)
    db_path = db_api.pneuma_db.dataset_manager.dataset_db_path / DATASET_NAME / f"{DATASET_NAME}.db"
    fixed = fix_text_codes(db_path, present)
    print(f"Re-read as text: {', '.join(fixed) or 'none'}")

    # Schema-only summary (row counts are published in the MIMIC-III documentation)
    con = duckdb.connect(str(db_path), read_only=True)
    for (name,) in con.execute("SHOW TABLES").fetchall():
        rows = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        columns = con.execute(
            "SELECT column_name || ' ' || data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [name]).fetchall()
        print(f"{name:20s} {rows:>9} rows | {', '.join(c for (c,) in columns)}")
    con.close()
    print(f"Dataset -> {db_path}")


if __name__ == "__main__":
    main()
