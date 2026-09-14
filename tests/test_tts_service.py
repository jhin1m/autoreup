import unittest

from backend.app.services.tts import TtsBlock, build_tts_blocks, cache_key, clean_tts_text, group_tts_blocks, to_provider_scale


class TtsServiceTest(unittest.TestCase):
    def test_build_tts_blocks_merges_display_split_fragments(self):
        cues = [
            {"start": 0.0, "end": 1.0, "translated": "Không có nghĩa ta không đi được,", "display_split_from": "1-3", "source_cue_ids": [1]},
            {"start": 1.0, "end": 2.0, "translated": "cũng không có nghĩa", "display_split_from": "1-3", "source_cue_ids": [2]},
            {"start": 2.0, "end": 3.0, "translated": "anh hùng thiên hạ không đi được.", "display_split_from": "1-3", "source_cue_ids": [3]},
        ]

        blocks = build_tts_blocks(cues)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].start, 0.0)
        self.assertEqual(blocks[0].end, 3.0)
        self.assertEqual(blocks[0].source_cue_ids, (1, 2, 3))
        self.assertEqual(blocks[0].text, "Không có nghĩa ta không đi được cũng không có nghĩa anh hùng thiên hạ không đi được")

    def test_build_tts_blocks_keeps_unsplit_cues_separate(self):
        cues = [
            {"start": 0.0, "end": 1.0, "translated": "Câu một", "span_id": "0-0", "source_cue_ids": [0]},
            {"start": 1.2, "end": 2.0, "translated": "Câu hai", "span_id": "1-1", "source_cue_ids": [1]},
        ]

        blocks = build_tts_blocks(cues)

        self.assertEqual([block.text for block in blocks], ["Câu một", "Câu hai"])

    def test_clean_tts_text_strips_tags_brackets_and_punctuation_in_continuous_mode(self):
        self.assertEqual(clean_tts_text("<i>Xin chào</i>, [noise] các bạn!", "continuous"), "Xin chào các bạn")

    def test_clean_tts_text_expressive_keeps_punctuation_variety(self):
        # expressive mode (default): punctuation is a prosody instruction — the
        # variety (comma / period / ellipsis / question) must survive, including
        # the closing mark.
        self.assertEqual(clean_tts_text("<i>Xin chào</i>, [noise] các bạn!"), "Xin chào, các bạn!")
        self.assertEqual(clean_tts_text("Đợi đã… rồi sao? Không thể nào."), "Đợi đã… rồi sao? Không thể nào.")

    def test_clean_tts_text_natural_flattens_punctuation_and_drops_trailing(self):
        # legacy mode stays available for comparison: one uniform comma pause,
        # trailing mark dropped (the scheduler owns the gap).
        self.assertEqual(clean_tts_text("<i>Xin chào</i>, [noise] các bạn!", "natural"), "Xin chào, các bạn")

    def test_provider_scale_maps_pipeline_ratio_to_capcut_style_integer(self):
        self.assertEqual(to_provider_scale(1.15), 12)
        self.assertEqual(to_provider_scale(0), 1)
        self.assertEqual(to_provider_scale(3), 20)

    def test_cache_key_changes_when_voice_or_text_changes(self):
        first = cache_key("http://localhost:8080", "voice-a", "xin chào", 1.0, 1.0, 1.0, "continuous")
        second = cache_key("http://localhost:8080", "voice-b", "xin chào", 1.0, 1.0, 1.0, "continuous")
        third = cache_key("http://localhost:8080", "voice-a", "xin chào bạn", 1.0, 1.0, 1.0, "continuous")

        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_cache_key_changes_when_provider_style_changes(self):
        neutral = cache_key("http://localhost:8080", "voice-a", "xin chào", 1.0, 1.0, 1.0, "continuous")
        faster = cache_key("http://localhost:8080", "voice-a", "xin chào", 1.4, 1.0, 1.0, "continuous")

        self.assertNotEqual(neutral, faster)

    def test_group_tts_blocks_merges_adjacent_short_blocks_for_smooth_voice(self):
        blocks = [
            TtsBlock("0", 0.0, 1.0, "Câu đầu", (0,)),
            TtsBlock("1", 1.05, 2.0, "câu sau", (1,)),
            TtsBlock("2", 4.0, 5.0, "câu xa", (2,)),
        ]

        grouped = group_tts_blocks(blocks, max_gap=0.2, max_duration=3.0, max_chars=80)

        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0].block_id, "0-1")
        self.assertEqual(grouped[0].source_cue_ids, (0, 1))
        self.assertEqual(grouped[0].text, "Câu đầu, câu sau")


if __name__ == "__main__":
    unittest.main()
