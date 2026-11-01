# scripts/recompute_ehrsql_gold.py
"""
Recomputes EHRSQL reference answers on the team's MIMIC-III v1.4 copy, for
end-to-end evaluation.

EHRSQL's stored answers are not valid for our data: its database re-shifted each
patient's dates into 2100-2105, removed records after 2105-12-31 23:59:00, and
shuffled diagnoses, prescriptions, etc. across patients (Lee et al., 2022,
Section 3.2 and Supplementary D). We therefore re-execute each reference query on
our copy. The raw database is never modified.

Step 1, `--build` (once): materializes an EHRSQL-compatible copy of the tables
EHRSQL uses into a separate, git-ignored DuckDB file:
- text values lowercased (EHRSQL lowercases all values and queries);
- EHRSQL-only columns added: admissions.age (age at admission, from
  patients.dob), diagnoses_icd.charttime and procedures_icd.charttime
  (approximated by the admission time; MIMIC-III records no diagnosis or
  procedure time), transfers.careunit / wardid (= curr_careunit / curr_wardid).

Step 2 (default): translates each SQLite reference query to DuckDB
(strftime argument order, datetime(..., modifiers), current_time) and runs it on
that copy, with a per-query timeout. Two time modes:
- raw: tables as they are;
- truncate: records after current_time are treated as not yet existing, and
  admissions / ICU stays / transfers / prescriptions still open at
  current_time have no end time, mirroring EHRSQL's own preprocessing.

Answers are derived from credentialed data: they are written only to the
git-ignored data_src/ehrsql/ and never printed; the script prints only counts.

Usage (from the project root):
    python scripts/recompute_ehrsql_gold.py --build
    python scripts/recompute_ehrsql_gold.py --split valid --mode truncate
    python scripts/recompute_ehrsql_gold.py --split valid --mode raw --limit 200
"""
import argparse
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "data_src" / "ehrsql" / "mimic_iii"
OUTPUT_DIR = PROJECT_ROOT / "data_src" / "ehrsql"
DB_PATH = PROJECT_ROOT / "src" / "pneuma_seeker" / "services" / "db" / "datasets" / "mimic_iii" / "mimic_iii.db"
COMPAT_PATH = OUTPUT_DIR / "compat" / "mimic_iii_ehrsql_compat.duckdb"
EHRSQL_CURRENT_TIME = "2105-12-31 23:59:00"  # Lee et al. (2022), Section 3.2

# Tables in EHRSQL's MIMIC-III schema that the team's copy has (labevents, chartevents,
# outputevents and EHRSQL's cost table are not available).
EHRSQL_TABLES = ["patients", "admissions", "d_icd_diagnoses", "d_icd_procedures", "d_labitems", "d_items",
                 "diagnoses_icd", "procedures_icd", "prescriptions", "inputevents_cv", "microbiologyevents",
                 "icustays", "transfers"]
EXTRA_COLUMNS = {
    "admissions": {"age": "CAST(FLOOR(date_diff('day', p.dob, t.admittime) / 365.25) AS INTEGER)"},
    "diagnoses_icd": {"charttime": "a.admittime"},
    "procedures_icd": {"charttime": "a.admittime"},
    "transfers": {"careunit": "lower(t.curr_careunit)", "wardid": "t.curr_wardid"},
}
EXTRA_JOINS = {
    "admissions": 'LEFT JOIN src."patients" p ON p.subject_id = t.subject_id',
    "diagnoses_icd": 'LEFT JOIN src."admissions" a ON a.hadm_id = t.hadm_id',
    "procedures_icd": 'LEFT JOIN src."admissions" a ON a.hadm_id = t.hadm_id',
}
# truncate mode: (event time column that must be <= current_time, end-time columns nulled after it)
TRUNCATE = {
    "admissions": ("admittime", ["dischtime", "deathtime"]),
    "icustays": ("intime", ["outtime"]),
    "transfers": ("intime", ["outtime"]),
    "diagnoses_icd": ("charttime", []),
    "procedures_icd": ("charttime", []),
    "prescriptions": ("startdate", ["enddate"]),
    "inputevents_cv": ("charttime", []),
    "microbiologyevents": ("coalesce(charttime, chartdate)", []),
    "patients": (None, ["dod"]),
}


def build_compat() -> None:
    COMPAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COMPAT_PATH.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(f"ATTACH '{DB_PATH.as_posix()}' AS src (READ_ONLY)")
    for table in EHRSQL_TABLES:
        start = time.time()
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = 'src' AND table_name = ? ORDER BY ordinal_position", [table]).fetchall()
        select = [f'lower(t."{c}") AS "{c}"' if dtype == "VARCHAR" else f't."{c}"' for c, dtype in cols]
        select += [f'{expr} AS "{name}"' for name, expr in EXTRA_COLUMNS.get(table, {}).items()]
        con.execute(f'CREATE TABLE "{table}" AS SELECT {", ".join(select)} '
                    f'FROM src."{table}" t {EXTRA_JOINS.get(table, "")}')
        print(f"  {table:20s} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>12,} rows  {time.time() - start:5.1f}s")
    con.execute("DETACH src")
    con.close()
    tmp.replace(COMPAT_PATH)
    print(f"Compatibility copy -> {COMPAT_PATH} ({COMPAT_PATH.stat().st_size / 1e9:.2f} GB)")


def open_compat(mode: str, current_time: str) -> tuple[duckdb.DuckDBPyConnection, set[str]]:
    """In-memory connection whose tables are views over the compatibility copy (filtered in truncate mode)."""
    con = duckdb.connect()
    con.execute(f"ATTACH '{COMPAT_PATH.as_posix()}' AS c (READ_ONLY)")
    ct = f"TIMESTAMP '{current_time}'"
    for table in EHRSQL_TABLES:
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'c' AND table_name = ? "
            "ORDER BY ordinal_position", [table]).fetchall()]
        event_col, end_cols = TRUNCATE.get(table, (None, [])) if mode == "truncate" else (None, [])
        select = [f'CASE WHEN "{c}" > {ct} THEN NULL ELSE "{c}" END AS "{c}"' if c in end_cols else f'"{c}"'
                  for c in cols]
        where = f"WHERE {event_col} <= {ct}" if event_col else ""
        con.execute(f'CREATE VIEW "{table}" AS SELECT {", ".join(select)} FROM c."{table}" {where}')
    return con, set(EHRSQL_TABLES)


_UNIT = {"year": "YEAR", "month": "MONTH", "day": "DAY", "hour": "HOUR", "minute": "MINUTE"}


def _datetime_call(args: list[str]) -> str:
    """SQLite datetime(x, modifiers...) -> DuckDB expression."""
    expr = f"CAST({args[0]} AS TIMESTAMP)"
    for mod in args[1:]:
        m = mod.strip().strip("'").strip()
        start = re.fullmatch(r"start of (year|month|day)", m)
        shift = re.fullmatch(r"([+-]?\d+)\s+(year|month|day|hour|minute)s?", m)
        if start:
            expr = f"date_trunc('{start.group(1)}', {expr})"
        elif shift:
            expr = f"({expr} + INTERVAL ({int(shift.group(1))}) {_UNIT[shift.group(2)]})"
        else:
            raise ValueError(f"unsupported datetime modifier {m!r}")
    return expr


def _split_args(text: str) -> list[str]:
    args, depth, quote, current = [], 0, False, ""
    for ch in text:
        if ch == "'":
            quote = not quote
        if not quote:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                args.append(current)
                current = ""
                continue
        current += ch
    args.append(current)
    return [a.strip() for a in args]


def _replace_calls(sql: str, name: str, convert) -> str:
    """Replaces every call name(...) whose arguments contain no further strftime/datetime call."""
    pattern = re.compile(rf"\b{name}\s*\(")
    while True:
        changed = False
        for m in reversed(list(pattern.finditer(sql))):
            depth, i, quote = 1, m.end(), False
            while i < len(sql) and depth:
                if sql[i] == "'":
                    quote = not quote
                elif not quote:
                    depth += sql[i] == "("
                    depth -= sql[i] == ")"
                i += 1
            inner = sql[m.end():i - 1]
            if re.search(r"\b(strftime|datetime)\s*\(", inner):
                continue
            sql = sql[:m.start()] + convert(_split_args(inner)) + sql[i:]
            changed = True
            break
        if not changed:
            return sql


def _strftime_call(args: list[str]) -> str:
    fmt = args[0].replace("%y", "%Y")  # EHRSQL lowercased '%Y'
    expr = f"__duck_strftime__(CAST({args[1]} AS TIMESTAMP), {fmt})"  # renamed back in translate()
    # SQLite compares and subtracts '%j' (day of year) as numbers
    return f"CAST({expr} AS INTEGER)" if "%j" in fmt else expr


def translate(sql: str, current_time: str) -> str:
    """EHRSQL SQLite (lowercased) -> DuckDB."""
    out = re.sub(r"\bcurrent_time\b", f"TIMESTAMP '{current_time}'", sql)
    out = _replace_calls(out, "datetime", _datetime_call)
    out = _replace_calls(out, "strftime", _strftime_call)
    # The converted calls use a temporary name so they are not converted again
    return out.replace("__duck_strftime__(", "strftime(")


def run_with_timeout(con: duckdb.DuckDBPyConnection, sql: str, seconds: float):
    timer = threading.Timer(seconds, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="Materialize the compatibility copy and exit")
    parser.add_argument("--split", default="valid", choices=["valid", "test"])
    parser.add_argument("--mode", default="truncate", choices=["truncate", "raw"])
    parser.add_argument("--current-time", default=EHRSQL_CURRENT_TIME)
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds per query")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N answerable questions")
    args = parser.parse_args()

    if args.build:
        build_compat()
        return
    if not COMPAT_PATH.exists():
        raise SystemExit(f"No compatibility copy at {COMPAT_PATH}; run with --build first.")

    records = [r for r in json.loads((SOURCE_DIR / f"{args.split}.json").read_text()) if not r["is_impossible"]]
    if args.limit:
        records = records[:args.limit]
    con, tables = open_compat(args.mode, args.current_time)
    stats, errors, gold = Counter(), Counter(), {}
    started = time.time()
    for r in records:
        sql = " ".join(r["query"].split())
        if set(re.findall(r"\b(?:from|join)\s+([a-z_]+)", sql)) - tables:
            stats["skipped"] += 1
            continue
        try:
            rows = run_with_timeout(con, translate(sql, args.current_time), args.timeout)
        except duckdb.InterruptException:
            stats["timeout"] += 1
            continue
        except Exception as exc:
            stats["error"] += 1
            errors[type(exc).__name__ + ": " + str(exc).split("\n")[0][:90]] += 1
            continue
        values = [[None if v is None else str(v) for v in row] for row in rows]
        empty = not values or all(all(v is None for v in row) for row in values)
        zero = not empty and len(values) == 1 and len(values[0]) == 1 and values[0][0] in ("0", "False", "false")
        stats["ok"] += 1
        stats["empty"] += empty
        stats["zero/false"] += zero
        gold[r["id"]] = {"answer": values, "empty": empty, "mode": args.mode, "current_time": args.current_time}
    out = OUTPUT_DIR / f"ehrsql_{args.split}_gold_on_mimic_iii_{args.mode}.json"
    out.write_text(json.dumps(gold, ensure_ascii=False))
    print(f"{args.split} [{args.mode}]: {len(records)} answerable | executed {stats['ok']} | errors {stats['error']} "
          f"| timeouts {stats['timeout']} | skipped (missing table) {stats['skipped']} | {time.time() - started:.0f}s")
    print(f"  of executed: empty {stats['empty']} | single 0/false {stats['zero/false']} "
          f"| other {stats['ok'] - stats['empty'] - stats['zero/false']}")
    print("  error types:", dict(errors.most_common(8)) or "none")
    print(f"  answers -> {out} (not printed)")


if __name__ == "__main__":
    main()
