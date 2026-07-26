"""
тесты маскирования секретов и безопасной html-разметки"""

import tempfile
import unittest
from pathlib import Path

from utils.logger import mask_proxy_url
from utils.report import _safe_url, build_html_report


class SecurityUtilitiesTests(unittest.TestCase):
    def test_proxy_mask_removes_credentials(self):
        cases = (
            ("http://user:password@127.0.0.1:8080", "127.0.0.1:8080"),
            ("user:password@proxy.example:9000", "proxy.example:9000"),
            ("https://proxy.example:443", "proxy.example:443"),
            (None, None),
        )

        for proxy_url, expected_value in cases:
            with self.subTest(proxy_url=proxy_url):
                self.assertEqual(mask_proxy_url(proxy_url), expected_value)

    def test_safe_url_accepts_http_and_rejects_active_content(self):
        self.assertEqual(
            _safe_url("https://example.com/item/123"),
            "https://example.com/item/123",
        )
        self.assertIsNone(_safe_url("javascript:alert(1)"))
        self.assertIsNone(_safe_url("data:text/html,test"))
        self.assertIsNone(_safe_url("/relative/path"))

    def test_html_report_escapes_untrusted_target_name(self):
        payload = {
            "ai_target_name": '<script>alert("x")</script>',
            "items": [],
            "filtered_items": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir, "report.html")
            build_html_report(payload, str(output_path))
            report_html = output_path.read_text(encoding="utf-8")

        self.assertIn("Bag Price Analysis Report", report_html)
        self.assertIn("&lt;script&gt;", report_html)
        self.assertNotIn('<script>alert("x")</script>', report_html)


if __name__ == "__main__":
    unittest.main()
