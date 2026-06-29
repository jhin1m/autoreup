"""Thin LLM access layer for the dubbing stages.

Wraps the provider-neutral ``backend.app.integrations.llm`` client with a typed
config, a retry loop, and the loose-TSV parsing every stage relies on. Prompts
live in :mod:`backend.app.dubbing.prompts`; nothing here is genre-specific.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

from backend.app.integrations.llm.client import (
    DEFAULT_RESPONSES_BASE_URL,
    default_api_key,
    generate_text,
)
from backend.app.integrations.llm.protocol import LLMFormat, normalize_format


@dataclass(slots=True)
class LLMConfig:
    provider: str = "responses"
    base_url: str | None = DEFAULT_RESPONSES_BASE_URL
    model: str = "gpt-5.4-mini"
    api_key: str = ""
    max_tokens: int = 4096
    stream: bool = False
    timeout: float = 180.0
    retries: int = 3

    def with_model(self, model: str) -> "LLMConfig":
        return replace(self, model=model)

    def resolved_base_url(self) -> str | None:
        # Anthropic/Gemini wire formats must not be pointed at the local Responses URL.
        if (
            normalize_format(self.provider)
            in {LLMFormat.anthropic_messages, LLMFormat.gemini_generate_content}
            and self.base_url == DEFAULT_RESPONSES_BASE_URL
        ):
            return None
        return self.base_url

    def resolved_api_key(self) -> str:
        return self.api_key or default_api_key(self.provider)


class LLMError(RuntimeError):
    pass


def complete(system: str, user: str, config: LLMConfig) -> str:
    """Call the model with retries; raise LLMError after the last attempt."""
    last_error: Exception | None = None
    for attempt in range(max(1, config.retries)):
        try:
            return generate_text(
                system=system,
                user=user,
                model=config.model,
                provider=config.provider,
                api_key=config.resolved_api_key(),
                base_url=config.resolved_base_url(),
                max_tokens=config.max_tokens,
                stream=config.stream,
                timeout=config.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - provider clients raise various types
            last_error = exc
            if attempt + 1 < config.retries:
                time.sleep(0.5 * (attempt + 1))
    raise LLMError(f"LLM call failed after {config.retries} attempts: {last_error}")


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def parse_tsv_rows(text: str, expected_cols: int) -> list[list[str]]:
    """Parse loose model output into rows of exactly ``expected_cols`` columns.

    Strips code fences, ignores blank/markdown lines, and splits on tabs (falling
    back to runs of 2+ spaces). Rows with too few columns are skipped; extra
    columns are joined into the last field.
    """
    cleaned = _FENCE_RE.sub("", text.strip())
    rows: list[list[str]] = []
    for raw in cleaned.splitlines():
        line = raw.rstrip("\r\n")  # keep trailing tabs so empty final columns survive
        if not line.strip() or line.lstrip().startswith(("#", "//", "|---", "```")):
            continue
        cells = line.split("\t") if "\t" in line else re.split(r"\s{2,}", line.strip())
        cells = [cell.strip() for cell in cells]
        if len(cells) < expected_cols:
            continue
        if len(cells) > expected_cols:
            cells = cells[: expected_cols - 1] + ["\t".join(cells[expected_cols - 1 :])]
        rows.append(cells)
    return rows
