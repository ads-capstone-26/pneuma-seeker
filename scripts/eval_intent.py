# scripts/eval_intent.py
"""
Evaluates Figure 2 (IntentPreparer) on a benchmark question file.

Per question it records the result status, the tables and columns Figure 2
selected, and the intermediate context, and scores them against the gold
annotations (data_sources, gold_columns, and answer_type "unanswerable").

Metrics:
- status_accuracy: "ready" for answerable questions, "unanswerable" otherwise
- table_recall / table_precision: required_tables vs. gold data_sources
- all_tables_found: every gold table is in required_tables
- column_recall: gold_columns covered by concepts + schema_mapping
- candidate_recall: gold tables present among S3 candidates (retrieval ceiling)
- code_recall / code_precision: for each code column filtered in gold_sql (icd9_code,
  itemid, org_name, ...), the codes matched by the gold condition vs. by the chosen
  filter_values, both evaluated over the column's distinct values (so '250%' and an
  explicit list compare fairly). Only codes are compared, never patient rows.

Usage (from the project root, with the pneuma_seeker env active):
    python scripts/eval_intent.py --allow-private-llm
    python scripts/eval_intent.py --allow-private-llm --ids mimic3-hard-1 mimic3-medium-1
    python scripts/eval_intent.py --dataset mimic_iii --rescore eval_results/intent_eval_....jsonl  # no LLM
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from time import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from run_intent import OUTPUT_DIR, build_preparer  # noqa: E402

from pneuma_seeker.shared.str_processor import clean_column_table_name  # noqa: E402


def gold_table_ids(data_sources: list[str]) -> list[str]:
    """
    Normalizes gold file names ("diagnoses_icd.csv") the way tables are named in the
    dataset. Wildcard sources ("prefix_*.csv") match a family of tables.
    """
    gold = []
    for source in data_sources:
        parts = Path(source).stem.split("*")
        gold.append("*".join(clean_column_table_name(part) if part else "" for part in parts))
    return sorted(set(gold))


def _matches(gold_item: str, table: str) -> bool:
    if "*" not in gold_item:
        return gold_item == table
    pattern = ".*".join(re.escape(part) for part in gold_item.split("*"))
    return re.fullmatch(pattern, table) is not None


def found_items(retrieved: list[str], gold: list[str]) -> set[str]:
    """Gold items hit by at least one retrieved table (a pattern counts once)."""
    return {item for item in gold if any(_matches(item, table) for table in retrieved)}


def predicted_columns(result: dict) -> set[str]:
    columns = {
        f"{c['table']}.{c['column']}" for c in result.get("concepts", []) if c.get("table") and c.get("column")
    }
    for table, fields in (result.get("schema_mapping") or {}).items():
        columns.update(f"{table}.{column}" for column in fields.values())
    return columns


def score(question: dict, result: dict, candidates: list[str]) -> dict:
    expected = "unanswerable" if question["answer_type"] == "unanswerable" else "ready"
    metrics = {"expected_status": expected, "status_correct": result["status"] == expected}
    if expected == "unanswerable":
        return metrics

    optional = set(question.get("optional_tables") or [])  # Neither required nor penalized
    gold = [table for table in gold_table_ids(question["data_sources"]) if table not in optional]
    chosen = [table for table in result.get("required_tables", []) if table not in optional]
    found = found_items(chosen, gold)
    gold_columns = set(question.get("gold_columns") or [])
    metrics.update({
        "table_recall": len(found) / len(gold) if gold else 0.0,
        "table_precision": len(found_items_per_table(chosen, gold)) / len(chosen) if chosen else 0.0,
        "all_tables_found": len(found) == len(gold),
        "column_recall": len(gold_columns & predicted_columns(result)) / len(gold_columns) if gold_columns else None,
        "candidate_recall": len(found_items(candidates, gold)) / len(gold) if gold else 0.0,
        "missing_tables": sorted(set(gold) - found),
        "missing_columns": sorted(gold_columns - predicted_columns(result)),
    })
    return metrics


CODE_COLUMNS = ("icd9_code", "itemid", "drug", "org_name", "spec_type_desc", "ab_name")
_GOLD_CONDITION = re.compile(
    r"(?:\b\w+\.)?\b(?P<column>" + "|".join(CODE_COLUMNS) + r")\s+(?P<op>IN|=|LIKE)\s*"
    r"(?P<value>\([^)]*\)|'[^']*'|\d+)",
    re.IGNORECASE,
)


def gold_code_conditions(question: dict) -> dict[tuple[str, str], list[str]]:
    """{(table, column): [SQL predicates on v]} for every code column filtered in gold_sql."""
    conditions: dict[tuple[str, str], list[str]] = {}
    for match in _GOLD_CONDITION.finditer(question.get("gold_sql") or ""):
        column = match["column"].lower()
        tables = [c.split(".")[0] for c in question.get("gold_columns") or [] if c.endswith("." + column)]
        if not tables:
            continue
        value = match["value"]
        if value.startswith("("):
            values = [v.strip().strip("'") for v in value[1:-1].split(",")]
            predicate = "v IN (" + ", ".join(f"'{v}'" for v in values) + ")"
        else:
            literal = value.strip("'")
            predicate = f"v {'LIKE' if match['op'].upper() == 'LIKE' else '='} '{literal}'"
        conditions.setdefault((tables[0], column), []).append(predicate)
    return conditions


def code_metrics(question: dict, result: dict, dataset: str, execute) -> dict:
    """Code recall/precision per gold code column, averaged; `execute(sql)` returns rows."""
    if question.get("gold_code_predicates") is not None:  # Given explicitly, e.g. by convert_ehrsql.py
        conditions = {tuple(key.split(".")): value for key, value in question["gold_code_predicates"].items()}
    else:
        conditions = gold_code_conditions(question)
    if not conditions or result.get("status") != "ready":
        return {}
    recalls, precisions, details = [], [], {}
    for (table, column), gold_predicates in conditions.items():
        chosen = [str(value) for c in result.get("concepts", []) if c.get("table") == table
                  and c.get("column") == column for value in c.get("filter_values") or []]
        chosen_predicates = [
            f"v LIKE '{value}'" if ("%" in value or "_" in value) else f"v = '{value}'"
            for value in (v.replace("'", "''") for v in chosen)
        ] or ["FALSE"]
        rows = execute(f"""
            SELECT v, ({' OR '.join(gold_predicates)}) AS g, ({' OR '.join(chosen_predicates)}) AS p
            FROM (SELECT DISTINCT CAST("{column}" AS VARCHAR) AS v FROM {dataset}."{table}" WHERE "{column}" IS NOT NULL)""")
        gold_codes = {row[0] for row in rows if row[1]}
        chosen_codes = {row[0] for row in rows if row[2]}
        hit = gold_codes & chosen_codes
        recalls.append(len(hit) / len(gold_codes) if gold_codes else 1.0)
        precisions.append(len(hit) / len(chosen_codes) if chosen_codes else 0.0)
        details[f"{table}.{column}"] = {
            "gold": len(gold_codes), "chosen": len(chosen_codes),
            "missing": sorted(gold_codes - chosen_codes)[:10], "extra": sorted(chosen_codes - gold_codes)[:10],
        }
    return {
        "code_recall": sum(recalls) / len(recalls),
        "code_precision": sum(precisions) / len(precisions),
        "code_details": details,
    }


def found_items_per_table(chosen: list[str], gold: list[str]) -> list[str]:
    """Chosen tables that match some gold item."""
    return [table for table in chosen if found_items([table], gold)]


def summarize(records: list[dict], by_category: bool = True) -> dict:
    ok = [r for r in records if not r["error"]]
    answerable = [r for r in ok if r["metrics"]["expected_status"] == "ready"]
    mean = lambda values: round(sum(values) / len(values), 3) if values else None  # noqa: E731
    return {
        "questions": len(records),
        "errors": len(records) - len(ok),
        "status_accuracy": mean([r["metrics"]["status_correct"] for r in ok]),
        "table_recall": mean([r["metrics"]["table_recall"] for r in answerable]),
        "table_precision": mean([r["metrics"]["table_precision"] for r in answerable]),
        "all_tables_found": mean([r["metrics"]["all_tables_found"] for r in answerable]),
        "column_recall": mean([r["metrics"]["column_recall"] for r in answerable if r["metrics"]["column_recall"] is not None]),
        "candidate_recall": mean([r["metrics"]["candidate_recall"] for r in answerable]),
        "code_recall": mean([r["metrics"]["code_recall"] for r in answerable if "code_recall" in r["metrics"]]),
        "code_precision": mean([r["metrics"]["code_precision"] for r in answerable if "code_precision" in r["metrics"]]),
        "avg_seconds": mean([r["seconds"] for r in ok]),
        "avg_tokens": mean([r["input_tokens"] + r["output_tokens"] for r in ok]),
    } | ({"by_category": {
        category: summarize([r for r in records if r.get("category") == category], by_category=False)
        for category in sorted({r["category"] for r in records if r.get("category")})
    }} if by_category and any(r.get("category") for r in records) else {})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="mimic_iii")
    parser.add_argument("--ids", nargs="+", default=None)
    parser.add_argument("--tag", default="", help="Label added to the output file name")
    parser.add_argument("--rescore", help="Recompute metrics for an earlier results file (no LLM calls)")
    parser.add_argument("--questions", help="Question file (default: data_src/<dataset>/<dataset>_tabular.json)")
    parser.add_argument("--allow-private-llm", action="store_true",
                        help="Allow credentialed datasets (MIMIC-III): only with an LLM endpoint approved under the PhysioNet DUA")
    args = parser.parse_args()

    questions_path = Path(args.questions) if args.questions else (
        PROJECT_ROOT / "data_src" / args.dataset / f"{args.dataset}_tabular.json")
    questions = json.loads(questions_path.read_text())
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]
    if args.rescore:
        rescore(Path(args.rescore), questions, args.dataset)
        return

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    preparer = build_preparer(f"eval_intent_{run_id}", args.allow_private_llm)
    llm = preparer.language_model_api.llm
    execute = _executor(preparer.db_api, args.dataset)
    out_path = OUTPUT_DIR / f"intent_eval_{args.dataset}{'_' + args.tag if args.tag else ''}_{run_id}.jsonl"

    records = []
    with open(out_path, "w") as out:
        for i, question in enumerate(questions, start=1):
            preparer.last_context = None  # Independent questions: no adjustment reuse
            in_tokens, out_tokens = llm.total_input_tokens, llm.total_output_tokens
            start = time()
            record = {"id": question["id"], "query": question["query"], "category": question.get("category"),
                      "error": None}
            try:
                result = preparer.prepare(question["query"], args.dataset)
                result_dict = result.model_dump(mode="json", exclude={"tables"})
                context = preparer.last_context
                candidates = [table.name for table in context.tables]
                record.update({
                    "status": result.status,
                    "result": result_dict,
                    "candidates": candidates,
                    "interpretation": context.interpretation,
                    "search_queries": context.search_queries,
                    "repair_issues": context.repair_issues,
                    "code_candidates": [
                        {"term": c.term, "system": c.code_system, "codes": [m["code"] for m in c.matches][:15],
                         "umls": c.umls_concepts}
                        for c in context.code_candidates
                    ],
                    "metrics": score(question, result_dict, candidates),
                })
                record["metrics"].update(code_metrics(question, result_dict, args.dataset, execute))
            except Exception as exc:
                record.update({"error": f"{type(exc).__name__}: {exc}", "metrics": {}})
            record.update({
                "seconds": round(time() - start, 1),
                "input_tokens": llm.total_input_tokens - in_tokens,
                "output_tokens": llm.total_output_tokens - out_tokens,
            })
            records.append(record)
            out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            out.flush()

            m = record["metrics"]
            if record["error"]:
                line = "ERROR " + record["error"][:120]
            elif m.get("expected_status") == "unanswerable":
                line = f"status={record['status']} (expected unanswerable)"
            else:
                line = (f"status={record['status']} tables={m['table_recall']:.2f} {_code_text(m)}"
                        f"cols={m['column_recall'] if m['column_recall'] is None else round(m['column_recall'], 2)} "
                        f"missing={m['missing_tables']}")
            print(f"[{i}/{len(questions)}] {question['id']}: {line}", flush=True)

    summary = summarize(records)
    print("\nSUMMARY " + json.dumps(summary))
    print(f"Per-question results: {out_path}")


def _code_text(metrics: dict) -> str:
    if "code_recall" not in metrics:
        return ""
    return f"codes(r/p)={metrics['code_recall']:.2f}/{metrics['code_precision']:.2f} "


def _executor(db_api, dataset: str):
    user, chat = "eval_user", "eval_intent_codes"
    db_api.link_dataset_tables(user, chat, dataset)
    return lambda sql: list(db_api.execute_query(user, chat, sql).itertuples(index=False, name=None))


def rescore(path: Path, questions: list[dict], dataset: str) -> None:
    """Recomputes all metrics for a results file, e.g. after adding a metric."""
    from pneuma_seeker.services.core.api.db import DBAPI
    from pneuma_seeker.shared.config import Config
    from pneuma_seeker.shared.logger import setup_logger

    config = Config(str(PROJECT_ROOT / ".env"))
    db_api = DBAPI(config, setup_logger("Rescore"), workspace_db_path=str(OUTPUT_DIR / "workspaces"))
    execute = _executor(db_api, dataset)
    by_id = {q["id"]: q for q in questions}
    records = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        question = by_id.get(record["id"])
        if question is None:
            continue
        if not record["error"]:
            record["metrics"] = score(question, record["result"], record["candidates"])
            record["metrics"].update(code_metrics(question, record["result"], dataset, execute))
            m = record["metrics"]
            if m.get("expected_status") == "ready":
                print(f"{record['id']:22s} {_code_text(m)}"
                      + " ".join(f"{k}: missing={v['missing']} extra={v['extra']}"
                                 for k, v in m.get("code_details", {}).items() if v["missing"] or v["extra"]))
        records.append(record)
    out_path = path.with_name(path.stem + "_rescored.jsonl")
    out_path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records))
    print("\nSUMMARY " + json.dumps(summarize(records)))
    print(f"Rescored results: {out_path}")


if __name__ == "__main__":
    main()
