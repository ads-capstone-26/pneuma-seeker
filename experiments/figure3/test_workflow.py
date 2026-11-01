import json
import unittest
from copy import deepcopy

from workflow import run_workflow
from figure3 import load_spec, synthetic_database, reference_planner, InformationRequired
from pneuma_seeker.shared.schemas.core.intent_spec import IntentSpec, ClarificationNeeded, Unanswerable, AdjustmentRequest


class Preparer:
    def __init__(self, results):
        self.results = results
        self.requests = []

    def prepare(self, question, dataset, **kwargs):
        self.requests.append(kwargs)
        return self.results[len(self.requests) - 1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()
        full = deepcopy(self.spec)
        for concept in full['concepts']:
            concept['confidence'] = 1.0
        full['reasoning_confidence_score'] = 1.0
        self.ready = IntentSpec.model_validate(full)
        self.db = synthetic_database(self.spec)

    def tearDown(self):
        self.db.close()

    def plan(self, spec):
        self.assertEqual(spec['question'], self.spec['question'])
        self.assertTrue(all(t['sample_rows'] == [] for t in spec['tables']))
        return reference_planner(self.spec)

    def test_typed_handoff(self):
        p = Preparer([self.ready])
        r = run_workflow(self.spec['question'], p, self.db, self.plan)
        self.assertEqual(r['execution_result']['answer'], 25.0)
        self.assertEqual(r['intent_spec']['status'], 'ready')
        self.assertEqual(r['upstream_adjustments_used'], 0)

    def test_nonready_stops_before_planning(self):
        common = dict(question=self.spec['question'], dataset_name='mimic_iii')
        for result in [ClarificationNeeded(**common, ambiguity_markers=['scope'], clarification_questions=['Which scope?']),
                       Unanswerable(**common, reason='Missing tables')]:
            def forbidden(spec):
                self.fail('Must not plan a non-ready result')
            r = run_workflow(self.spec['question'], Preparer([result]), self.db, forbidden)
            self.assertEqual(r['status'], result.status)

    def test_adjustment_reaches_actual_interface(self):
        p = Preparer([self.ready, self.ready])
        calls = []
        def planner(spec):
            calls.append(1)
            if len(calls) == 1:
                raise InformationRequired('Confirm denominator')
            return self.plan(spec)
        r = run_workflow(self.spec['question'], p, self.db, planner)
        self.assertEqual(r['status'], 'success')
        self.assertIsInstance(p.requests[1]['adjustment_request'], AdjustmentRequest)
        self.assertEqual(p.requests[1]['adjustment_request'].reason, 'Confirm denominator')
        self.assertEqual(r['upstream_adjustments_used'], 1)

    def test_adjustment_budget(self):
        p = Preparer([self.ready, self.ready])
        def planner(spec):
            raise InformationRequired('Still missing')
        r = run_workflow(self.spec['question'], p, self.db, planner)
        self.assertEqual(len(p.requests), 2)
        self.assertIn('limit reached', r['requested_action'])

    def test_adjusted_refusal_stops(self):
        refused = Unanswerable(question=self.spec['question'], dataset_name='mimic_iii', reason='Cannot resolve')
        p = Preparer([self.ready, refused])
        def planner(spec):
            raise InformationRequired('Missing definition')
        r = run_workflow(self.spec['question'], p, self.db, planner)
        self.assertEqual(r['status'], 'unanswerable')
        self.assertEqual(r['upstream_adjustments_used'], 1)

    def test_new_conventions_reach_model_without_patients(self):
        from llm_planner import make_planner

        convention = {'id': 'duplicate_rows',
                      'rule': 'Count distinct hadm_id because diagnoses rows may repeat.'}
        ready = self.ready.model_copy(deep=True)
        ready.query_conventions = [convention]
        self.assertNotIn('patients', ready.required_tables)
        self.db.execute('INSERT INTO mimic_iii.diagnoses_icd SELECT * FROM mimic_iii.diagnoses_icd')
        captured = []
        plan = reference_planner(self.spec)
        plan.update(status='ready', steps=['Deduplicate admissions before counting'])

        class Model:
            def chat(self, messages, option):
                captured.append(json.loads(messages[1]['content'])['intent_spec'])
                return iter([json.dumps(plan)])

        result = run_workflow(self.spec['question'], Preparer([ready]), self.db,
                              make_planner(Model()))
        self.assertEqual(captured[0]['query_conventions'], [convention])
        self.assertNotIn('patients', captured[0]['schema_mapping'])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['execution_result']['answer'], 25.0)

    def test_execution_information_request_replans_with_updated_spec(self):
        from figure3 import InformationRequired
        updated = self.ready.model_copy(deep=True)
        updated.query_conventions = [{'id': 'confirmed', 'rule': 'Use admission counts'}]
        seen = []
        def planner(spec):
            seen.append(spec['query_conventions'])
            plan = reference_planner(self.spec)
            if len(seen) == 1:
                plan['sql'] = 'SELECT missing_column FROM mimic_iii.admissions'
            return plan
        def repair(spec, plan, error):
            raise InformationRequired('Confirm admission count mapping')
        p = Preparer([self.ready, updated])
        result = run_workflow(self.spec['question'], p, self.db, planner, repair=repair)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['upstream_adjustments_used'], 1)
        self.assertEqual(seen[-1], updated.query_conventions)

    def test_execution_adjustment_stops_after_one_retry(self):
        def planner(spec):
            return {'dialect': 'duckdb', 'parameters': [], 'sql': 'SELECT missing_column'}
        def repair(spec, plan, error):
            raise InformationRequired('Missing mapping')
        p = Preparer([self.ready, self.ready])
        result = run_workflow(self.spec['question'], p, self.db, planner, repair=repair)
        self.assertEqual(result['status'], 'execution_failure')
        self.assertEqual(result['failure_kind'], 'upstream_adjustment_limit')
        self.assertEqual(len(p.requests), 2)
