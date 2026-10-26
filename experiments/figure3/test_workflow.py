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
