"""Prompt template loading and rendering utilities.

This file reads stable system prompt templates from the Prompt folder and
lists available prompts. Ticket JSON is sent separately as a user message,
so prompts should contain instructions only.
"""

import logging
from pathlib import Path
from typing import List

from app.exceptions import PromptNotFoundError

log = logging.getLogger(__name__)


class PromptStore:
    def __init__(self, prompt_dir: str) -> None:
        self.prompt_dir = Path(prompt_dir)

    def list_prompts(self) -> List[str]:
        if not self.prompt_dir.exists():
            log.warning("Prompt directory does not exist: %s", self.prompt_dir)
            return []

        prompts = sorted(path.stem for path in self.prompt_dir.glob("*.txt"))
        log.debug("Available prompts: %s", prompts)
        return prompts

    def load(self, prompt_name: str) -> str:
        safe_name = Path(prompt_name).stem
        prompt_path = self.prompt_dir / f"{safe_name}.txt"

        if not prompt_path.exists():
            log.error("Prompt '%s' not found in %s", safe_name, self.prompt_dir)
            raise PromptNotFoundError(f"Prompt '{safe_name}' was not found in {self.prompt_dir}")

        log.info("Loading prompt '%s' from %s", safe_name, prompt_path)
        return prompt_path.read_text(encoding="utf-8")
