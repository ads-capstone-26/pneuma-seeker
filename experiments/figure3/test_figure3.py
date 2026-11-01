import unittest
from copy import deepcopy

from figure3 import load_spec, synthetic_database, run_figure3, reference_planner, execute_plan
from figure3 import run_with_upstream


class Figure3Tests(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()
        self.db = synthetic_database(self.spec)

    def tearDown(self):
        self.db.close()

    def answer(self):
        result = run_figure3(self.spec, self.db)
        self.assertEqual(result['status'], 'success', result)
        return result['execution_result']['answer']

    def test_cohort_and_unchanged_input(self):
        original = deepcopy(self.spec)
        self.assertEqual(self.answer(), 25.0)
        self.assertEqual(self.spec, original)

    def test_upstream_adjustment_once(self):
        broken = deepcopy(self.spec)
        broken.pop('schema_mapping')
        calls = []
        def adjust(spec, issue):
            calls.append(issue['status'])
            return deepcopy(self.spec)
        result = run_with_upstream(broken, self.db, reference_planner, adjust=adjust)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['upstream_adjustments_used'], 1)
        self.assertEqual(len(calls), 1)

    def test_upstream_does_not_loop(self):
        broken = deepcopy(self.spec)
        broken.pop('schema_mapping')
        calls = []
        def adjust(spec, issue):
            calls.append(1)
            return spec
        result = run_with_upstream(broken, self.db, reference_planner, adjust=adjust)
        self.assertEqual(result['status'], 'execution_failure')
        self.assertIn('limit reached', result['requested_action'])
        self.assertEqual(len(calls), 1)

    def test_no_deaths(self):
        self.db.execute('UPDATE mimic_iii.admissions SET hospital_expire_flag=0')
        self.assertEqual(self.answer(), 0.0)

    def test_all_deaths(self):
        self.db.execute('UPDATE mimic_iii.admissions SET hospital_expire_flag=1')
        self.assertEqual(self.answer(), 100.0)

    def test_empty_cohort(self):
        self.db.execute('DELETE FROM mimic_iii.diagnoses_icd')
        self.assertIsNone(self.answer())

    def test_duplicate_diagnoses(self):
        self.db.execute('INSERT INTO mimic_iii.diagnoses_icd SELECT * FROM mimic_iii.diagnoses_icd')
        self.assertEqual(self.answer(), 25.0)

    def test_missing_mapping(self):
        self.spec['schema_mapping'].pop('admissions')
        self.assertEqual(run_figure3(self.spec, self.db)['status'], 'needs_information_adjustment')

    def test_changed_intent_is_not_upstream_error(self):
        self.spec['question'] = 'A different question'
        self.assertEqual(run_figure3(self.spec, self.db)['status'], 'unsupported_intent')

    def test_physical_schema_mismatch(self):
        self.db.execute('ALTER TABLE mimic_iii.admissions RENAME COLUMN hospital_expire_flag TO wrong_flag')
        self.assertEqual(run_figure3(self.spec, self.db)['status'], 'needs_information_adjustment')

    def test_repair_and_limit(self):
        plan = reference_planner(self.spec)
        plan['sql'] = plan['sql'].replace('hospital_expire_flag', 'hospital_expire_typo')
        def repair(current, error):
            current['sql'] = current['sql'].replace('hospital_expire_typo', 'hospital_expire_flag')
            return current
        result = execute_plan(self.db, plan, repair)
        self.assertEqual(result['execution_result']['answer'], 25.0)
        self.assertEqual(len(result['attempts']), 2)
        failed = execute_plan(self.db, plan, lambda p, e: p)
        self.assertEqual(failed['status'], 'execution_failure')
        self.assertEqual(len(failed['attempts']), 2)

    def test_parameter_count_repair_and_limit(self):
        plan = reference_planner(self.spec)
        plan['parameters'].append('extra')
        result = execute_plan(self.db, plan, lambda p, e: reference_planner(self.spec))
        self.assertEqual(result['execution_result']['answer'], 25.0)
        self.assertEqual(len(result['attempts']), 2)
        failed = execute_plan(self.db, plan, lambda p, e: p)
        self.assertEqual(failed['status'], 'execution_failure')
        self.assertEqual(len(failed['attempts']), 2)

    def test_reject_mutation_and_multiple_statements(self):
        for sql in ['DELETE FROM mimic_iii.admissions', 'SELECT 1; SELECT 2']:
            plan = {'dialect': 'duckdb', 'sql': sql, 'parameters': []}
            self.assertEqual(execute_plan(self.db, plan)['status'], 'execution_failure')
        self.assertEqual(self.answer(), 25.0)


class ResultShapeTests(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()
        self.db = synthetic_database(self.spec)

    def tearDown(self):
        self.db.close()

    def execute(self, sql, kind='table', **kwargs):
        return execute_plan(self.db, {'dialect': 'duckdb', 'sql': sql, 'parameters': []},
                            answer_type=kind, **kwargs)

    def test_grouped_table_and_full_result(self):
        spec = deepcopy(self.spec)
        spec['output_spec']['answer_type'] = 'table'
        spec['aggregation_level'] = 'admission_type'
        plan = {'dialect': 'duckdb', 'parameters': [], 'sql': 'SELECT admission_type, COUNT(DISTINCT hadm_id) AS n FROM mimic_iii.admissions GROUP BY admission_type ORDER BY admission_type'}
        result = run_figure3(spec, self.db, lambda _: plan)
        self.assertEqual(result['status'], 'success')
        rows = result['execution_result']['rows']
        self.assertEqual(rows, [{'admission_type': 'ELECTIVE', 'n': 1}, {'admission_type': 'EMERGENCY', 'n': 4}, {'admission_type': 'NEWBORN', 'n': 1}])

    def test_preview_does_not_truncate_full_result(self):
        result = self.execute('SELECT range AS n FROM range(150) ORDER BY n')['execution_result']
        self.assertEqual(result['row_count'], 150)
        self.assertEqual(len(result['preview']), 100)
        self.assertEqual(len(result['rows']), 150)
        self.assertEqual(result['result_reference'], '#/execution_result/rows')

    def test_empty_table_is_success(self):
        result = self.execute('SELECT hadm_id FROM mimic_iii.admissions WHERE FALSE')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['execution_result']['row_count'], 0)
        self.assertEqual(result['execution_result']['schema'][0]['name'], 'hadm_id')

    def test_scalar_shapes_and_list(self):
        for sql, kind, expected in [("SELECT 'ok' AS answer", 'string', 'ok'), ('SELECT TRUE AS answer', 'boolean', True)]:
            self.assertEqual(self.execute(sql, kind)['execution_result']['answer'], expected)
        self.assertEqual(self.execute('SELECT 1 AS n UNION ALL SELECT 2', 'list')['execution_result']['row_count'], 2)
        self.assertEqual(self.execute("SELECT 'bad' AS answer", 'number')['status'], 'execution_failure')

    def test_serializable_dates_and_decimals(self):
        import json
        result = self.execute("SELECT DATE '2026-01-01' AS day, 1.25::DECIMAL(10,2) AS amount")
        self.assertEqual(result['execution_result']['rows'], [{'day': '2026-01-01', 'amount': '1.25'}])
        json.dumps(result, allow_nan=False)

    def test_duplicate_aliases_rejected(self):
        self.assertEqual(self.execute('SELECT 1 AS x, 2 AS x')['status'], 'execution_failure')

    def test_sql_repair_does_not_require_upstream(self):
        result = execute_plan(self.db, {'dialect': 'duckdb', 'parameters': [], 'sql': 'SELECT * FROM typo_table'},
                              repair=lambda plan, error: {'dialect': 'duckdb', 'parameters': [], 'sql': 'SELECT 1 AS answer'})
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(result['attempts']), 2)


if __name__ == '__main__':
    unittest.main()
