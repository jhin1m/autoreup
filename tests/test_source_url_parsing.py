import unittest

from backend.app.studio.models import Platform
from backend.app.studio.providers import _extract_video_id, detect_platform, extract_url


class SourceUrlParsingTest(unittest.TestCase):
    def test_extract_url_from_douyin_share_blob(self):
        blob = (
            "4.33 复制打开抖音，看看【付不同的作品】诡异园《1024房》第一集 第一次入园 # 动画 ... "
            "https://v.douyin.com/MBZfioQ1QQE/ :0pm 08/11 w@F.Hv JIv:/"
        )
        self.assertEqual(extract_url(blob), "https://v.douyin.com/MBZfioQ1QQE/")

    def test_extract_url_trims_trailing_punctuation(self):
        self.assertEqual(extract_url("xem ngay https://v.douyin.com/ABC/】"), "https://v.douyin.com/ABC/")

    def test_extract_url_passthrough_for_plain_url(self):
        url = "https://www.douyin.com/video/7234567890123456789"
        self.assertEqual(extract_url(url), url)

    def test_extract_url_returns_trimmed_when_no_url(self):
        self.assertEqual(extract_url("  BV1xx411c7mD  "), "BV1xx411c7mD")

    def test_detect_platform_after_extraction(self):
        self.assertEqual(detect_platform(extract_url("hi https://v.douyin.com/MBZfioQ1QQE/")), Platform.douyin)

    def test_video_id_from_resolved_share_path(self):
        self.assertEqual(
            _extract_video_id("https://www.iesdouyin.com/share/video/7234567890123456789/?region=CN"),
            "7234567890123456789",
        )

    def test_video_id_from_modal_query(self):
        self.assertEqual(
            _extract_video_id("https://www.douyin.com/jingxuan?modal_id=7649023281428778240"),
            "7649023281428778240",
        )


if __name__ == "__main__":
    unittest.main()
