# LLM Protocol

AutoReup calls LLMs through one internal text-generation protocol. Provider adapters translate this protocol to each external wire format or SDK.

## Internal request

```json
{
  "model": "gpt-5.4-mini",
  "system": "You are...",
  "messages": [
    { "role": "user", "content": "..." }
  ],
  "temperature": 0.0,
  "max_tokens": 4096,
  "stream": true,
  "response_format": "text"
}
```

## Supported provider formats

| Internal format | Wire/API target | Notes |
| --- | --- | --- |
| `responses` | OpenAI-compatible `/v1/responses` | Preferred local gateway. Uses `instructions`, `input`, `reasoning.effort=none`. |
| `chat_completions` | OpenAI-compatible `/v1/chat/completions` | For OpenAI-compatible servers that do not expose Responses. |
| `anthropic_messages` | Anthropic/DeepSeek Messages SDK | For Claude-style messages, with disabled thinking where supported. |
| `gemini_generate_content` | Gemini `models/{model}:generateContent` | REST adapter; SDK can be added behind the same interface. |

Compatibility aliases accepted by scripts: `responses`, `chat`, `messages`, `anthropic`, `gemini`.

## Default local gateway

```text
provider/format: responses
base_url: http://localhost:8090/v1
stream: true
reasoning.effort: none
```
