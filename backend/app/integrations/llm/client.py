import json
import os
from typing import Any

import httpx

from .protocol import LLMFormat, LLMRequest, LLMResponse, normalize_format


DEFAULT_RESPONSES_BASE_URL = "http://localhost:8090/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def default_api_key(provider: str) -> str:
    fmt = normalize_format(provider)
    if fmt in {LLMFormat.responses, LLMFormat.chat_completions}:
        return os.environ.get("OPENAI_API_KEY") or os.environ.get("LOCAL_API_KEY") or "local"
    if fmt == LLMFormat.gemini_generate_content:
        return os.environ.get("GEMINI_API_KEY", "")
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")


class LLMClient:
    def __init__(self, provider: str, api_key: str = "", base_url: str | None = None, timeout: float = 180):
        self.provider_format = normalize_format(provider)
        self.api_key = api_key or default_api_key(provider)
        self.base_url = base_url or self._default_base_url()
        self.timeout = timeout

    def generate(self, request: LLMRequest) -> LLMResponse:
        if self.provider_format == LLMFormat.responses:
            return self._responses(request)
        if self.provider_format == LLMFormat.chat_completions:
            return self._chat_completions(request)
        if self.provider_format == LLMFormat.anthropic_messages:
            return self._anthropic_messages(request)
        if self.provider_format == LLMFormat.gemini_generate_content:
            return self._gemini_generate_content(request)
        raise AssertionError(self.provider_format)

    def _default_base_url(self) -> str:
        if self.provider_format in {LLMFormat.responses, LLMFormat.chat_completions}:
            return DEFAULT_RESPONSES_BASE_URL
        if self.provider_format == LLMFormat.gemini_generate_content:
            return DEFAULT_GEMINI_BASE_URL
        return DEFAULT_ANTHROPIC_BASE_URL

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.provider_format != LLMFormat.gemini_generate_content:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _responses(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "instructions": request.system,
            "input": _messages_as_input(request),
            "store": False,
            "reasoning": {"effort": request.reasoning_effort},
            "text": {"verbosity": "low"},
        }
        url = self.base_url.rstrip("/") + "/responses"
        if request.stream:
            payload["stream"] = True
            return LLMResponse(text=self._stream_responses(url, payload), provider_format=self.provider_format)
        data = self._post_json(url, payload)
        _raise_if_error(data)
        return LLMResponse(text=response_text(data), raw=data, provider_format=self.provider_format)

    def _stream_responses(self, url: str, payload: dict[str, Any]) -> str:
        deltas: list[str] = []
        event_data: list[str] = []
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, json=payload, headers=self._headers()) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data and data != "[DONE]":
                            event_data.append(data)
                        continue
                    if line:
                        continue
                    _collect_response_events(event_data, deltas)
                    event_data = []
        _collect_response_events(event_data, deltas)
        return "".join(deltas)

    def _chat_completions(self, request: LLMRequest) -> LLMResponse:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(message.model_dump() for message in request.messages)
        payload = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": request.stream,
        }
        url = self.base_url.rstrip("/") + "/chat/completions"
        if request.stream:
            return LLMResponse(text=self._stream_chat(url, payload), provider_format=self.provider_format)
        data = self._post_json(url, payload)
        _raise_if_error(data)
        return LLMResponse(text=(data.get("choices") or [{}])[0].get("message", {}).get("content", ""), raw=data, provider_format=self.provider_format)

    def _stream_chat(self, url: str, payload: dict[str, Any]) -> str:
        deltas: list[str] = []
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, json=payload, headers=self._headers()) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    delta = (event.get("choices") or [{}])[0].get("delta", {})
                    deltas.append(str(delta.get("content", "")))
        return "".join(deltas)

    def _anthropic_messages(self, request: LLMRequest) -> LLMResponse:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("anthropic package is required for anthropic/messages format") from exc
        client = anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        message = client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": message.role, "content": [{"type": "text", "text": message.content}]} for message in request.messages],
            temperature=request.temperature,
            thinking={"type": "disabled"},
        )
        text = "\n".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        return LLMResponse(text=text, provider_format=self.provider_format)

    def _gemini_generate_content(self, request: LLMRequest) -> LLMResponse:
        contents = []
        for message in request.messages:
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature, "maxOutputTokens": request.max_tokens},
        }
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        url = f"{self.base_url.rstrip('/')}/models/{request.model}:generateContent"
        params = {"key": self.api_key} if self.api_key else None
        data = self._post_json(url, payload, params=params)
        _raise_if_error(data)
        parts = []
        for candidate in data.get("candidates", []) or []:
            for part in (candidate.get("content") or {}).get("parts", []) or []:
                if "text" in part:
                    parts.append(str(part["text"]))
        return LLMResponse(text="".join(parts), raw=data, provider_format=self.provider_format)

    def _post_json(self, url: str, payload: dict[str, Any], params: dict[str, str] | None = None) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, headers=self._headers(), params=params)
            response.raise_for_status()
            return response.json()


def generate_text(system: str, user: str, model: str, provider: str, api_key: str = "", base_url: str | None = None, max_tokens: int = 4096, stream: bool = False, timeout: float = 180) -> str:
    request = LLMRequest.text(system=system, user=user, model=model, max_tokens=max_tokens, stream=stream)
    return LLMClient(provider=provider, api_key=api_key, base_url=base_url, timeout=timeout).generate(request).text


def response_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    parts: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content["text"]))
    return "".join(parts)


def _messages_as_input(request: LLMRequest) -> str | list[dict[str, str]]:
    if len(request.messages) == 1 and request.messages[0].role == "user":
        return request.messages[0].content
    return [message.model_dump() for message in request.messages]


def _collect_response_events(event_data: list[str], deltas: list[str]) -> None:
    for raw in event_data:
        event = json.loads(raw)
        if event.get("type") == "response.output_text.delta":
            deltas.append(str(event.get("delta", "")))
        elif event.get("type") == "response.completed":
            error = (event.get("response") or {}).get("error")
            if error:
                raise RuntimeError(error)


def _raise_if_error(data: dict[str, Any]) -> None:
    if data.get("error"):
        raise RuntimeError(data["error"])
