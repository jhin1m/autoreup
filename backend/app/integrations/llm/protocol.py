from enum import StrEnum

from pydantic import BaseModel, Field


class LLMFormat(StrEnum):
    responses = "responses"
    chat_completions = "chat_completions"
    anthropic_messages = "anthropic_messages"
    gemini_generate_content = "gemini_generate_content"


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMRequest(BaseModel):
    model: str
    system: str = ""
    messages: list[LLMMessage] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 4096
    stream: bool = False
    response_format: str = "text"
    reasoning_effort: str = "none"

    @classmethod
    def text(cls, system: str, user: str, model: str, max_tokens: int = 4096, stream: bool = False) -> "LLMRequest":
        return cls(model=model, system=system, messages=[LLMMessage(role="user", content=user)], max_tokens=max_tokens, stream=stream)


class LLMResponse(BaseModel):
    text: str
    raw: dict | None = None
    provider_format: LLMFormat


def normalize_format(provider: str) -> LLMFormat:
    value = provider.strip().lower().replace("-", "_")
    aliases = {
        "response": LLMFormat.responses,
        "responses": LLMFormat.responses,
        "openai_responses": LLMFormat.responses,
        "chat": LLMFormat.chat_completions,
        "chat_completion": LLMFormat.chat_completions,
        "chat_completions": LLMFormat.chat_completions,
        "openai_chat": LLMFormat.chat_completions,
        "message": LLMFormat.anthropic_messages,
        "messages": LLMFormat.anthropic_messages,
        "anthropic": LLMFormat.anthropic_messages,
        "anthropic_messages": LLMFormat.anthropic_messages,
        "gemini": LLMFormat.gemini_generate_content,
        "gemini_generate_content": LLMFormat.gemini_generate_content,
    }
    if value not in aliases:
        raise ValueError(f"Unsupported LLM provider format: {provider}")
    return aliases[value]
