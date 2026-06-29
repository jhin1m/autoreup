import unittest

from backend.app.integrations.llm.client import response_text
from backend.app.integrations.llm.protocol import LLMFormat, normalize_format


class LLMProtocolTest(unittest.TestCase):
    def test_provider_aliases_cover_required_formats(self):
        self.assertEqual(normalize_format("responses"), LLMFormat.responses)
        self.assertEqual(normalize_format("chat"), LLMFormat.chat_completions)
        self.assertEqual(normalize_format("/v1 chat".replace("/v1 ", "")), LLMFormat.chat_completions)
        self.assertEqual(normalize_format("messages"), LLMFormat.anthropic_messages)
        self.assertEqual(normalize_format("anthropic"), LLMFormat.anthropic_messages)
        self.assertEqual(normalize_format("gemini"), LLMFormat.gemini_generate_content)

    def test_responses_text_parser_supports_output_text_and_parts(self):
        self.assertEqual(response_text({"output_text": "hello"}), "hello")
        self.assertEqual(
            response_text({"output": [{"content": [{"type": "output_text", "text": "he"}, {"type": "output_text", "text": "llo"}]}]}),
            "hello",
        )


if __name__ == "__main__":
    unittest.main()
