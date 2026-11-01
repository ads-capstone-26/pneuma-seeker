# scripts/run_intent.py
"""
Runs Figure 2 (IntentPreparer) on one benchmark question or a free-form question
and prints the result plus the intermediate context.

Usage (from the project root, with the pneuma_seeker env active):
    python scripts/run_intent.py --id mimic3-medium-3 --allow-private-llm
    python scripts/run_intent.py --dataset mimic_iii --allow-private-llm --question "How many ICU stays are recorded?"

--allow-private-llm is required for credentialed MIMIC-III: the prompts then carry
only the question, public schema documentation, and dictionary entries.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pneuma_seeker.services.core.api.db import DBAPI
from pneuma_seeker.services.core.api.language_model import LanguageModelAPI
from pneuma_seeker.services.core.intent.main import IntentPreparer
from pneuma_seeker.services.core.intent.umls import UMLSClient
from pneuma_seeker.shared.config import Config
from pneuma_seeker.shared.logger import setup_logger
from pneuma_seeker.shared.schemas.core.ir_system import RetrieverType

INDEX_DIR = (
    PROJECT_ROOT / "src" / "pneuma_seeker" / "services" / "core" / "ir_system"
    / "retriever" / "impl" / "indices" / "pneuma"
)
OUTPUT_DIR = PROJECT_ROOT / "eval_results"


def build_preparer(chat_id: str, allow_private_llm: bool = False) -> IntentPreparer:
    """Creates an IntentPreparer that queries a copy of the indices (Chroma rewrites them)."""
    config = Config(str(PROJECT_ROOT / ".env"))
    logger = setup_logger("Figure 2")
    OUTPUT_DIR.mkdir(exist_ok=True)
    db_api = DBAPI(config, logger, workspace_db_path=str(OUTPUT_DIR / "workspaces"))
    language_model_api = LanguageModelAPI(config, logger)
    # The OpenAI SDK allows 5 s to connect; slow TLS handshakes need more headroom.
    for model in (language_model_api.llm, language_model_api.embed_model):
        client = getattr(model, "client", None)
        if hasattr(client, "with_options"):
            model.client = client.with_options(
                timeout=httpx.Timeout(600, connect=30), max_retries=3
            )
    # UMLS content is licensed: cache it under the git-ignored eval_results/.
    umls = UMLSClient(logger, cache_path=str(OUTPUT_DIR / "umls_cache.json"))
    logger.info(f"UMLS lookups {'enabled' if umls.enabled else 'disabled (no UMLS_API_KEY)'}")
    preparer = IntentPreparer(
        "eval_user", chat_id, config, logger, db_api, language_model_api, umls=umls,
        allow_private_llm=allow_private_llm,
    )
    index_copy = OUTPUT_DIR / "indices"
    for index in INDEX_DIR.iterdir():  # Copy indices built after the first run, too
        if index.is_dir() and not (index_copy / index.name).exists():
            shutil.copytree(index, index_copy / index.name)
    preparer.retriever.retriever_factory.get_retriever(
        RetrieverType.PNEUMA_RETRIEVER
    ).index_path = str(index_copy)
    return preparer


def find_question(question_id: str) -> dict:
    prefix = question_id.split("-")[0]
    dataset = {"mimic3": "mimic_iii"}.get(prefix, prefix)
    questions = json.loads((PROJECT_ROOT / "data_src" / dataset / f"{dataset}_tabular.json").read_text())
    for question in questions:
        if question["id"] == question_id:
            return {**question, "dataset": dataset}
    raise SystemExit(f"Question id not found: {question_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="Question id, e.g. mimic3-easy-1")
    parser.add_argument("--dataset", help="Dataset name for a free-form question")
    parser.add_argument("--question", help="Free-form question")
    parser.add_argument("--interactive", action="store_true", help="Allow ClarificationNeeded")
    parser.add_argument("--allow-private-llm", action="store_true",
                        help="Allow credentialed datasets (MIMIC-III): only with an LLM endpoint approved under the PhysioNet DUA")
    args = parser.parse_args()

    if args.id:
        item = find_question(args.id)
        question, dataset = item["query"], item["dataset"]
    elif args.dataset and args.question:
        item, question, dataset = None, args.question, args.dataset
    else:
        raise SystemExit("Pass --id, or --dataset with --question.")

    preparer = build_preparer(f"run_intent_{datetime.now():%Y%m%d_%H%M%S}", args.allow_private_llm)
    llm = preparer.language_model_api.llm
    result = preparer.prepare(question, dataset, interactive=args.interactive)
    context = preparer.last_context

    print("\n=== Question ===\n" + question)
    print("\n=== S2 interpretation ===\n" + json.dumps(context.interpretation, ensure_ascii=False, indent=1))
    print("\n=== S3 search queries ===\n" + "\n".join(f"- {q}" for q in context.search_queries))
    print(f"\n=== S3 candidate tables ({len(context.tables)}) ===")
    print(", ".join(table.name for table in context.tables))
    if context.families:
        print("Partition families: " + json.dumps({k: len(v) for k, v in context.families.items()}))
    print(f"\n=== Result: {result.status} ===")
    print(result.model_dump_json(indent=1, exclude={"tables"}))
    if item is not None:
        print("\n=== Gold ===")
        print("data_sources:", item["data_sources"], "| answer:", item["answer"])
    timings = {k: round(v, 1) for k, v in context.timings.items()}
    print(f"\nTimings (s): {timings} | tokens in/out: {llm.total_input_tokens}/{llm.total_output_tokens}")


if __name__ == "__main__":
    main()
