"""Local command-line runner for Jira ticket analysis.

This file is used for local testing without starting the API server.
It accepts a Jira ticket metadata JSON file, selects a prompt template,
calls the analyzer service, and prints the model output as JSON.
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from app.config import settings
from app.llm_client import build_llm_client
from app.prompt_store import PromptStore
from app.ticket_analyzer import TicketAnalyzer


def load_ticket_json(path: str) -> Dict[str, Any]:
    ticket_path = Path(path)

    if not ticket_path.exists():
        raise FileNotFoundError(f"Ticket JSON file not found: {ticket_path}")

    with ticket_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("Ticket JSON input must be a JSON object.")

    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Jira ticket AI analysis locally.")
    parser.add_argument(
        "--ticket-json",
        required=True,
        help="Path to a Jira ticket metadata JSON file.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Prompt name from the Prompt folder, without .txt.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    analyzer = TicketAnalyzer(
        settings=settings,
        prompt_store=PromptStore(settings.prompt_dir),
        llm_client=build_llm_client(settings),
    )

    result = analyzer.analyze(
        ticket_data=load_ticket_json(args.ticket_json),
        prompt_name=args.prompt,
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
