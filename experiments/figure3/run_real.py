"""Run the delivered HF IntentSpec on the locally prepared MIMIC database."""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import duckdb

from figure3 import load_spec, run_figure3, REFERENCE_HASH
from llm_planner import configured_llm, make_planner

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'src/pneuma_seeker/services/db/datasets/mimic_iii/mimic_iii.db'
DEFAULT_OUTPUT = Path(__file__).parent / 'local_results/real_run.json'


def connect_database(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError('MIMIC database missing; run prepare_mimic3.py load first')
    # Faith's loader stores tables in main. The file's catalog name is mimic_iii,
    # so mimic_iii."admissions" resolves directly, without copying patient rows.
    db = duckdb.connect(str(path), read_only=True, config={'enable_external_access': 'false'})
    try:
        if db.execute('SELECT current_database()').fetchone()[0] != 'mimic_iii':
            raise ValueError('Expected database filename mimic_iii.db for supplied table paths')
        return db
    except Exception:
        db.close()
        raise


def run_real(db_path=DEFAULT_DB, output_path=DEFAULT_OUTPUT):
    spec = load_spec()
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    if digest != REFERENCE_HASH:
        raise ValueError('Independent reference is for the delivered HF contract only')
    db = connect_database(db_path)
    llm = None
    started = perf_counter()
    try:
        from figure3 import validate_spec
        validate_spec(spec, db)  # Check physical schema before incurring API usage.
        llm = configured_llm()
        planner = make_planner(llm)
        outcome = run_figure3(spec, db, planner,
                             repair=lambda plan, error: planner(spec, plan, error))
        # Independent reference uses distinct sets and INTERSECT rather than
        # reusing the model's predicates or deriving an answer from its output.
        numerator, denominator = db.execute('''
            WITH hf AS (
              SELECT DISTINCT hadm_id FROM main.diagnoses_icd WHERE icd9_code LIKE '428%'
              INTERSECT SELECT hadm_id FROM main.admissions
            ), deaths AS (
              SELECT hadm_id FROM hf
              INTERSECT SELECT hadm_id FROM main.admissions WHERE hospital_expire_flag=1
            )
            SELECT (SELECT COUNT(*) FROM deaths), (SELECT COUNT(*) FROM hf)
        ''').fetchone()
        expected = 100.0 * numerator / denominator if denominator else None
        answer = outcome.get('execution_result', {}).get('answer')
        matches = outcome['status'] == 'success' and (
            answer is None if expected is None else answer is not None and math.isclose(answer, expected, rel_tol=1e-9, abs_tol=1e-9))
        outcome['reference_check'] = {'numerator': numerator, 'denominator': denominator,
                                      'answer': expected, 'matches': matches}
        outcome['data_source'] = 'local_mimic_iii_database'
        outcome['model'] = llm.config.LLM_PATH
        outcome['usage'] = {'input_tokens': llm.total_input_tokens, 'output_tokens': llm.total_output_tokens}
        outcome['elapsed_seconds'] = round(perf_counter() - started, 3)
        outcome['created_at_utc'] = datetime.now(timezone.utc).isoformat()
        outcome['intent_sha256'] = digest
        outcome['question'] = spec['question']
        outcome['intent_spec'] = spec
        outcome['database'] = {'path': str(Path(db_path).resolve()), 'read_only': True,
                               'duckdb_version': duckdb.__version__}
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(outcome, indent=2) + '\n')
        # Keep evaluation labels out of the independent reviewer's input.
        handoff = {k: v for k, v in outcome.items() if k != 'reference_check'}
        output.with_name('figure4_handoff.json').write_text(json.dumps(handoff, indent=2) + '\n')
        return outcome
    finally:
        db.close()
        if llm is not None:
            llm.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    try:
        result = run_real(args.db)
        print(json.dumps({'status': result['status'], 'reference_matches': result['reference_check']['matches'],
                          'output': str(DEFAULT_OUTPUT), 'model': result['model']}))
        raise SystemExit(0 if result['reference_check']['matches'] else 1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
