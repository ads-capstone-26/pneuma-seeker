# Figure 3 local handoff

Input: `experiments/intent_spec_example_mimic3_figure3.json`, unchanged.

From the repository root, activate `pneuma_seeker`, then run:

```sh
python experiments/figure3/figure3.py
python -m unittest discover -s experiments/figure3 -p 'test_*.py' -v
```

Or open `figure3_mimic3_prototype.ipynb`, select the `pneuma_seeker` kernel,
and Run All. The final section has `RUN_INTEGRATED_WORKFLOW=True` and incurs API usage.
Set it to False for offline-only runs. Earlier live demonstrations default to False. Only DuckDB is needed offline.

The offline reference planner supports this exact delivered JSON, guarded by a
canonical JSON hash. It is a regression baseline, not a general semantic planner.
Changes to the contract return `unsupported_intent`, not an upstream data error.
The JSON supplies metadata, not real records or a database connection.

The fictional fixture has four qualifying admissions (101–104), one death (101),
and an expected answer of 25%. It includes repeated diagnoses, multiple admissions
for one patient, secondary diagnoses, a newborn admission (no adult filter was
requested), negative duration (retained for counts), non-HF deaths, and orphan diagnoses.
NULL for an empty cohort is a local policy; it is not specified by Figure 2.
`row_count=1` describes the scalar output, not the cohort size. Results are inline.

Integration entry point: `run_figure3(spec, db, planner, repair=None)`.
Statuses: `success`, `needs_information_adjustment`, `unsupported_intent`,
`planning_failure`, `execution_failure`. SQL parser/binder errors and parameter-count mismatches allow one repair.
`run_with_upstream` accepts an `adjust(spec, issue)` callback and allows at most
one Figure 2 adjustment. Without a callback it returns the adjustment request.
The live `workflow.py` integration below wires this control to the teammate module.

`llm_planner.make_planner(llm)` accepts an initialized existing Pneuma adapter.
It passes the supplied contract and conventions to the model without hardcoding
the HF query. Use `repair=lambda plan, error: planner(spec, plan, error)` with
`run_figure3`. Live planning reads the local `.env` through the existing Config
and adapter. Restart the kernel after changing configuration. Run directly with
`python experiments/figure3/llm_planner.py` or use the final notebook cell.
Figure 3 defaults to the shared `LLM_PATH` (local integration uses
`gpt-5.4-mini-2026-03-17`). Override it with `FIGURE3_LLM_MODEL` in local `.env`.
It records model, elapsed time, tokens and six synthetic checks. Five reuse the
generated SQL locally; the sixth calls the model to repair an injected SQL typo. Expected answers never enter prompts.
Provider failures are reported by exception type, without raw credentials.
Validate its answer on the same fixture before real-data integration.
The local runner disables external access; it is not a sandbox for arbitrary SQL
against a production connection. Real-data integration should use a read-only DB
connection and appropriate query limits.

The notebook saves `live_run_result.json` with generated SQL, attempts, output,
usage and checks. The reference planner is used only in offline sections.
Live success does not fall back to the reference planner. Eighteen offline tests
cover execution and bounded upstream adjustment. Real single-question MIMIC-III execution is now implemented below;
multi-question accuracy evaluation remains outstanding.

## Real database execution

Faith's preparation code and its tests/docs were copied unchanged from commit
1720f7671f0c7940a9a5927e045c682937e83ae9. Original CSVs are unchanged.
Build once with your own local source directory:

```sh
python scripts/prepare_mimic3.py inventory --mimic-dir /path/to/MIMIC_III-selected
python scripts/prepare_mimic3.py load --mimic-dir /path/to/MIMIC_III-selected --memory-limit 4GB --threads 4
python experiments/figure3/run_real.py
```

Or run the final notebook section (`RUN_REAL_DATABASE=True`). The database is
opened read-only with external access disabled. Schema is checked before calling
the model. This run uses the real local records, not the synthetic fixture.
The same delivered HF question is checked against an independent distinct-set
reference query. This one-question check is not a multi-question evaluation or
an implementation of the independent Figure 4 reviewer.

The full handoff is `local_results/real_run.json`: original question, IntentSpec,
QueryPlan, ExecutionResult, status, attempts, model, tokens, timestamp, database
path and reference check. Give the reviewer the primary artifacts and access to
the same local database. For blind validation, withhold `reference_check` from
the reviewer; it is an evaluation label, not reviewer input.

Database files, preparation reports and real result files are git-ignored.
Do not commit those local artifacts. Share code through Git; run the real-data
workflow in your team's authorized local environment.

For Figure 4, use `local_results/figure4_handoff.json`, automatically emitted
without the reference-check label. Keep `real_run.json` for local evaluation.
The initial real run matched the independent reference. The 21-table load had
zero rejected rows and matched the shared row-count fingerprint 55cb7586f72a;
matching row counts alone does not prove byte-for-byte data equivalence.

## Live Figure 2 integration (primary entry point)

```sh
python experiments/figure3/workflow.py
python experiments/figure3/workflow.py --question "Among hospital admissions with a heart failure diagnosis, what percentage ended in death during that admission?"
```

The teammate's IntentPreparer and Pydantic contracts are synchronized with
master commit c673ab4aa6a9f14d9fe32bec7cd991bddeb73fc8.
The planner receives query_conventions and supports contracts without a patients table.
`prepare(question, 'mimic_iii')` generates the full IntentSpec, then
`to_figure3_view(max_sample_rows=0)` feeds the model planner directly.
The old JSON contributes only the default question text, not schema or filters.
The local DBAPI facade uses the real read-only DuckDB connection for catalog and
code lookups. The LLM facade retains Figure 2 prompts with nonstreaming calls and
an 8000-token output cap. Both stages use the configured Figure 3 model in this
standalone integrated runner. Memory is off; UMLS follows local key availability.
No embedding index, Postgres server, or Conductor is required for this catalog path.

`needs_clarification` and `unanswerable` stop before SQL. A Figure 3 information
request invokes the same preparer's `prepare(..., adjustment_request=AdjustmentRequest(...))`
at most once, preserving its context. A second information request stops. An SQL
repair receives the latest spec. The full typed spec and adjustment history are
retained for review. The executor supports number/string/boolean scalars, single-column lists, and
multi-column tables, including grouped and empty results. ExecutionResult includes
schema, exact row_count, all rows inline, a JSON pointer result_reference, and the
first 100 rows as preview. Decimal table values are strings to preserve precision;
dates/times use ISO format. Full results currently materialize in memory, so large
result sets need a future streaming/file-backed implementation.

SQL parser, binder, catalog and parameter-count errors allow one internal repair.
The repair planner can raise InformationRequired when the supplied contract lacks
necessary information; this enters the same one-adjustment Figure 2 budget.
Repeated adjustment requests terminate with execution_failure and
failure_kind=upstream_adjustment_limit. Empty tables are successful outputs, not
an automatic reason to adjust upstream information.

**Current handoff:** `local_results/figure4_integrated_handoff.json`.
`integrated_run.json` contains the same trace. The separate
`integrated_execution_check.json` is a development check of SQL fidelity to the
newly generated spec, not independent confirmation of its clinical interpretation.
Never give this reference-check file to a blind result reviewer.

A live integration run succeeded, with no upstream adjustment needed. It selected
a broader HF code list and one decimal place, unlike the fixed JSON's 428% scope.
This is a real Figure 2 interpretation difference; Figure 3 preserves it for Figure
4 to assess rather than overriding it to force the old expected answer. Five new
offline tests cover typed handoff, non-ready states, actual AdjustmentRequest
construction, the adjustment limit, and refusal after adjustment. Live clarification
and adjustment branches have not been forced against the provider.


## Updated Figure 3 validation

The offline suite now contains 28 tests, including grouped/empty tables, full
results versus preview truncation, scalar types, date/decimal serialization,
and execution-time information adjustment with a strict one-retry limit.
Live validation uses `gpt-5.4-mini-2026-03-17`: the real integrated workflow,
six synthetic checks including SQL repair, and a grouped admissions query.
Local validation artifacts stay under ignored `local_results/`.
This is focused integration validation, not a multi-question accuracy benchmark.
Conditional query conventions apply only when their condition is met; a general
admissions question must not silently become an adult-only cohort.
