"""
тесты публичного http-контракта"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import routes


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.client = routes.app.test_client()

    def test_health_reports_ready_state_with_required_key(self):
        with (
            patch.object(routes, "SERPAPI_KEY", "test-key"),
            patch.dict(
                os.environ,
                {"VISUAL_SIMILARITY_ENABLED": "0"},
                clear=False,
            ),
            patch.object(routes.os, "makedirs"),
            patch.object(routes.os, "access", return_value=True),
        ):
            response = self.client.get("/health")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checks"]["serpapi_key"]["status"], "ok")
        self.assertEqual(payload["checks"]["clip_model"]["status"], "skip")

    def test_analyze_rejects_request_without_image(self):
        response = self.client.post("/analyze", data={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {
                "message": "не передано поле image",
                "status": "error",
            },
        )

    def test_results_route_serves_only_allowed_report_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir, "report_test.html")
            report_path.write_text("<html>ok</html>", encoding="utf-8")

            with patch.object(routes, "get_results_dir", return_value=temp_dir):
                valid_response = self.client.get("/results/report_test.html")
                invalid_response = self.client.get("/results/not-allowed.txt")
                traversal_response = self.client.get("/results/../.env")

        self.assertEqual(valid_response.status_code, 200)
        self.assertEqual(valid_response.get_data(as_text=True), "<html>ok</html>")
        self.assertEqual(invalid_response.status_code, 404)
        self.assertEqual(traversal_response.status_code, 404)

    def test_report_status_maps_service_states_to_http_codes(self):
        cases = (
            ({"status": "ready", "task_id": "ready"}, 200),
            ({"status": "not_found", "task_id": "missing"}, 404),
            ({"status": "error", "task_id": "failed"}, 503),
        )

        for service_payload, expected_status in cases:
            with self.subTest(service_payload=service_payload):
                with patch.object(
                    routes,
                    "get_report_task_status",
                    return_value=service_payload,
                ):
                    response = self.client.get(
                        f"/report-status/{service_payload['task_id']}"
                    )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.get_json(), service_payload)


if __name__ == "__main__":
    unittest.main()
