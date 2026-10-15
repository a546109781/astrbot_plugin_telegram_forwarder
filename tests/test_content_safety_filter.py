import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "filters" / "content_safety.py"
MODULE_NAME = "astrbot_plugin_telegram_forwarder.core.filters.content_safety"


def load_module():
    astrbot_api = SimpleNamespace(logger=MagicMock())
    with patch.dict(
        sys.modules,
        {
            "astrbot": MagicMock(),
            "astrbot.api": astrbot_api,
        },
    ):
        sys.modules.pop(MODULE_NAME, None)
        spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def test_parse_ai_decision_accepts_strict_json():
    module = load_module()

    assert module.parse_ai_decision('{"filter": true, "msg": "NSFW 图片"}') == {
        "filter": True,
        "msg": "NSFW 图片",
    }
    assert module.parse_ai_decision('{"filter": false, "msg": "普通内容"}') == {
        "filter": False,
        "msg": "普通内容",
    }


def test_parse_ai_decision_accepts_markdown_json_fence():
    module = load_module()

    assert module.parse_ai_decision(
        '```json\n{"filter": true, "msg": "NSFW 图片"}\n```'
    ) == {"filter": True, "msg": "NSFW 图片"}


@pytest.mark.parametrize(
    "raw",
    [
        '判断如下：{"filter": true, "msg": "bad"}',
        '{"filter": "true", "msg": "bad"}',
        '{"filter": true}',
        "not json",
    ],
)
def test_parse_ai_decision_rejects_non_strict_output(raw):
    module = load_module()

    with pytest.raises(ValueError):
        module.parse_ai_decision(raw)


def test_ai_prompt_always_appends_json_contract():
    module = load_module()

    prompt = module.build_ai_prompt("自定义判断标准")

    assert prompt.startswith("自定义判断标准")
    assert "只返回 JSON 对象" in prompt
    assert '"filter": true/false' in prompt
    assert '"msg": "判断原因"' in prompt


def test_image_data_url_uses_detected_mime_type():
    module = load_module()

    assert module.image_data_url(b"\x89PNG\r\n\x1a\nrest").startswith(
        "data:image/png;base64,"
    )
    assert module.image_data_url(b"\xff\xd8\xffrest").startswith(
        "data:image/jpeg;base64,"
    )


def test_qr_risk_classification():
    module = load_module()

    assert module.is_risky_qr_payload(
        "https://loan.example/apply?channel=fast", ["loan", "借款"]
    )
    assert module.is_risky_qr_payload("立即借款，秒到账", ["loan", "借款"])
    assert module.is_risky_qr_payload(
        "https://example.com/?q=%E7%BD%91%E8%B4%B7", ["网贷"]
    )
    assert not module.is_risky_qr_payload("https://example.org/docs", ["loan", "借款"])


def test_local_qr_filter_modes():
    module = load_module()
    detector = MagicMock(return_value=["https://example.org/docs"])
    content_filter = module.ContentSafetyFilter(qr_decoder=detector)

    all_result = content_filter.check_qr(
        b"image", {"qr_filter_enabled": True, "qr_filter_mode": "全部二维码"}
    )
    risk_result = content_filter.check_qr(
        b"image",
        {
            "qr_filter_enabled": True,
            "qr_filter_mode": "风险二维码",
            "qr_risk_keywords": ["loan", "借款"],
        },
    )

    assert all_result == {"filter": True, "msg": "检测到二维码"}
    assert risk_result == {"filter": False, "msg": "二维码未命中风险规则"}


def test_qr_decoder_failure_is_fail_open():
    module = load_module()
    content_filter = module.ContentSafetyFilter(
        qr_decoder=MagicMock(side_effect=ValueError("broken image"))
    )

    result = content_filter.check_qr(
        b"broken", {"qr_filter_enabled": True, "qr_filter_mode": "全部二维码"}
    )

    assert result == {"filter": False, "msg": "二维码识别不可用，已放行"}


def test_qr_dependency_missing_is_reported_as_unavailable(monkeypatch):
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    monkeypatch.setitem(sys.modules, "zxingcpp", None)

    result = content_filter.check_qr(
        b"image", {"qr_filter_enabled": True, "qr_filter_mode": "全部二维码"}
    )

    assert result == {"filter": False, "msg": "二维码识别不可用，已放行"}


class RecordingFrame:
    def __init__(self, size, calls):
        self.size = size
        self.calls = calls

    @property
    def width(self):
        return self.size[0]

    @property
    def height(self):
        return self.size[1]

    def thumbnail(self, box):
        self.calls.append(("thumbnail", box))
        self.size = (min(self.size[0], box[0]), min(self.size[1], box[1]))


class RecordingImage:
    """记录 seek/draft/copy 调用顺序的假 PIL Image。"""

    def __init__(self, size, calls, n_frames=1):
        self.size = size
        self.calls = calls
        self.n_frames = n_frames

    @property
    def width(self):
        return self.size[0]

    @property
    def height(self):
        return self.size[1]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def seek(self, index):
        self.calls.append(("seek", index))

    def draft(self, _mode, box):
        self.calls.append(("draft", box))
        self.size = (min(self.size[0], box[0]), min(self.size[1], box[1]))

    def copy(self):
        self.calls.append(("copy", self.size))
        return RecordingFrame(self.size, self.calls)


def install_fake_imaging(monkeypatch, image):
    pil = ModuleType("PIL")
    pil_image = ModuleType("PIL.Image")
    pil_image.open = lambda _fh: image
    pil.Image = pil_image
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", pil_image)

    zxing = ModuleType("zxingcpp")
    zxing.read_barcodes = lambda _frame: [SimpleNamespace(text="https://example.org")]
    monkeypatch.setitem(sys.modules, "zxingcpp", zxing)


def test_qr_decode_normal_image_copies_without_downscale(monkeypatch):
    module = load_module()
    calls = []
    install_fake_imaging(monkeypatch, RecordingImage((1000, 1000), calls))

    payloads = module.ContentSafetyFilter._decode_qr(b"image")

    assert payloads == ["https://example.org"]
    assert [name for name, _ in calls] == ["seek", "copy"]


def test_qr_decode_checks_pixel_budget_before_copying_frame(monkeypatch):
    """像素数判断必须早于 copy()，否则解压炸弹已经把内存吃掉了。"""
    module = load_module()
    calls = []
    # 5000x4000 = 2000 万像素，超过 QR_MAX_PIXELS 软上限。
    install_fake_imaging(monkeypatch, RecordingImage((5000, 4000), calls))

    payloads = module.ContentSafetyFilter._decode_qr(b"image")

    assert payloads == ["https://example.org"]
    assert [name for name, _ in calls] == ["seek", "draft", "copy", "thumbnail"]
    # draft 已把待解码尺寸压到上限内，copy 不再分配全尺寸位图。
    copied_size = next(size for name, size in calls if name == "copy")
    assert max(copied_size) <= module.ContentSafetyFilter.QR_MAX_SIDE


def test_qr_decode_skips_frames_over_hard_pixel_limit(monkeypatch):
    module = load_module()
    calls = []
    # 10000x10000 = 1 亿像素，超过 QR_HARD_MAX_PIXELS。
    install_fake_imaging(monkeypatch, RecordingImage((10000, 10000), calls))

    payloads = module.ContentSafetyFilter._decode_qr(b"image")

    assert payloads == []
    assert [name for name, _ in calls] == ["seek"]


def test_qr_decode_survives_image_without_draft(monkeypatch):
    module = load_module()
    calls = []
    image = RecordingImage((5000, 4000), calls)

    def _no_draft(*_args, **_kwargs):
        raise AttributeError("draft unsupported")

    image.draft = _no_draft
    install_fake_imaging(monkeypatch, image)

    payloads = module.ContentSafetyFilter._decode_qr(b"image")

    assert payloads == ["https://example.org"]
    assert [name for name, _ in calls] == ["seek", "copy", "thumbnail"]


def test_combined_filter_short_circuits_ai_when_qr_matches():
    module = load_module()
    content_filter = module.ContentSafetyFilter(
        qr_decoder=lambda _: ["https://loan.example/apply"]
    )
    content_filter.check_ai = AsyncMock(
        return_value={"filter": False, "msg": "AI 放行"}
    )
    config = {
        "qr_filter_enabled": True,
        "qr_filter_mode": "风险二维码",
        "qr_risk_keywords": ["loan"],
        "ai_filter_enabled": True,
    }

    result = asyncio.run(content_filter.check("text", b"image", config))

    assert result == {"filter": True, "msg": "二维码命中风险规则"}
    content_filter.check_ai.assert_not_awaited()


def test_combined_filter_uses_ai_after_qr_passes():
    module = load_module()
    content_filter = module.ContentSafetyFilter(qr_decoder=lambda _: [])
    content_filter.check_ai = AsyncMock(
        return_value={"filter": True, "msg": "AI 判断为 NSFW"}
    )
    config = {
        "qr_filter_enabled": True,
        "ai_filter_enabled": True,
    }

    result = asyncio.run(content_filter.check("text", b"image", config))

    assert result == {"filter": True, "msg": "AI 判断为 NSFW"}
    content_filter.check_ai.assert_awaited_once_with("text", b"image", config, None)


class _ProviderResponse:
    def __init__(self, text):
        self.completion_text = text


class _Provider:
    def __init__(self, ret=None, exc=None, sleep=0.0, reject_model=False):
        self.ret, self.exc, self.sleep, self.reject_model = ret, exc, sleep, reject_model
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.reject_model and "model" in kwargs:
            raise TypeError("unexpected keyword argument 'model'")
        if self.exc:
            raise self.exc
        return _ProviderResponse(self.ret)


_BASE_AI_CONFIG = {
    "ai_filter_enabled": True,
    "ai_filter_model": "vision-model",
    "ai_filter_prompt": "过滤不良推广",
    "ai_filter_timeout": 10,
}


def test_ai_uses_provider_text_chat_with_contract_and_image():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(ret='{"filter": true, "msg": "网贷二维码"}')

    result = asyncio.run(
        content_filter.check_ai("消息正文", b"jpeg", dict(_BASE_AI_CONFIG), provider)
    )

    assert result == {"filter": True, "msg": "网贷二维码"}
    kwargs = provider.calls[0]
    assert kwargs["prompt"] == "消息正文"
    assert kwargs["model"] == "vision-model"
    assert kwargs["contexts"] == []
    assert kwargs["system_prompt"].startswith("过滤不良推广")
    assert "只返回 JSON 对象" in kwargs["system_prompt"]
    assert kwargs["image_urls"][0].startswith("data:image/jpeg;base64,")


def test_ai_omits_model_kwarg_when_unset():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(ret='{"filter": false, "msg": "放行"}')
    config = dict(_BASE_AI_CONFIG)
    config["ai_filter_model"] = ""

    asyncio.run(content_filter.check_ai("text", None, config, provider))

    assert "model" not in provider.calls[0]


def test_ai_model_kwarg_falls_back_when_provider_rejects_it():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(
        ret='{"filter": false, "msg": "放行"}', reject_model=True
    )

    result = asyncio.run(
        content_filter.check_ai("text", None, dict(_BASE_AI_CONFIG), provider)
    )

    assert result == {"filter": False, "msg": "放行"}
    assert "model" not in provider.calls[-1]


def test_ai_missing_provider_is_fail_open():
    module = load_module()
    content_filter = module.ContentSafetyFilter()

    result = asyncio.run(
        content_filter.check_ai("text", None, dict(_BASE_AI_CONFIG), None)
    )

    assert result == {"filter": False, "msg": "AI 模型服务不可用（未配置 Provider），已放行"}


def test_ai_provider_failure_is_fail_open():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(exc=RuntimeError("provider offline"))

    result = asyncio.run(
        content_filter.check_ai("text", None, dict(_BASE_AI_CONFIG), provider)
    )

    assert result == {"filter": False, "msg": "AI 过滤不可用，已放行"}


def test_ai_timeout_is_fail_open():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(sleep=5.0)
    config = dict(_BASE_AI_CONFIG)
    config["ai_filter_timeout"] = 1

    result = asyncio.run(content_filter.check_ai("text", None, config, provider))

    assert result == {"filter": False, "msg": "AI 过滤超时，已放行"}


def test_ai_invalid_timeout_uses_default_instead_of_crashing():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(ret='{"filter": false, "msg": "放行"}')
    config = dict(_BASE_AI_CONFIG)
    config["ai_filter_timeout"] = "invalid"

    result = asyncio.run(content_filter.check_ai("text", None, config, provider))

    assert result == {"filter": False, "msg": "放行"}


def test_ai_malformed_provider_output_is_fail_open():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(ret="这不是 JSON")

    result = asyncio.run(
        content_filter.check_ai("text", None, dict(_BASE_AI_CONFIG), provider)
    )

    assert result == {"filter": False, "msg": "AI 过滤不可用，已放行"}


def test_ai_disabled_skips_provider_call():
    module = load_module()
    content_filter = module.ContentSafetyFilter()
    provider = _Provider(ret='{"filter": true, "msg": "不应被调用"}')
    config = dict(_BASE_AI_CONFIG)
    config["ai_filter_enabled"] = False

    result = asyncio.run(content_filter.check_ai("text", None, config, provider))

    assert result == {"filter": False, "msg": "AI 过滤未启用"}
    assert provider.calls == []
