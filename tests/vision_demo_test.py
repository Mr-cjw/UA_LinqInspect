"""Tests for the transparent synthetic-data vision inspection demo."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image, ImageFilter

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
RPI_ROOT = REPO_ROOT.parent / "rpi4-opcua-oee-rca-gateway"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import vision_demo
from demo_edge_core import _DashboardHandler


def post_json(base_url: str, path: str, payload: dict[str, object]) -> tuple[int, dict]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get_text(base_url: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(base_url + path, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


class VisionModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = vision_demo.get_model()
        cls.card = cls.model.model_card()

    def test_model_card_declares_synthetic_data_boundary(self) -> None:
        self.assertEqual(self.card["modelVersion"], vision_demo.MODEL_VERSION)
        self.assertEqual(self.card["datasetVersion"], vision_demo.DATASET_VERSION)
        self.assertEqual(self.card["trainingSamples"], 850)
        self.assertEqual(self.card["validationSamples"], 250)
        self.assertGreaterEqual(self.card["validationAccuracy"], 0.9)
        self.assertGreaterEqual(self.card["validationMacroRecall"], 0.9)
        self.assertEqual(len(self.card["modelSha256"]), 64)
        metrics = self.card["validationMetrics"]
        self.assertIn("仅合成留出集", metrics["scope"])
        self.assertEqual(len(metrics["perClass"]), len(vision_demo.CLASSES))
        self.assertEqual(sum(item["support"] for item in metrics["perClass"]), 250)
        self.assertEqual(len(metrics["confusionMatrix"]["rows"]), len(vision_demo.CLASSES))
        limitations = " ".join(self.card["limitations"])
        self.assertIn("合成", limitations)
        self.assertIn("不是客户现场精度", limitations)
        self.assertIn("高置信度不等于真实产线正确率", limitations)
        self.assertIn("未知缺陷", limitations)
        self.assertIn("人工复核", limitations)
        quality_gate = self.card["inputQualityGate"]
        self.assertEqual(quality_gate["version"], vision_demo.QUALITY_GATE_VERSION)
        self.assertEqual(quality_gate["thresholds"]["minDimension"], 64)
        self.assertIn("不替代真实采图", quality_gate["scope"])

    def test_all_builtin_samples_are_classified_with_real_inference(self) -> None:
        for label in vision_demo.CLASSES:
            with self.subTest(label=label):
                result = vision_demo.classify_sample(label)
                self.assertEqual(result["predictedClass"], label)
                self.assertEqual(result["sample"], label)
                self.assertGreaterEqual(result["confidence"], 0.0)
                self.assertLessEqual(result["confidence"], 1.0)
                self.assertGreaterEqual(result["inferenceMs"], 0.0)
                self.assertEqual(result["decision"], "OK" if label == "OK" else "NG")
                self.assertEqual(result["modelCard"]["datasetVersion"], vision_demo.DATASET_VERSION)

    def test_probability_distribution_is_complete_and_normalized(self) -> None:
        result = vision_demo.classify_sample("STAIN")
        probabilities = result["probabilities"]
        self.assertEqual([item["code"] for item in probabilities], list(vision_demo.CLASSES))
        self.assertAlmostEqual(sum(item["probability"] for item in probabilities), 1.0, places=5)

    def test_low_confidence_is_routed_to_review_without_ng_order(self) -> None:
        for label in ("OK", "SCRATCH"):
            with self.subTest(label=label):
                with mock.patch.object(vision_demo, "REVIEW_THRESHOLD", 1.1):
                    result = vision_demo.classify_sample(label)
                self.assertEqual(result["decision"], "REVIEW")
                self.assertIn("人工复核", result["decisionReason"])
                self.assertIn("不自动开单", result["decisionReason"])

    def test_quality_gate_rejects_unusable_images_before_classification(self) -> None:
        cases = {
            "blank": Image.new("RGB", (128, 128), (128, 128, 128)),
            "blurred": vision_demo.make_sample_image("SCRATCH", 7001).filter(
                ImageFilter.GaussianBlur(12)
            ),
            "overexposed": Image.new("RGB", (128, 128), (255, 255, 255)),
            "underexposed": Image.new("RGB", (128, 128), (0, 0, 0)),
            "too-small": vision_demo.make_sample_image("OK", 7001).resize((32, 32)),
        }
        for name, image in cases.items():
            with self.subTest(name=name):
                result = vision_demo.classify_image(image)
                self.assertEqual(result["qualityGate"]["status"], "REVIEW")
                self.assertEqual(result["decision"], "REVIEW")
                self.assertIsNone(result["predictedClass"])
                self.assertEqual(result["probabilities"], [])
                self.assertIn("图像质量门禁未通过", result["decisionReason"])
                self.assertIn("不执行自动分类", result["decisionReason"])
                self.assertIn("不自动放行或开单", result["decisionReason"])

    def test_quality_gate_does_not_invoke_classifier(self) -> None:
        model = vision_demo.get_model()
        blank = Image.new("RGB", (128, 128), (128, 128, 128))
        with mock.patch.object(
            model,
            "predict",
            side_effect=AssertionError("quality gate must not invoke prediction"),
        ) as predict:
            result = vision_demo.classify_image(blank)
        predict.assert_not_called()
        self.assertEqual(result["qualityGate"]["status"], "REVIEW")
        self.assertEqual(result["decision"], "REVIEW")
        self.assertIsNone(result["predictedClass"])
        self.assertEqual(result["probabilities"], [])

    def test_quality_gate_passes_valid_samples_and_exposes_metrics(self) -> None:
        result = vision_demo.classify_sample("OK")
        gate = result["qualityGate"]
        self.assertEqual(gate["status"], "PASS")
        self.assertEqual(len(gate["checks"]), 5)
        for key in (
            "width",
            "height",
            "meanBrightness",
            "contrast",
            "sharpness",
            "overexposedFraction",
            "underexposedFraction",
        ):
            self.assertIn(key, gate["metrics"])
        self.assertEqual(
            result["modelCard"]["inputQualityGate"]["version"],
            vision_demo.QUALITY_GATE_VERSION,
        )

    def test_data_url_upload_is_decoded_and_classified(self) -> None:
        buffer = io.BytesIO()
        vision_demo.make_sample_image("DENT", 7001).save(buffer, format="PNG")
        payload = buffer.getvalue()
        encoded = base64.b64encode(payload).decode("ascii")
        result = vision_demo.classify_base64("data:image/png;base64," + encoded)
        self.assertEqual(result["predictedClass"], "DENT")
        self.assertEqual(result["imageSha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["imageHashScope"], "original-upload-bytes")

    def test_invalid_base64_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Base64"):
            vision_demo.classify_base64("not-base64!!!")

    def test_oversized_or_unsupported_upload_is_rejected(self) -> None:
        encoded = base64.b64encode(b"x" * 64).decode("ascii")
        with mock.patch.object(vision_demo, "MAX_UPLOAD_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "8 MB"):
                vision_demo.classify_base64(encoded)

        buffer = io.BytesIO()
        vision_demo.make_sample_image("OK", 7001).save(buffer, format="BMP")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        with self.assertRaisesRegex(ValueError, "PNG/JPEG/WebP"):
            vision_demo.classify_base64(encoded)

        buffer = io.BytesIO()
        vision_demo.make_sample_image("OK", 7001).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        with mock.patch.object(vision_demo, "MAX_IMAGE_PIXELS", 100):
            with self.assertRaisesRegex(ValueError, "像素数"):
                vision_demo.classify_base64(encoded)

    def test_dashboard_exposes_routes_and_boundaries_without_personalized_copy(self) -> None:
        html = vision_demo.VISION_DASHBOARD
        self.assertIn("/api/vision/classify", html)
        self.assertIn("/api/vision/run", html)
        self.assertIn("低置信度", html)
        self.assertIn("人工复核", html)
        self.assertIn("合成", html)
        self.assertIn("类别召回", html)
        self.assertIn("图像质量", html)
        self.assertIn("质量指标", html)
        self.assertIn("不执行自动分类", html)
        self.assertIn("图像 SHA-256", html)
        self.assertIn("不是生产视觉验收", html)
        self.assertIn("未知缺陷零漏检", html)
        self.assertNotIn("投资" + "评审", html)
        self.assertNotIn("/api/" + "invest" + "or", html)


class VisionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _DashboardHandler)
        cls.server.daemon_threads = True
        cls.server.demo_lock = threading.Lock()
        cls.server.joint_lock = threading.Lock()
        cls.server.vision_lock = threading.Lock()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_vision_page_is_served(self) -> None:
        status, html = get_text(self.base_url, "/vision")
        self.assertEqual(status, 200)
        self.assertIn("AI 质检最小闭环", html)
        self.assertIn("模型卡", html)

    def test_classify_route_returns_real_probabilities(self) -> None:
        status, result = post_json(self.base_url, "/api/vision/classify", {"sample": "SCRATCH"})
        self.assertEqual(status, 200)
        self.assertEqual(result["predictedClass"], "SCRATCH")
        self.assertEqual(result["decision"], "NG")
        self.assertEqual(len(result["probabilities"]), 5)
        self.assertAlmostEqual(sum(item["probability"] for item in result["probabilities"]), 1.0, places=5)

    def test_ok_sample_never_creates_a_mes_order(self) -> None:
        status, result = post_json(self.base_url, "/api/vision/run", {"sample": "OK"})
        self.assertEqual(status, 200)
        self.assertEqual(result["vision"]["decision"], "OK")
        self.assertIsNone(result["delivery"])
        self.assertIn("未生成 MES 工单", result["message"])

    def test_quality_gate_failure_never_creates_a_mes_order(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (128, 128), (128, 128, 128)).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        status, result = post_json(
            self.base_url,
            "/api/vision/run",
            {"imageBase64": "data:image/png;base64," + encoded},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["vision"]["qualityGate"]["status"], "REVIEW")
        self.assertEqual(result["vision"]["decision"], "REVIEW")
        self.assertIsNone(result["delivery"])
        self.assertIn("图像质量门禁未通过", result["message"])

    def test_invalid_sample_is_a_bad_request(self) -> None:
        status, result = post_json(self.base_url, "/api/vision/classify", {"sample": "UNKNOWN"})
        self.assertEqual(status, 400)
        self.assertIn("unsupported synthetic label", result["error"])

    @unittest.skipUnless(
        (RPI_ROOT / "src" / "index.js").is_file()
        and (RPI_ROOT / "node_modules" / "node-opcua").is_dir(),
        "sibling OEE/RCA gateway repository is unavailable",
    )
    def test_ng_sample_is_the_actual_input_to_the_joint_delivery(self) -> None:
        status, result = post_json(self.base_url, "/api/vision/run", {"sample": "STAIN"})
        self.assertEqual(status, 200)
        vision = result["vision"]
        delivery = result["delivery"]
        self.assertEqual(vision["predictedClass"], "STAIN")
        self.assertEqual(delivery["defectType"], vision["predictedClass"])
        self.assertAlmostEqual(delivery["defectConfidence"], vision["confidence"], places=5)
        self.assertEqual(delivery["visionModelVersion"], vision["modelCard"]["modelVersion"])
        self.assertEqual(delivery["visionModelSha256"], vision["modelCard"]["modelSha256"])
        self.assertEqual(delivery["imageSha256"], vision["imageSha256"])
        self.assertEqual(delivery["imageHashScope"], vision["imageHashScope"])
        self.assertEqual(delivery["imageId"], f"IMG-JX-JOINT-{vision['imageSha256'][:8].upper()}")
        self.assertEqual(delivery["finalState"], "DONE")
        self.assertTrue(delivery["mesReceiptNo"].startswith("MES-JX-DEMO-"))
        self.assertIn("合成", delivery["visionValidationScope"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
