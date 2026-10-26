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
        self.assertEqual(result['status'], 'needs_information_adjustment')
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

    def test_reject_mutation_and_multiple_statements(self):
        for sql in ['DELETE FROM mimic_iii.admissions', 'SELECT 1; SELECT 2']:
            plan = {'dialect': 'duckdb', 'sql': sql, 'parameters': []}
            self.assertEqual(execute_plan(self.db, plan)['status'], 'execution_failure')
        self.assertEqual(self.answer(), 25.0)


if __name__ == '__main__':
    unittest.main()
