"""Live Figure 2 -> Figure 3 -> Figure 4 handoff on local MIMIC-III."""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from figure3 import load_spec, run_figure3
from llm_planner import configured_llm, make_planner
from run_real import connect_database, DEFAULT_DB
from pneuma_seeker.shared.schemas.core.intent_spec import AdjustmentRequest, IntentSpec

OUTPUT = Path(__file__).parent / 'local_results/integrated_run.json'


class LocalDatabaseAPI:
    """The DBAPI subset used by Figure 2's real MIMIC catalog/code lookup path.

    Uses the same read-only connection as Figure 3, without a Postgres server or
    writable workspace copy. It does not replace Figure 2 interpretation logic.
    """
    def __init__(self, db):
        self.db = db

    def link_dataset_tables(self, user_id, chat_id, dataset_name):
        if dataset_name != 'mimic_iii':
            raise ValueError('This workflow supports the local mimic_iii catalog')

    def execute_query(self, user_id, chat_id, sql, params=None):
        return self.db.execute(sql, params or []).fetchdf()

    def get_table_description(self, dataset_name, table_name):
        from pneuma_seeker.services.core.data_prep.mimic3_docs import MIMIC_III_TABLE_DOCS
        return MIMIC_III_TABLE_DOCS.get(table_name, ('', {}))[0]


class LocalLanguageModelAPI:
    """Reuse the configured adapter with bounded Figure 2 generation."""
    def __init__(self, llm):
        self.llm = llm

    def chat(self, messages, option):
        # Preserve the teammate's prompts, JSON mode and conversational retries.
        return self.llm.chat(messages, replace(option, max_new_tokens=8000, stream=False))


def run_workflow(question, preparer, db, planner, *, dataset='mimic_iii',
                 interactive=True, repair=None):
    """Real typed handoff; one upstream adjustment, separate SQL repair budget.

    repair(spec, plan, error) receives the latest Figure 2 output, never the old
    contract after an adjustment. Non-ready Figure 2 results stop before SQL.
    """
    history = []
    adjustment = None
    for used in range(2):
        try:
            result = preparer.prepare(question, dataset, interactive=interactive,
                                      use_memory=False, adjustment_request=adjustment)
        except Exception as exc:
            return {'status': 'figure2_failure', 'reason': type(exc).__name__,
                    'question': question, 'figure2_history': history,
                    'upstream_adjustments_used': used}
        full = result.model_dump(mode='json')
        history.append(full)
        if not isinstance(result, IntentSpec):
            return {'status': result.status, 'question': question,
                    'figure2_result': full, 'figure2_history': history,
                    'upstream_adjustments_used': used}
        spec = result.to_figure3_view(max_sample_rows=0)
        repair_plan = (lambda plan, error: repair(spec, plan, error)) if repair else None
        outcome = run_figure3(spec, db, planner, repair_plan)
        outcome.update(question=question, intent_spec=full, figure3_input=spec,
                       figure2_history=deepcopy(history), upstream_adjustments_used=used)
        if outcome['status'] != 'needs_information_adjustment':
            return outcome
        if used == 1:
            outcome['requested_action'] = 'stop: upstream adjustment limit reached'
            return outcome
        adjustment = AdjustmentRequest(reason=outcome['reason'])


def run_integrated(question, db_path=DEFAULT_DB, output=OUTPUT):
    from pneuma_seeker.services.core.intent.main import IntentPreparer
    llm = configured_llm()
    db = None
    preparer = None
    start = perf_counter()
    try:
        db = connect_database(db_path)
        logger = logging.getLogger('figure2_integration')
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        preparer = IntentPreparer('local_figure3', 'integrated', llm.config, logger,
                                  LocalDatabaseAPI(db), LocalLanguageModelAPI(llm),
                                  allow_private_llm=True, sample_size=0)
        planner = make_planner(llm)
        outcome = run_workflow(question, preparer, db, planner,
                               repair=lambda spec, plan, error: planner(spec, plan, error))
        outcome.update(model=llm.config.LLM_PATH,
                       data_source='local_mimic_iii_database',
                       created_at_utc=datetime.now(timezone.utc).isoformat(),
                       elapsed_seconds=round(perf_counter() - start, 3),
                       usage={'input_tokens': llm.total_input_tokens,
                              'output_tokens': llm.total_output_tokens},
                       database={'path': str(Path(db_path).resolve()), 'read_only': True},
                       figure2_source_commit='d30ca4835efca211ef186c7d4d9bc55ddcb10469')
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(outcome, indent=2) + '\n')
        # No reference answers or evaluation labels are sent to Figure 4.
        output.with_name('figure4_integrated_handoff.json').write_text(json.dumps(outcome, indent=2) + '\n')
        return outcome
    finally:
        if db is not None:
            db.close()
        if preparer is not None:
            preparer.umls._client.close()
        llm.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question', default=load_spec()['question'])
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    try:
        result = run_integrated(args.question, args.db)
        print(json.dumps({'status': result['status'], 'model': result['model'],
                          'upstream_adjustments_used': result['upstream_adjustments_used'],
                          'output': str(OUTPUT)}))
        raise SystemExit(0 if result['status'] == 'success' else 1)
    except Exception as exc:
        print(json.dumps({'status': 'setup_failure', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
