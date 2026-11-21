"""Transparent, hardware-free vision inspection demonstration.

The model is a small neural network trained from deterministic synthetic
images. It performs real inference, but its validation accuracy applies only to
the synthetic distribution. This module is intentionally explicit about that
boundary and must not be presented as production vision performance.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

MODEL_VERSION = "synthetic-vision-mlp-v1"
MODEL_TYPE = "MLP over pooled grayscale + edge + local-contrast features"
DATASET_VERSION = "procedural-synthetic-v1"
IMAGE_SIZE = 128
FEATURE_SIZE = 32
CLASSES = ("OK", "SCRATCH", "DENT", "STAIN", "MISSING")
DISPLAY_NAMES = {
    "OK": "正常",
    "SCRATCH": "划痕",
    "DENT": "凹坑",
    "STAIN": "污渍",
    "MISSING": "缺件",
}
TRAIN_PER_CLASS = 170
VALIDATION_PER_CLASS = 50
MODEL_SEED = 20261121
REVIEW_THRESHOLD = 0.60
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})

_LIMITATIONS = [
    "训练和验证图像均由程序合成，不代表真实工厂图像分布。",
    "留出集准确率只对合成数据成立，不是客户现场精度。",
    "这是闭集分类器；未知缺陷会被归入最相似的已知类别。",
    "概率未做现场校准；高置信度不等于真实产线正确率。",
    "置信度低于阈值时只进入人工复核，不自动生成 MES 工单。",
    "没有使用真实相机、光源、镜头、工装或客户标注数据。",
]


def _background(rng: np.random.Generator) -> np.ndarray:
    base = rng.normal(128, 6, (12, 12))
    image = Image.fromarray(np.clip(base, 90, 165).astype(np.uint8)).convert("L")
    image = image.resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR)
    data = np.asarray(image, dtype=np.float32)
    yy, xx = np.mgrid[0:IMAGE_SIZE, 0:IMAGE_SIZE]
    data += 7 * np.sin(xx / (IMAGE_SIZE / 5) + rng.uniform(0, 6.28))
    data += 4 * np.sin(yy / (IMAGE_SIZE / 7) + rng.uniform(0, 6.28))
    data += rng.normal(0, 3.5, (IMAGE_SIZE, IMAGE_SIZE))
    return np.clip(data, 0, 255).astype(np.uint8)


def make_sample_image(label: str, seed: int) -> Image.Image:
    """Generate one deterministic synthetic inspection image."""
    if label not in CLASSES:
        raise ValueError(f"unsupported synthetic label: {label}")
    rng = np.random.default_rng(seed)
    image = Image.fromarray(_background(rng)).convert("L")

    if label == "SCRATCH":
        draw = ImageDraw.Draw(image)
        for _ in range(int(rng.integers(1, 3))):
            x1, y1 = rng.uniform(-15, 30), rng.uniform(10, 118)
            x2, y2 = rng.uniform(98, 143), rng.uniform(10, 118)
            draw.line((x1, y1, x2, y2), fill=int(rng.uniform(205, 245)), width=int(rng.integers(3, 9)))
        draw.line((x1 + 2, y1 + 2, x2 + 2, y2 + 2), fill=25, width=2)
    elif label == "DENT":
        cx, cy = rng.uniform(34, 94), rng.uniform(34, 94)
        rx, ry = rng.uniform(14, 30), rng.uniform(12, 26)
        shade = Image.new("L", (IMAGE_SIZE, IMAGE_SIZE), 0)
        ImageDraw.Draw(shade).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=155)
        shade = shade.filter(ImageFilter.GaussianBlur(8))
        dark = Image.new("RGBA", (IMAGE_SIZE, IMAGE_SIZE), (25, 25, 35, 0))
        dark.putalpha(shade)
        image = Image.alpha_composite(image.convert("RGBA"), dark).convert("L")
        ImageDraw.Draw(image).arc(
            (cx - rx, cy - ry, cx + rx, cy + ry), 205, 335, fill=225, width=4
        )
    elif label == "STAIN":
        draw = ImageDraw.Draw(image)
        for _ in range(int(rng.integers(2, 5))):
            cx, cy = rng.uniform(16, 112), rng.uniform(16, 112)
            rx, ry = rng.uniform(8, 24), rng.uniform(6, 20)
            draw.ellipse(
                (cx - rx, cy - ry, cx + rx, cy + ry),
                fill=int(rng.uniform(40, 65)),
            )
        image = image.filter(ImageFilter.GaussianBlur(3))
    elif label == "MISSING":
        draw = ImageDraw.Draw(image)
        x1, y1 = rng.uniform(4, 52), rng.uniform(4, 52)
        width, height = rng.uniform(44, 76), rng.uniform(36, 64)
        draw.rectangle((x1, y1, x1 + width, y1 + height), fill=20)
        draw.rectangle((x1 + 4, y1 + 4, x1 + width - 4, y1 + height - 4), fill=5)

    return image.convert("RGB")


def _feature_vector(image: Image.Image) -> np.ndarray:
    image = ImageOps.exif_transpose(image).convert("L").resize(
        (IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR
    )
    data = np.asarray(image, dtype=np.float32) / 255.0
    gx = np.zeros_like(data)
    gy = np.zeros_like(data)
    gx[:, 1:-1] = np.abs(data[:, 2:] - data[:, :-2])
    gy[1:-1, :] = np.abs(data[2:, :] - data[:-2, :])
    edge = np.clip((gx + gy) * 2.5, 0.0, 1.0)
    blurred = np.asarray(
        Image.fromarray((data * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(5)
        ),
        dtype=np.float32,
    ) / 255.0
    local_contrast = np.abs(data - blurred)

    def pool(values: np.ndarray) -> np.ndarray:
        block = IMAGE_SIZE // FEATURE_SIZE
        return values.reshape(FEATURE_SIZE, block, FEATURE_SIZE, block).max(axis=(1, 3))

    return np.concatenate(
        [pool(data), pool(edge), pool(local_contrast)]
    ).reshape(-1)


def _png_data_url(image: Image.Image) -> tuple[str, str]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}", hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Prediction:
    label: str
    confidence: float
    probabilities: dict[str, float]
    decision: str
    decision_reason: str


class SyntheticVisionModel:
    """A deterministic image classifier with an explicit synthetic-data card."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._weights: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None
        self._mean: np.ndarray | None = None
        self._scale: np.ndarray | None = None
        self.validation_accuracy = 0.0
        self.validation_macro_recall = 0.0
        self.validation_samples = 0
        self.training_samples = 0
        self.validation_metrics: list[dict[str, Any]] = []
        self.validation_confusion: list[list[int]] = []
        self.model_hash = ""

    def _train(self) -> None:
        rng = np.random.default_rng(MODEL_SEED)
        train_features: list[np.ndarray] = []
        train_labels: list[int] = []
        test_features: list[np.ndarray] = []
        test_labels: list[int] = []
        for class_index, label in enumerate(CLASSES):
            for sample_index in range(TRAIN_PER_CLASS + VALIDATION_PER_CLASS):
                seed = int(rng.integers(0, 2**31 - 1))
                vector = _feature_vector(make_sample_image(label, seed))
                if sample_index < TRAIN_PER_CLASS:
                    train_features.append(vector)
                    train_labels.append(class_index)
                else:
                    test_features.append(vector)
                    test_labels.append(class_index)

        train_x = np.stack(train_features).astype(np.float32)
        train_y = np.asarray(train_labels, dtype=np.int64)
        test_x = np.stack(test_features).astype(np.float32)
        test_y = np.asarray(test_labels, dtype=np.int64)
        train_order = rng.permutation(len(train_y))
        test_order = rng.permutation(len(test_y))
        train_x, train_y = train_x[train_order], train_y[train_order]
        test_x, test_y = test_x[test_order], test_y[test_order]

        mean = train_x.mean(axis=0)
        scale = train_x.std(axis=0) + 1e-6
        train_x = (train_x - mean) / scale
        test_x = (test_x - mean) / scale

        hidden = 96
        output = len(CLASSES)
        initializer = np.random.default_rng(7)
        w1 = initializer.normal(0, np.sqrt(2 / train_x.shape[1]), (train_x.shape[1], hidden)).astype(np.float32)
        b1 = np.zeros(hidden, dtype=np.float32)
        w2 = initializer.normal(0, np.sqrt(2 / hidden), (hidden, output)).astype(np.float32)
        b2 = np.zeros(output, dtype=np.float32)
        params = [w1, b1, w2, b2]
        first_moment = [np.zeros_like(item) for item in params]
        second_moment = [np.zeros_like(item) for item in params]

        for epoch in range(1, 81):
            epoch_order = rng.permutation(len(train_y))
            for start in range(0, len(train_y), 64):
                indexes = epoch_order[start:start + 64]
                batch_x, batch_y = train_x[indexes], train_y[indexes]
                z1 = batch_x @ w1 + b1
                a1 = np.maximum(z1, 0)
                z2 = a1 @ w2 + b2
                z2 -= z2.max(axis=1, keepdims=True)
                probabilities = np.exp(z2)
                probabilities /= probabilities.sum(axis=1, keepdims=True)
                probabilities[np.arange(len(batch_y)), batch_y] -= 1
                probabilities /= len(batch_y)
                gw2 = a1.T @ probabilities + 1e-4 * w2
                gb2 = probabilities.sum(axis=0)
                dz1 = (probabilities @ w2.T) * (z1 > 0)
                gw1 = batch_x.T @ dz1 + 1e-4 * w1
                gb1 = dz1.sum(axis=0)
                for index, (param, gradient) in enumerate(
                    zip(params, (gw1, gb1, gw2, gb2))
                ):
                    first_moment[index] = 0.9 * first_moment[index] + 0.1 * gradient
                    second_moment[index] = 0.999 * second_moment[index] + 0.001 * gradient * gradient
                    corrected_first = first_moment[index] / (1 - 0.9**epoch)
                    corrected_second = second_moment[index] / (1 - 0.999**epoch)
                    param -= 0.001 * corrected_first / (np.sqrt(corrected_second) + 1e-8)

        predictions = (np.maximum(test_x @ w1 + b1, 0) @ w2 + b2).argmax(axis=1)
        confusion = np.zeros((output, output), dtype=np.int64)
        for actual, predicted in zip(test_y, predictions):
            confusion[int(actual), int(predicted)] += 1
        per_class_metrics: list[dict[str, Any]] = []
        for index, code in enumerate(CLASSES):
            true_positive = int(confusion[index, index])
            false_positive = int(confusion[:, index].sum() - true_positive)
            false_negative = int(confusion[index, :].sum() - true_positive)
            precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
            recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
            per_class_metrics.append(
                {
                    "code": code,
                    "label": DISPLAY_NAMES[code],
                    "precision": round(precision, 6),
                    "recall": round(recall, 6),
                    "support": int(confusion[index, :].sum()),
                }
            )
        model_digest = hashlib.sha256()
        for value in (w1, b1, w2, b2, mean, scale):
            model_digest.update(np.asarray(value, dtype="<f4").tobytes())
        self._weights = (w1, b1, w2, b2)
        self._mean = mean
        self._scale = scale
        self.validation_accuracy = float((predictions == test_y).mean())
        self.validation_macro_recall = float(
            np.mean([item["recall"] for item in per_class_metrics])
        )
        self.validation_samples = int(len(test_y))
        self.training_samples = int(len(train_y))
        self.validation_metrics = per_class_metrics
        self.validation_confusion = confusion.tolist()
        self.model_hash = model_digest.hexdigest()
        self._ready = True

    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if not self._ready:
                self._train()

    def predict(self, image: Image.Image) -> Prediction:
        self.ensure_ready()
        assert self._weights is not None and self._mean is not None and self._scale is not None
        w1, b1, w2, b2 = self._weights
        vector = (_feature_vector(image) - self._mean) / self._scale
        logits = np.maximum(vector @ w1 + b1, 0) @ w2 + b2
        logits -= logits.max()
        values = np.exp(logits)
        values /= values.sum()
        index = int(values.argmax())
        label = CLASSES[index]
        confidence = float(values[index])
        probabilities = {
            class_name: round(float(probability), 6)
            for class_name, probability in zip(CLASSES, values)
        }
        if confidence < REVIEW_THRESHOLD:
            decision = "REVIEW"
            reason = (
                f"最高置信度 {confidence:.1%} 低于 {REVIEW_THRESHOLD:.0%} 阈值"
                f"（预测为 {DISPLAY_NAMES[label]}），转人工复核，不自动开单或放行。"
            )
        elif label == "OK":
            decision = "OK"
            reason = "模型判定为正常且达到置信度阈值；本 Demo 仍建议保留人工抽检。"
        else:
            decision = "NG"
            reason = f"模型判定为 {DISPLAY_NAMES[label]}，达到自动判定阈值；可进入质量交付链路。"
        return Prediction(label, confidence, probabilities, decision, reason)

    def model_card(self) -> dict[str, Any]:
        self.ensure_ready()
        return {
            "modelVersion": MODEL_VERSION,
            "modelType": MODEL_TYPE,
            "datasetVersion": DATASET_VERSION,
            "trainingSamples": self.training_samples,
            "validationSamples": self.validation_samples,
            "validationAccuracy": round(self.validation_accuracy, 6),
            "validationMacroRecall": round(self.validation_macro_recall, 6),
            "validationMetrics": {
                "scope": "仅合成留出集；类别级指标不代表客户现场",
                "perClass": self.validation_metrics,
                "confusionMatrix": {
                    "labels": list(CLASSES),
                    "rows": self.validation_confusion,
                },
            },
            "validationScope": "合成留出集，不是生产现场数据",
            "reviewThreshold": REVIEW_THRESHOLD,
            "modelSha256": self.model_hash,
            "classes": [
                {"code": code, "label": DISPLAY_NAMES[code]} for code in CLASSES
            ],
            "limitations": _LIMITATIONS,
        }


_MODEL: SyntheticVisionModel | None = None
_MODEL_LOCK = threading.Lock()


def get_model() -> SyntheticVisionModel:
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = SyntheticVisionModel()
    return _MODEL


def classify_image(
    image: Image.Image,
    *,
    sample: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    model = get_model()
    prediction = model.predict(image)
    data_url, canonical_hash = _png_data_url(image)
    image_hash = source_sha256 or canonical_hash
    result = {
        "sample": sample,
        "imageDataUrl": data_url,
        "imageSha256": image_hash,
        "imageHashScope": "original-upload-bytes" if source_sha256 else "canonical-png-bytes",
        "imageWidth": image.width,
        "imageHeight": image.height,
        "predictedClass": prediction.label,
        "predictedLabel": DISPLAY_NAMES[prediction.label],
        "confidence": round(prediction.confidence, 6),
        "decision": prediction.decision,
        "decisionReason": prediction.decision_reason,
        "probabilities": [
            {
                "code": code,
                "label": DISPLAY_NAMES[code],
                "probability": prediction.probabilities[code],
            }
            for code in CLASSES
        ],
        "modelCard": model.model_card(),
        "inferenceMs": round((time.perf_counter() - started) * 1000, 2),
    }
    return result


def classify_sample(label: str, seed: int = 7001) -> dict[str, Any]:
    return classify_image(make_sample_image(label, seed), sample=label)


def classify_base64(image_base64: str) -> dict[str, Any]:
    encoded = image_base64.split(",", 1)[-1]
    try:
        payload = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("图片不是有效的 Base64 数据") from exc
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("图片超过 8 MB 限制")
    source_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        image = Image.open(io.BytesIO(payload))
        if image.format not in ALLOWED_IMAGE_FORMATS:
            raise ValueError("仅支持 PNG/JPEG/WebP 图片")
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError(
                f"图片像素数超过 {MAX_IMAGE_PIXELS // 1_000_000} MP 限制"
            )
        image.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法解析图片，请上传 PNG/JPEG/WebP") from exc
    return classify_image(
        ImageOps.exif_transpose(image).convert("RGB"),
        source_sha256=source_sha256,
    )


VISION_DASHBOARD = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 质检最小闭环 · 真实推理 Demo</title>
<style>
:root{--bg:#09131c;--panel:#102132;--panel2:#0c1b29;--line:#29475f;--text:#edf4fb;--muted:#99afc3;--mint:#43d6a0;--amber:#e6b45b;--red:#f0746b;--blue:#68b8ee}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0%,#4a3118 0,transparent 30%),radial-gradient(circle at 5% 0%,#143955 0,transparent 36%),var(--bg);color:var(--text);font-family:"Noto Sans SC","Microsoft YaHei",system-ui,sans-serif;line-height:1.65}.wrap{max-width:1220px;margin:auto;padding:0 26px 55px}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:20px 0;border-bottom:1px solid #ffffff16}.brand{font-weight:900}.mark{display:inline-grid;place-items:center;width:31px;height:31px;margin-right:9px;border-radius:9px;background:linear-gradient(135deg,var(--mint),#b9f3dd);color:#0b1b18}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a{border:1px solid #45647b;border-radius:8px;padding:7px 11px;color:var(--text);text-decoration:none;font-size:11px;font-weight:800}.nav a:hover{border-color:var(--mint);color:var(--mint)}
.hero{display:grid;grid-template-columns:1.25fr .75fr;gap:30px;align-items:center;padding:44px 0 32px}.eyebrow{font-size:11px;font-weight:900;letter-spacing:.15em;color:var(--mint)}h1{font-size:clamp(34px,4.6vw,58px);line-height:1.06;letter-spacing:-.04em;margin:12px 0 15px}h1 em{font-style:normal;color:var(--amber)}.lede{font-size:15px;line-height:1.85;color:#c4d2de;margin:0;max-width:780px}.warn{border:1px solid #7d5b25;background:#251f12;border-radius:12px;padding:13px 15px;color:#ead6ab;font-size:11px;line-height:1.7}.warn b{color:var(--amber)}
.main{display:grid;grid-template-columns:1.03fr .97fr;gap:15px}.panel{border:1px solid var(--line);border-radius:16px;background:linear-gradient(180deg,#112437,#0b1a27);padding:18px}.panel h2{font-size:16px;margin:0 0 12px}.samples{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}.sample{border:1px solid #35566f;background:#0c2032;color:#d4e4f2;border-radius:8px;padding:8px 10px;font-size:11px;font-weight:800;cursor:pointer}.sample.active{border-color:var(--mint);background:#103326;color:var(--mint)}.upload{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:8px 0 13px}.upload input{font-size:10px;color:var(--muted);max-width:230px}.imagebox{position:relative;border:1px solid #2b4b63;border-radius:12px;overflow:hidden;background:#07131e;min-height:280px;display:grid;place-items:center}.imagebox img{display:block;width:100%;max-height:390px;object-fit:contain}.imagebox .empty{color:var(--muted);font-size:11px;padding:35px}.tag{position:absolute;left:12px;top:12px;border:1px solid #2c6d56;background:#0c2d23d9;color:var(--mint);border-radius:99px;padding:5px 9px;font-size:10px;font-weight:900}
.decision{border:1px solid #2d526d;border-radius:12px;padding:15px;background:#0a1d2d;margin-top:13px}.decision .head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.decision h3{margin:0;font-size:26px}.decision .meta{text-align:right;color:var(--muted);font-size:10px}.decision p{color:var(--muted);font-size:11px;margin:7px 0 0}.pill{display:inline-block;border-radius:99px;padding:4px 8px;font-size:9px;font-weight:900;border:1px solid #47637a;color:var(--blue)}.pill.ok{border-color:#2d795e;background:#102f25;color:var(--mint)}.pill.ng{border-color:#7b4335;background:#301a18;color:var(--red)}.pill.review{border-color:#775b2c;background:#2d2414;color:var(--amber)}.run{width:100%;margin-top:12px;border:0;border-radius:9px;padding:12px;font-size:13px;font-weight:900;cursor:pointer;background:linear-gradient(135deg,var(--mint),#b8f1db);color:#082018}.run:disabled{opacity:.45;cursor:not-allowed}.run.secondary{background:#122d42;color:var(--blue);border:1px solid #315d7d}
.bars{display:grid;gap:7px;margin-top:12px}.barrow{display:grid;grid-template-columns:64px 1fr 48px;gap:8px;align-items:center;font-size:10px;color:var(--muted)}.track{height:7px;border-radius:99px;background:#0a1925;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--mint));border-radius:99px}.cardgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.stat{border:1px solid #27465e;border-radius:9px;padding:9px;background:#0b1c2b}.stat span{display:block;font-size:9px;color:var(--muted)}.stat b{display:block;margin-top:3px;font-size:14px}.modelcard{margin-top:12px;border-top:1px solid #ffffff12;padding-top:12px}.modelcard h3{font-size:12px;margin:0 0 7px}.modelcard ul{padding-left:16px;margin:5px 0 0}.modelcard li{font-size:10px;color:var(--muted);line-height:1.55;margin:4px 0}.delivery{margin-top:13px;border:1px solid #2d526d;border-radius:12px;padding:13px;background:#0a1d2d;display:none}.delivery h3{font-size:13px;margin:0 0 7px}.steps{margin-top:9px;display:grid;gap:5px}.step{border:1px solid #203e56;border-radius:7px;padding:7px 8px;font-size:10px;color:var(--muted)}.step b{color:var(--text)}.error{color:var(--red);font-size:11px;white-space:pre-wrap}.spin{animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.4}}.foot{color:var(--muted);font-size:10px;line-height:1.7;margin-top:20px;border-top:1px solid #ffffff12;padding-top:13px}
@media(max-width:900px){.hero,.main{grid-template-columns:1fr}.hero{padding-top:30px}}@media(max-width:560px){.wrap{padding:0 15px}.hero h1{font-size:34px}.cardgrid{grid-template-columns:1fr}.nav{display:none}.barrow{grid-template-columns:54px 1fr 44px}}
</style>
</head>
<body><div class="wrap">
<header class="top"><div class="brand"><span class="mark">UL</span>UA_LinqInspect · Vision</div><nav class="nav"><a href="/overview">项目总览</a><a href="/vision">AI 质检</a><a href="/joint">联合时间线</a><a href="/">交付演示</a></nav></header>
<section class="hero"><div><div class="eyebrow">AI 质检最小闭环 · 真实推理，合成数据边界可见</div><h1>AI 先看见缺陷，<em>质量闭环</em>再让结果产生业务作用</h1><p class="lede">这张页面不是预录的“AI 效果图”。图片会进入本地神经网络，输出缺陷类别、置信度和完整概率分布；达到阈值后才允许进入 Edge Core、MES 回执与设备解释链路，低置信度只转人工复核。</p></div><div class="warn"><b>边界先行：</b>模型训练与验证均使用程序合成图像，留出集指标不等于真实产线精度；没有真实相机、客户样本和现场验收。本页用于证明推理链路与决策门禁可运行，不用于宣称生产 AI 能力。</div></section>
<div class="main">
 <section class="panel"><h2>1. 选择或上传一张检测图片</h2><div id="samples" class="samples"></div><div class="upload"><input id="file" type="file" accept="image/png,image/jpeg,image/webp"><button id="upload-btn" class="sample">上传图片并推理</button></div><div class="imagebox"><div id="image-empty" class="empty">选择内置样本或上传图片后，模型会在这里显示输入证据。</div><img id="image" alt="待检测图片" hidden><span id="image-tag" class="tag" hidden>等待推理</span></div><div id="decision" class="decision"><div class="head"><div><span id="decision-pill" class="pill">等待推理</span><h3 id="decision-title">尚未分类</h3></div><div id="decision-meta" class="meta">模型版本：--<br>推理耗时：--</div></div><p id="decision-reason">点击任意内置样本，真实模型会在本机训练一次并执行推理。</p><button id="deliver" class="run" disabled>把这条 NG 送入质量闭环</button></div></section>
 <aside class="panel"><h2>2. 模型输出与可信边界</h2><div id="bars" class="bars"><div class="error">尚未运行模型。</div></div><div class="cardgrid"><div class="stat"><span>留出集准确率</span><b id="accuracy">--</b></div><div class="stat"><span>验证样本</span><b id="validation">--</b></div><div class="stat"><span>模型版本</span><b id="version" style="font-size:10px">--</b></div><div class="stat"><span>训练数据</span><b style="font-size:11px">程序合成</b></div></div><div class="modelcard"><h3>模型卡：必须一起展示</h3><div id="model-meta" class="modelcard"><div class="error">模型尚未初始化。</div></div><ul id="limitations"></ul></div><div id="delivery" class="delivery"><h3>质量闭环结果</h3><div id="delivery-summary"></div><div id="delivery-steps" class="steps"></div></div></aside>
</div>
<p class="foot">真实执行：本机图片解码、神经网络推理、缺陷分类、置信度门禁、Edge Core API、SQLite Outbox、Worker、MES 回执与设备上下文。模拟部分：图像来自程序合成，下游 MES 与 OPC UA 写回为本地模拟。当前是 PoC+，不是生产视觉验收，也不承诺未知缺陷零漏检。</p>
</div>
<script>
const $=id=>document.getElementById(id);let currentPayload={sample:'SCRATCH'},currentResult=null;
const samples=[['OK','正常'],['SCRATCH','划痕'],['DENT','凹坑'],['STAIN','污渍'],['MISSING','缺件']];
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}function pct(v){return (Number(v)*100).toFixed(1)+'%';}
for(const [code,label] of samples){const b=document.createElement('button');b.className='sample'+(code==='SCRATCH'?' active':'');b.textContent=label;b.onclick=()=>{document.querySelectorAll('.sample').forEach(x=>x.classList.remove('active'));b.classList.add('active');currentPayload={sample:code};classify();};$('samples').appendChild(b);}
function setDecision(result){currentResult=result;const pill=$('decision-pill');pill.className='pill '+(result.decision==='NG'?'ng':result.decision==='OK'?'ok':'review');pill.textContent=result.decision;$('decision-title').textContent=result.predictedLabel;$('decision-reason').textContent=result.decisionReason;$('decision-meta').innerHTML='模型版本：'+esc(result.modelCard.modelVersion)+'<br>推理耗时：'+esc(result.inferenceMs)+' ms<br>图像 SHA-256：'+esc(String(result.imageSha256||'').slice(0,16))+'…<br>哈希口径：'+esc(result.imageHashScope);$('image').src=result.imageDataUrl;$('image').hidden=false;$('image-empty').hidden=true;$('image-tag').hidden=false;$('image-tag').textContent=result.decision+' · '+result.predictedLabel;$('deliver').disabled=result.decision!=='NG';$('deliver').textContent=result.decision==='NG'?'把这条 NG 送入质量闭环':result.decision==='OK'?'OK 样本不生成 MES 工单':'低置信度：转人工复核';$('accuracy').textContent=pct(result.modelCard.validationAccuracy);$('validation').textContent=result.modelCard.validationSamples;$('version').textContent=result.modelCard.modelVersion;const bars=$('bars');bars.innerHTML='';for(const item of result.probabilities){const row=document.createElement('div');row.className='barrow';const n=document.createElement('span');n.textContent=item.label;const t=document.createElement('div');t.className='track';const f=document.createElement('div');f.className='fill';f.style.width=Math.max(0,Math.min(100,item.probability*100))+'%';t.appendChild(f);const v=document.createElement('span');v.textContent=pct(item.probability);row.append(n,t,v);bars.appendChild(row);}const metrics=result.modelCard.validationMetrics||{};const recalls=(metrics.perClass||[]).map(x=>esc(x.label)+' '+pct(x.recall)).join(' · ');const meta=$('model-meta');meta.innerHTML='<div class="modelcard"><h3>'+esc(result.modelCard.modelVersion)+'</h3><div style="font-size:10px;color:var(--muted)">'+esc(result.modelCard.modelType)+'<br>'+esc(result.modelCard.validationScope)+'<br>合成留出集宏平均召回 '+pct(result.modelCard.validationMacroRecall)+'<br>类别召回：'+recalls+'<br>自动判定阈值 '+pct(result.modelCard.reviewThreshold)+'<br>模型哈希：'+esc(String(result.modelCard.modelSha256||'').slice(0,12))+'…</div></div>';const ul=$('limitations');ul.innerHTML='';for(const text of result.modelCard.limitations){const li=document.createElement('li');li.textContent=text;ul.appendChild(li);}}
async function classify(){const button=$('deliver');button.disabled=true;button.textContent='真实推理中…';$('decision-pill').className='pill';$('decision-pill').textContent='推理中';$('decision-title').textContent='模型正在运行';$('decision-reason').textContent='第一次运行会训练小型模型，之后使用内存中的参数。';$('bars').innerHTML='<div class="error spin">神经网络推理中…</div>';try{const res=await fetch('/api/vision/classify',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(currentPayload)});const data=await res.json();if(!res.ok)throw new Error(data.error||('HTTP '+res.status));setDecision(data);}catch(e){$('decision-pill').className='pill';$('decision-pill').textContent='失败';$('decision-title').textContent='推理失败';$('decision-reason').textContent=e.message;button.disabled=true;}}
async function runDelivery(){if(!currentResult||currentResult.decision!=='NG')return;const button=$('deliver');button.disabled=true;button.textContent='正在启动设备上下文与 MES 链路…';$('delivery').style.display='block';$('delivery-summary').innerHTML='<div class="error spin">正在运行联合质量闭环…</div>';$('delivery-steps').innerHTML='';try{const res=await fetch('/api/vision/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(currentPayload)});const data=await res.json();if(!res.ok)throw new Error(data.error||('HTTP '+res.status));if(!data.delivery){$('delivery-summary').textContent=data.message||'本次没有生成 MES 工单。';button.disabled=false;button.textContent='重新运行推理';return;}$('delivery-summary').innerHTML='<span class="pill ok">DONE</span> MES 回执 <b>'+esc(data.delivery.mesReceiptNo)+'</b><br><span style="font-size:10px;color:var(--muted)">correlationId='+esc(data.delivery.correlationId)+'</span>';const box=$('delivery-steps');for(const step of data.delivery.steps){const row=document.createElement('div');row.className='step';row.innerHTML='<b>'+esc(step.number)+'. '+esc(step.title)+'</b><br>'+esc(step.detail);box.appendChild(row);}button.disabled=false;button.textContent='↻ 再运行一次质量闭环';}catch(e){$('delivery-summary').innerHTML='<div class="error">质量闭环失败：'+esc(e.message)+'</div>';button.disabled=false;button.textContent='重新运行质量闭环';}}
$('upload-btn').onclick=()=>{const file=$('file').files[0];if(!file)return;$('decision-pill').textContent='读取图片';const reader=new FileReader();reader.onload=()=>{currentPayload={imageBase64:String(reader.result)};classify();};reader.readAsDataURL(file);};
$('deliver').onclick=runDelivery;
window.addEventListener('load',()=>{const params=new URLSearchParams(location.search);if(params.get('auto')==='1')classify();});
</script>
</body></html>"""
