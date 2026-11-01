# Proposal: structured time filters in the IntentSpec

*Draft for discussion between Figure 2 (Ziming) and Figure 3 (Xiaofeng). Nothing is implemented yet.*

## Why

Most real EHR questions involve time. EHRSQL reports that 93.2% of its queries use at least one time column (Lee et al., 2022, Table 4). Today the IntentSpec stores time in a free-form `temporal_constraints` dict, which means Figure 3 has to guess what "last year" or "within 2 months after" means. EHRSQL breaks time into three filter types, each mapped to an exact SQL pattern (Lee et al., 2022, Section 3.1.2 and Supplementary B.2, Table 8). Adopting the same structure has three benefits:

- Figure 3 can build the SQL condition mechanically instead of interpreting free text.
- Our answers follow the same semantics as EHRSQL's reference SQL, so end-to-end scores are meaningful.
- Figure 2 can detect, by rule, questions that MIMIC-III cannot answer, such as ordering two diagnoses within one admission.

## Proposed fields

```json
"reference_time": "current_time",
"time_filters": {
  "global": [
    {"applies_to": "admissions.admittime", "expression": "relative", "unit": "year",
     "interval": "in", "offset": -1}
  ],
  "within": [
    {"first": "diagnosis: <concept term>", "second": "procedure: <concept term>",
     "order": "after", "unit": "month", "amount": 2,
     "first_time": "admissions.admittime", "second_time": "admissions.admittime"}
  ],
  "exact": [
    {"applies_to": "microbiologyevents.charttime", "option": "last"}
  ]
},
"patient_scope": "individual",
"unanswerable_type": null
```

`temporal_constraints` is kept during the transition and mirrors a plain-text summary of `time_filters`.

### `global`: the overall time window of the events asked about

| Field | Values | Meaning |
|---|---|---|
| `applies_to` | `table.column` | Time column the window is applied to |
| `expression` | `absolute` / `relative` / `mixed` | "in 2104" / "last year" / "in 05/this year" |
| `unit` | `year` / `month` / `day` / `hospital_visit` / `icu_visit` | Granularity |
| `interval` | `in` / `since` / `until` | Equal to / on or after / on or before |
| `value` | e.g. `"2104"`, `"2104-05"`, `"2104-05-03"` | Absolute dates only |
| `offset` | integer ≤ 0 | Relative calendar units: `-1` = last year/month/day, `0` = this one |
| `amount` | positive integer | Rolling windows: "since 2 years ago" → `amount: 2`, `interval: since` |
| `option` | `first` / `last` / `current` | For `hospital_visit` / `icu_visit` units |

The semantics below follow EHRSQL's Table 8, with `current_time` as the reference time:

| Example phrase | Encoding | SQL pattern (EHRSQL, SQLite) |
|---|---|---|
| last year | `relative, year, in, offset -1` | `datetime(col,'start of year') = datetime(current_time,'start of year','-1 year')` (calendar year) |
| since 2 years ago | `relative, year, since, amount 2` | `datetime(col) >= datetime(current_time,'-2 year')` (rolling window) |
| until last month | `relative, month, until, offset -1` | `datetime(col,'start of month') <= datetime(current_time,'start of month','-1 month')` |
| in 05/2104 | `absolute, month, in, value "2104-05"` | `strftime('%Y-%m',col) = '2104-05'` |
| on the first hospital visit | `relative, hospital_visit, in, option first` | `dischtime IS NOT NULL ORDER BY admittime ASC LIMIT 1` |
| on the current hospital visit | `relative, hospital_visit, in, option current` | see "MIMIC-III differences" below |

Note the asymmetry, which is easy to get wrong: "last year" is a **calendar** year, whereas "since N years ago" is a **rolling** window.

### `within`: the relation between two events

| Field | Values | Meaning |
|---|---|---|
| `first`, `second` | concept terms (as in `concepts`) | The two events, in time order |
| `order` | `after` / `any` | Whether `second` must follow `first` |
| `unit` | `hospital_visit` / `icu_visit` / `year` / `month` / `day` | "within the same ..." or "within N ..." |
| `amount` | integer or null | null = "the same <unit>"; N = "within N <unit>s" |
| `first_time`, `second_time` | `table.column` | Time columns used to compare the two events |

EHRSQL patterns: "the same hospital visit" means equal `hadm_id`; "within 2 months" means `second_time BETWEEN first_time AND first_time + 2 months`; "the same month" means equal `start of month`.

### `exact`: one specific event

`option`: `first` / `last` / `at` (with `value`). "first" means `ORDER BY time ASC LIMIT 1`. Ties follow EHRSQL v1.5, which returns all rows at the extreme time.

### `patient_scope`

`none` (e.g. "What is the intake method of drug X?"), `individual` ("patient 1234"), or `group` ("How many patients..."). These are EHRSQL's three scopes (Section 3.1.1).

### `unanswerable_type`

Set only when the status is unanswerable:

- `external_knowledge`: the question needs knowledge that is not in the database, such as side effects or recommendations;
- `beyond_schema`: the data are not recorded, such as future appointments or contact details;
- `missing_table`: the data exist in MIMIC-III but not in our copy;
- `no_event_time`: the question needs the order of events whose time MIMIC-III does not record.

The first two are EHRSQL's categories (Section 3.1.1 and Table 1); the last two are specific to our data.

## MIMIC-III differences (needs a team decision)

EHRSQL shifted all admissions into 2100–2105, set the current time to **2105-12-31 23:59:00**, and removed every record after it (Section 3.2). We use unmodified MIMIC-III v1.4, whose shifted dates run past 2105. To keep the semantics consistent with EHRSQL:

1. **`current_time` = `2105-12-31 23:59:00`**, used by Figure 3 at execution time and when recomputing the gold answers.
2. **Records after `current_time` are treated as not yet existing**, mirroring EHRSQL's removal. Figure 3 adds `time_col <= current_time` on the tables a query uses, and gold recomputation does the same.
3. **"Current hospital visit"** becomes `admittime <= current_time AND (dischtime IS NULL OR dischtime > current_time)`. On EHRSQL's database this is equivalent to its `dischtime IS NULL`; on ours it is the correct generalization.
4. **Diagnoses and procedures have no time** in MIMIC-III; EHRSQL added a `charttime` to them. Figure 2 dates them by their admission (`admissions.admittime`). A `within` filter with `order: after` between two diagnoses or procedures in the same hospital visit is unanswerable (`no_event_time`). Unordered co-occurrence in the same visit (`order: any`) remains answerable.

## Questions for Figure 3

1. Is this structure easy to translate into SQL on your side? Would you prefer anything simpler, for example only `global` and `within` at first?
2. Do you agree with `current_time` and points 2–3 above?
3. Should Figure 2 also emit a ready-made SQL condition string per filter, or do you prefer to build the SQL yourself from the fields?
4. Comparison operators: while we are changing the contract, should each concept also get an explicit `operator` (`=`, `>`, `>=`, `IN`, `LIKE`)?
