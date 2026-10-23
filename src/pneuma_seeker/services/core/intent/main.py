# src/pneuma_seeker/services/core/intent/main.py
"""
Figure 2: Task Interpretation and Schema Lookup.

A fixed pipeline (no LLM-chosen actions) that turns a user request into an
IntentSpec, a ClarificationNeeded, or an Unanswerable:

    S1 memory -> S2 interpret (LLM) -> S3 retrieve tables / partitions / joins
    -> S4 ground concepts to columns (LLM) -> S5 validate -> S6 decide

It reuses the existing retriever, enumerator, join-path discovery, and memory
layer, and is not wired into Conductor.

Path taken on MIMIC-III (dataset "mimic_iii", profile MIMIC_III: private + catalog):

    prepare()
     |- privacy guard           refuses to run unless allow_private_llm=True
     |- S1 memory               only if ENABLE_MEMORY_LAYER
     |- S2 interpret (LLM)      concepts with code_system + lookup_terms; search queries
     |- S3 _retrieve_catalog    every loaded table; public column docs (_with_docs);
     |                          no sample rows; joins on icustay_id / hadm_id / subject_id
     |- S3b code_lookup         UMLS (ICD9CM, LNC, RXNORM) + d_icd_* / d_items / d_labitems,
     |                          or free text (org_name, ab_name, ...); counts never sent
     |- S4 ground (LLM)         schema + domain notes + "not loaded" notes + code candidates
     |- S5 validate             names, then filter values and codes; one repair round (LLM)
     '- S6 decide               IntentSpec | ClarificationNeeded | Unanswerable

Not used on MIMIC-III: _retrieve_by_search (hybrid index, partition families,
value-overlap join discovery). Datasets without a MIMIC profile take that path.

MIMIC datasets get a domain profile (mimic_profile). For credentialed data
(MIMIC-III, profile.private) the LLM prompts carry only the question, the
public schema documentation, and dictionary entries: no sample rows and no
usage counts; and prepare() refuses to run unless allow_private_llm=True,
which should be set only for an LLM endpoint approved for the data.
"""
import re
from dataclasses import dataclass, field
from logging import Logger
from time import time
from typing import Any

from pneuma_seeker.services.core.action_set.impl.join_path_extraction import (
    JoinPathExtraction,
)
from pneuma_seeker.services.core.api.db import DBAPI
from pneuma_seeker.services.core.api.language_model import LanguageModelAPI
from pneuma_seeker.services.core.intent.code_lookup import (
    CodeCandidates,
    CodeLookup,
    format_candidates,
    refusal_title_issues,
)
from pneuma_seeker.services.core.intent.mimic_profile import MimicProfile, get_profile
from pneuma_seeker.services.core.intent.prompt_factory import IntentPromptFactory
from pneuma_seeker.services.core.intent.umls import UMLSClient
from pneuma_seeker.services.core.intent.validator import (
    decide,
    find_partition_families,
    parse_join_paths,
    repairable_issues,
    to_table_info,
    unsupported_refusal,
    validate_grounding,
)
from pneuma_seeker.services.core.ir_system.main import Retriever
from pneuma_seeker.shared.config import Config
from pneuma_seeker.shared.logger import formatted_log
from pneuma_seeker.shared.parser import parse_json
from pneuma_seeker.shared.schemas.core.intent_spec import (
    AdjustmentRequest,
    Figure2Result,
    JoinPath,
    ColumnInfo,
    IntentSpec,
    TableInfo,
)
from pneuma_seeker.shared.schemas.core.ir_system import AbstractDocument, RetrieverType
from pneuma_seeker.shared.schemas.language_model.message import LLMMessage
from pneuma_seeker.shared.schemas.language_model.option import LLMOption
from pneuma_seeker.shared.schemas.language_model.role import Role
from pneuma_seeker.shared.str_processor import clean_column_table_name


@dataclass
class PreparationContext:
    """Intermediate state of one prepare() call, kept for debugging, evaluation, and adjustment."""

    question: str
    dataset_name: str
    profile: MimicProfile | None = None
    available_tables: set[str] = field(default_factory=set)
    memory_entries: list[dict[str, Any]] = field(default_factory=list)
    interpretation: dict[str, Any] = field(default_factory=dict)
    search_queries: list[str] = field(default_factory=list)
    documents: dict[str, AbstractDocument] = field(default_factory=dict)
    tables: list[TableInfo] = field(default_factory=list)
    families: dict[str, list[str]] = field(default_factory=dict)
    join_hints: str = ""
    join_paths: list[JoinPath] = field(default_factory=list)
    grounding: dict[str, Any] = field(default_factory=dict)
    code_candidates: list[CodeCandidates] = field(default_factory=list)
    repair_issues: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


class IntentPreparer:
    def __init__(
        self,
        user_id: str,
        chat_id: str,
        config: Config,
        logger: Logger,
        db_api: DBAPI,
        language_model_api: LanguageModelAPI,
        retrieval_k: int = 10,
        sample_size: int = 3,
        max_partition_tables: int = 60,
        clarification_threshold: float = 0.7,
        umls: UMLSClient | None = None,
        allow_private_llm: bool = False,
    ):
        self.user_id = user_id
        self.chat_id = chat_id
        self.config = config
        self.logger = logger
        self.db_api = db_api
        self.language_model_api = language_model_api
        self.retrieval_k = retrieval_k
        self.sample_size = sample_size
        self.max_partition_tables = max_partition_tables
        self.clarification_threshold = clarification_threshold
        self.allow_private_llm = allow_private_llm
        self._snapshot_checked: set[str] = set()

        self.retriever = Retriever(user_id, chat_id, config, logger, db_api, language_model_api)
        self.join_path_extraction = JoinPathExtraction(
            user_id, chat_id, config, logger, db_api, language_model_api
        )
        self.prompt_factory = IntentPromptFactory()
        self.umls = umls if umls is not None else UMLSClient(logger)
        self.code_lookup = CodeLookup(user_id, chat_id, logger, db_api, self.umls)
        self.last_context: PreparationContext | None = None

    def prepare(
        self,
        question: str,
        dataset_name: str,
        history: list[LLMMessage] | None = None,
        memory_group_ids: list[str] | None = None,
        use_memory: bool = True,
        interactive: bool = False,
        adjustment_request: AdjustmentRequest | None = None,
    ) -> Figure2Result:
        """
        Runs the Figure 2 pipeline. With `adjustment_request`, reuses the previous
        context for the same question and widens the search before re-grounding.
        """
        question = question.strip()
        if not question:
            raise ValueError("No question provided.")
        history = history or []
        profile = get_profile(dataset_name)
        if profile is not None and profile.private and not self.allow_private_llm:
            raise PermissionError(
                f"{dataset_name} is credentialed {profile.name} data. Figure 2 sends the question, schema "
                "documentation, and dictionary entries to the configured LLM; set allow_private_llm=True only "
                "for an LLM endpoint approved under the PhysioNet data use agreement."
            )

        previous = self.last_context
        reuse = (
            adjustment_request is not None
            and previous is not None
            and previous.question == question
            and previous.dataset_name == dataset_name
        )
        if profile is not None:
            self._check_snapshot(dataset_name)
        context = PreparationContext(question=question, dataset_name=dataset_name, profile=profile)
        context.available_tables = set(self._table_layouts(dataset_name)) if profile else set()

        # S1: memory
        start = time()
        if reuse:
            context.memory_entries = previous.memory_entries
        elif use_memory and self.config.ENABLE_MEMORY_LAYER:
            try:
                context.memory_entries = self.db_api.retrieve_memory_context(
                    self.user_id,
                    dataset_name,
                    memory_group_ids or [],
                    question,
                    self.config.MEMORY_RETRIEVAL_TOP_K,
                )
            except Exception as exc:
                self.__log(f"Memory retrieval failed, continuing without it: {exc}")
        context.timings["memory"] = time() - start

        # S2: interpret
        start = time()
        if reuse:
            context.interpretation = previous.interpretation
        else:
            context.interpretation = self._chat_json(
                self.prompt_factory.get_interpretation_sys_prompt(
                    {name: system.description for name, system in profile.code_systems.items()}
                    if profile else None
                ),
                self.prompt_factory.get_interpretation_user_prompt(
                    question, history, context.memory_entries
                ),
            )
        context.timings["interpret"] = time() - start

        # S3: retrieve
        start = time()
        context.search_queries = self._search_queries(question, context.interpretation, adjustment_request)
        k = self.retrieval_k * 2 if reuse else self.retrieval_k
        context.documents = dict(previous.documents) if reuse else {}
        self._retrieve(context, k)
        context.timings["retrieve"] = time() - start

        # S3b: code grounding (MIMIC): codes that actually occur, from dictionaries + UMLS
        start = time()
        if profile:
            context.code_candidates = self.code_lookup.lookup(
                dataset_name, profile, context.interpretation.get("concepts") or [], context.available_tables,
                question=question,
            )
        context.timings["codes"] = time() - start

        # S4: ground
        start = time()
        messages = [
            LLMMessage(role=Role.SYSTEM.value, content=self.prompt_factory.get_grounding_sys_prompt()),
            LLMMessage(
                role=Role.USER.value,
                content=self.prompt_factory.get_grounding_user_prompt(
                    question,
                    context.interpretation,
                    context.tables,
                    context.families,
                    context.join_hints,
                    context.memory_entries,
                    adjustment_request,
                    domain_notes=self._domain_notes(context),
                    code_candidates=format_candidates(context.code_candidates, show_counts=not profile.private)
                    if profile else "",
                ),
            ),
        ]
        context.grounding, messages = self._converse_json(messages)
        context.timings["ground"] = time() - start

        # S5: validate; on failure, one repair round back to S4
        start = time()
        issues = self._validation_issues(context)
        if issues:
            context.repair_issues = issues
            self.__log(f"Repairing grounding: {issues}")
            messages.append(LLMMessage(role=Role.USER.value, content=self.prompt_factory.get_repair_prompt(issues)))
            repaired, messages = self._converse_json(messages)
            if repaired:
                context.grounding = repaired
        value_issues = []
        if profile:  # Remaining issues become notes in the spec, plus advisory UMLS coverage notes
            concepts, _, _, _ = validate_grounding(context.grounding, context.tables)
            value_issues = self._value_issues(context) + self.code_lookup.umls_coverage_notes(
                profile, concepts, context.code_candidates
            )
        context.timings["validate"] = time() - start

        # S6: decide
        self.last_context = context
        result = decide(
            question,
            dataset_name,
            context.interpretation,
            context.grounding,
            context.tables,
            context.join_paths,
            [entry["memory_id"] for entry in context.memory_entries if "memory_id" in entry],
            interactive,
            self.clarification_threshold,
            value_issues=value_issues,
        )
        if profile is not None and isinstance(result, IntentSpec):
            # Figure 3 follows the Figure 1 conventions that apply to the tables it will query
            result = result.model_copy(update={"query_conventions": profile.conventions_for(result.required_tables)})
        self.__log(f"Result: {result.status} (timings: { {k: round(v, 1) for k, v in context.timings.items()} })")
        return result

    # ------------------------------------------------------------------
    # S3 helpers
    # ------------------------------------------------------------------
    def _search_queries(
        self,
        question: str,
        interpretation: dict[str, Any],
        adjustment_request: AdjustmentRequest | None,
    ) -> list[str]:
        """The raw question plus the interpreter's queries (and missing concepts on adjustment)."""
        queries = [question]
        queries += [q for q in interpretation.get("search_queries") or [] if isinstance(q, str)]
        if adjustment_request is not None:
            queries += adjustment_request.missing_concepts
        return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))

    def _sample_size(self, context: PreparationContext) -> int:
        return 0 if context.profile is not None and context.profile.private else self.sample_size

    def _retrieve(self, context: PreparationContext, k: int) -> None:
        """S3: picks one of two paths (see the module docstring)."""
        if context.profile is not None and context.profile.catalog:
            self._retrieve_catalog(context)  # MIMIC-III
        else:
            self._retrieve_by_search(context, k)  # Datasets without a catalog profile

    # --- Catalog path (MIMIC-III) ---------------------------------------
    def _retrieve_catalog(self, context: PreparationContext) -> None:
        """A small documented schema: every table is a candidate, so no index is needed."""
        try:
            for doc in self.retriever.retrieve_documents(
                RetrieverType.ENUMERATOR, ".*", context.dataset_name,
                len(context.available_tables) or 100, True, 1,
            ):
                context.documents.setdefault(doc.doc_id, doc)
        except Exception as exc:
            self.__log(f"Table enumeration failed: {exc}")
        self._finish_mimic_tables(context)

    # --- Search path (datasets without a MIMIC profile) ------------------
    # Not used on MIMIC-III: the hybrid index, partition families (e.g. one table
    # per year or state in legal), and value-overlap join discovery.
    def _retrieve_by_search(self, context: PreparationContext, k: int) -> None:
        """Hybrid retrieval per query, partition expansion, then join-path hints."""
        for query in context.search_queries:
            try:
                docs = self.retriever.retrieve_documents(
                    RetrieverType.PNEUMA_RETRIEVER,
                    query,
                    context.dataset_name,
                    k,
                    True,
                    self.sample_size,
                )
            except Exception as exc:
                self.__log(f"Retrieval failed for '{query}': {exc}")
                continue
            for doc in docs:
                context.documents.setdefault(doc.doc_id, doc)

        self._expand_partitions(context)

        context.tables = [to_table_info(doc, self.sample_size) for doc in context.documents.values()]
        self._discover_joins(context)

    def _expand_partitions(self, context: PreparationContext) -> None:
        """Pulls in every same-layout sibling of a retrieved table, since top-k
        retrieval cannot return all of e.g. 22 yearly tables."""
        context.families = find_partition_families(
            list(context.documents), self._table_layouts(context.dataset_name)
        )
        for label, members in context.families.items():
            missing = [name for name in members if name not in context.documents]
            if not missing:
                continue
            if len(members) > self.max_partition_tables:
                self.__log(f"Skipping partition family '{label}': {len(members)} tables")
                continue
            pattern = "^(" + "|".join(re.escape(name) for name in missing) + ")$"
            try:
                siblings = self.retriever.retrieve_documents(
                    RetrieverType.ENUMERATOR,
                    pattern,
                    context.dataset_name,
                    len(missing),
                    True,
                    self.sample_size,
                )
            except Exception as exc:
                self.__log(f"Partition enumeration failed for '{label}': {exc}")
                continue
            for doc in siblings:
                # Enumerated tables lack retriever metadata such as column descriptions
                context.documents.setdefault(doc.doc_id, doc)

    def _discover_joins(self, context: PreparationContext) -> None:
        """Value-overlap join discovery; pairwise, so only the first member of a partition family is used."""
        representatives = [doc for name, doc in context.documents.items() if not any(
            name in members[1:] for members in context.families.values()
        )]
        try:
            context.join_hints = self.join_path_extraction.discover_join_paths(
                {doc.doc_id: doc.content for doc in representatives}
            )
            context.join_paths = parse_join_paths(context.join_hints)
        except Exception as exc:
            self.__log(f"Join path discovery failed: {exc}")

    # --- MIMIC helpers ---------------------------------------------------
    def _finish_mimic_tables(self, context: PreparationContext) -> None:
        """Schema evidence with documented descriptions, then joins on documented MIMIC keys."""
        profile = context.profile
        context.tables = [
            _with_docs(to_table_info(doc, self._sample_size(context)), profile)
            for doc in context.documents.values()
        ]
        # Join on documented MIMIC keys rather than guessing from value overlap.
        columns_by_table = {t.name: {c.name for c in t.columns} for t in context.tables}
        context.join_paths = profile.key_join_paths(columns_by_table, [t.name for t in context.tables])
        context.join_hints = "\n".join(
            f"- {p.left_table}.{p.left_column} = {p.right_table}.{p.right_column}" for p in context.join_paths
        )

    def _domain_notes(self, context: PreparationContext) -> str:
        if context.profile is None:
            return ""
        notes = [context.profile.domain_notes,
                 context.profile.conventions_notes(context.available_tables),
                 context.profile.unavailable_notes(context.available_tables)]
        return "\n\n".join(note for note in notes if note)

    def _check_snapshot(self, dataset_name: str) -> None:
        """Warns once per dataset if the local data differ from Figure 1's shared snapshot."""
        if dataset_name in self._snapshot_checked:
            return
        self._snapshot_checked.add(dataset_name)
        try:
            from pneuma_seeker.services.core.data_prep.paths import dataset_db_path
            from pneuma_seeker.services.core.data_prep.snapshot import compare_with_snapshot
            result = compare_with_snapshot(dataset_db_path())
        except Exception as exc:  # Figure 1 module or snapshot not available
            self.__log(f"Snapshot check skipped: {exc}")
            return
        if result.status != "match":
            self.logger.warning(result.message())
        else:
            self.__log(result.message())

    def _table_layouts(self, dataset_name: str) -> dict[str, tuple[str, ...]]:
        """Ordered column names of every table in the dataset (one metadata query)."""
        try:
            self.db_api.link_dataset_tables(self.user_id, self.chat_id, dataset_name)
            columns = self.db_api.execute_query(
                self.user_id,
                self.chat_id,
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = ? OR table_catalog = ?
                ORDER BY table_name, ordinal_position
                """.strip(),
                (dataset_name, dataset_name),
            )
        except Exception as exc:
            self.__log(f"Could not read table layouts: {exc}")
            return {}
        layouts: dict[str, list[str]] = {}
        for table, column in zip(columns["table_name"], columns["column_name"]):
            layouts.setdefault(clean_column_table_name(str(table)), []).append(
                clean_column_table_name(str(column))
            )
        return {table: tuple(cols) for table, cols in layouts.items()}

    # ------------------------------------------------------------------
    # S5 helpers
    # ------------------------------------------------------------------
    def _validation_issues(self, context: PreparationContext) -> list[str]:
        issues = repairable_issues(context.grounding, context.tables)
        issues = issues or unsupported_refusal(context.grounding, context.tables)
        issues = issues or refusal_title_issues(context.grounding, context.code_candidates)
        if context.profile is not None and not issues:
            issues = self._value_issues(context)
        return issues

    def _value_issues(self, context: PreparationContext) -> list[str]:
        concepts, _, _, _ = validate_grounding(context.grounding, context.tables)
        return self.code_lookup.verify_filter_values(context.dataset_name, context.profile, concepts) + \
            self.code_lookup.check_against_candidates(context.profile, concepts, context.code_candidates)

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------
    def _chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """One JSON-mode call, with a single retry when the output does not parse."""
        parsed, _ = self._converse_json([
            LLMMessage(role=Role.SYSTEM.value, content=system_prompt),
            LLMMessage(role=Role.USER.value, content=user_prompt),
        ])
        return parsed

    def _converse_json(self, messages: list[LLMMessage]) -> tuple[dict[str, Any], list[LLMMessage]]:
        """Continues a conversation in JSON mode; returns the parsed object and the extended messages."""
        messages = list(messages)
        response = ""
        for attempt in range(2):
            response = "".join(
                self.language_model_api.chat(messages, LLMOption(json_mode=True, stream=True))
            )
            messages.append(LLMMessage(role=Role.ASSISTANT.value, content=response))
            try:
                parsed = parse_json(response)
                if isinstance(parsed, dict):
                    return parsed, messages
                error = "the output is not a JSON object"
            except ValueError as exc:
                error = str(exc)
            self.__log(f"JSON parse failed (attempt {attempt + 1}): {error}")
            messages.append(
                LLMMessage(role=Role.USER.value, content=self.prompt_factory.get_json_retry_prompt(error))
            )
        return {}, messages

    def __log(self, message: str) -> None:
        formatted_log(self.logger, "IntentPreparer", message)


def _with_docs(table: TableInfo, profile: MimicProfile) -> TableInfo:
    """Fills missing table and column descriptions from the profile's public documentation."""
    docs = profile.table_docs.get(table.name)
    if docs is None:
        return table
    description, column_docs = docs
    return table.model_copy(update={
        "description": table.description or description,
        "columns": [
            column if column.description else ColumnInfo(
                name=column.name, type=column.type, description=column_docs.get(column.name, "")
            )
            for column in table.columns
        ],
    })
