import asyncio
import base64
import io
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger

JSON_CONTRACT = """
无论上面的自定义规则如何描述，你都必须只返回 JSON 对象，不得使用 Markdown 代码块或附加文字。
JSON 必须且只能包含两个字段：
{"filter": true/false, "msg": "判断原因"}
filter 的含义必须严格遵守：true = 该消息应当被过滤（丢弃、不转发）；false = 该消息无需过滤（放行、正常转发）。
当结论是“内容正常 / 应保留 / 可以转发”时，filter 必须为 false；只有确认需要拦截时才返回 true。
filter 必须是 JSON 布尔值；msg 必须是非空字符串。
""".strip()

# 模型偶发把布尔极性写反：理由明确说“应保留转发/无需过滤”，filter 却给了 true。
# 命中强放行措辞且 filter=true 时按放行处理（fail-open）；反向（false+拦截措辞）
# 不翻转，避免启发式误伤。负向前缀（不/非/勿/没）视为拦截表述，不命中。
_KEEP_VERDICT_RE = re.compile(
    r"(?<![不非勿没])("
    "应保留|应当保留|予以保留|无需过滤|无须过滤|不需要过滤|不应过滤|不该过滤|"
    "不必过滤|可以放行|应放行|予以放行|可以转发|可转发|应转发|应当转发|"
    "正常转发|属于正常内容|无风险内容"
    ")"
)

DEFAULT_AI_FILTER_PROMPT = """判断消息文字和图片是否应该过滤。重点过滤：
1. NSFW、色情、裸露或明显性暗示内容；
2. 二维码推广，尤其是网贷、借款、博彩、色情、诈骗类二维码；
3. 诱导扫码、贷款秒到账、虚假金融推广等风险信息。
正常新闻、普通聊天、技术文档和无风险二维码不要过滤。
""".strip()

DEFAULT_QR_RISK_KEYWORDS = [
    "loan",
    "借款",
    "贷款",
    "网贷",
    "秒到账",
    "博彩",
    "赌博",
    "色情",
    "约炮",
    "裸聊",
]

def parse_ai_decision(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {"filter", "msg"}:
        raise ValueError("AI 返回必须且只能包含 filter 和 msg")
    if not isinstance(value["filter"], bool):
        raise ValueError("filter 必须是布尔值")
    if not isinstance(value["msg"], str) or not value["msg"].strip():
        raise ValueError("msg 必须是非空字符串")
    return {"filter": value["filter"], "msg": value["msg"].strip()}


def build_ai_prompt(custom_prompt: str) -> str:
    prompt = str(custom_prompt or "").strip() or DEFAULT_AI_FILTER_PROMPT
    return f"{prompt}\n\n{JSON_CONTRACT}"


def image_data_url(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
        mime_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def is_risky_qr_payload(payload: str, keywords: list[str]) -> bool:
    text = unquote(str(payload or "")).lower()
    return any(
        str(keyword).strip().lower() in text
        for keyword in keywords
        if str(keyword).strip()
    )


class ContentSafetyFilter:
    # 二维码识别的像素预算：超过软上限先降采样，超过硬上限直接放弃识别，
    # 避免几 KB 的解压炸弹在解码阶段吃掉上 GB 内存。
    QR_MAX_PIXELS = 16_000_000
    QR_MAX_SIDE = 4000
    QR_HARD_MAX_PIXELS = 80_000_000

    def __init__(
        self,
        *,
        qr_decoder: Callable[[bytes], list[str]] | None = None,
    ):
        self.qr_decoder = qr_decoder or self._decode_qr

    @staticmethod
    def _decode_qr(image_bytes: bytes) -> list[str] | None:
        try:
            import zxingcpp
            from PIL import Image
        except ImportError:
            return None
        cls = ContentSafetyFilter
        payloads: list[str] = []
        with Image.open(io.BytesIO(image_bytes)) as image:
            for frame_index in range(min(4, getattr(image, "n_frames", 1))):
                image.seek(frame_index)
                # 尺寸取自文件头，不需要解码；必须在 copy() 之前判断，
                # 否则解压炸弹（几 KB 的 PNG 解出上亿像素）在 copy() 时就已把内存吃满。
                width, height = image.size
                pixels = width * height
                if pixels > cls.QR_HARD_MAX_PIXELS:
                    logger.warning(
                        f"[ContentSafety] 图片 {width}x{height} 像素数过大，跳过二维码识别。"
                    )
                    continue
                if pixels > cls.QR_MAX_PIXELS:
                    # JPEG 能直接按比例降采样解码，省掉一次全尺寸位图分配；
                    # 其它格式 draft() 是空操作，thumbnail() 兜底缩到上限内。
                    try:
                        image.draft("RGB", (cls.QR_MAX_SIDE, cls.QR_MAX_SIDE))
                    except (AttributeError, ValueError):
                        pass
                    frame = image.copy()
                    frame.thumbnail((cls.QR_MAX_SIDE, cls.QR_MAX_SIDE))
                else:
                    frame = image.copy()
                payloads.extend(
                    str(item.text or "") for item in zxingcpp.read_barcodes(frame)
                )
        return list(dict.fromkeys(payloads))

    def check_qr(self, image_bytes: bytes, config: dict) -> dict[str, Any]:
        if not config.get("qr_filter_enabled", False) or not image_bytes:
            return {"filter": False, "msg": "二维码过滤未启用"}
        try:
            payloads = self.qr_decoder(image_bytes)
        except Exception as exc:
            logger.warning(f"[ContentSafety] 二维码识别不可用: {type(exc).__name__}")
            return {"filter": False, "msg": "二维码识别不可用，已放行"}
        if payloads is None:
            logger.warning("[ContentSafety] 二维码识别依赖不可用")
            return {"filter": False, "msg": "二维码识别不可用，已放行"}
        if not payloads:
            return {"filter": False, "msg": "未检测到二维码"}
        mode = str(config.get("qr_filter_mode") or "风险二维码")
        if mode == "全部二维码":
            return {"filter": True, "msg": "检测到二维码"}
        keywords = config.get("qr_risk_keywords") or DEFAULT_QR_RISK_KEYWORDS
        if any(is_risky_qr_payload(payload, keywords) for payload in payloads):
            return {"filter": True, "msg": "二维码命中风险规则"}
        return {"filter": False, "msg": "二维码未命中风险规则"}

    async def check(
        self,
        text: str,
        image_bytes: bytes | None,
        config: dict,
        provider: Any = None,
    ) -> dict[str, Any]:
        qr_result = await asyncio.to_thread(self.check_qr, image_bytes or b"", config)
        if qr_result["filter"]:
            return qr_result
        return await self.check_ai(text, image_bytes, config, provider)

    async def check_ai(
        self,
        text: str,
        image_bytes: bytes | None,
        config: dict,
        provider: Any = None,
    ) -> dict[str, Any]:
        if not config.get("ai_filter_enabled", False):
            return {"filter": False, "msg": "AI 过滤未启用"}
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return {"filter": False, "msg": "AI 模型服务不可用（未配置 Provider），已放行"}
        model = str(config.get("ai_filter_model") or "").strip()

        async def call_model() -> Any:
            kwargs: dict[str, Any] = {
                "prompt": text or "(无文字)",
                "contexts": [],
                "func_tool": None,
                "system_prompt": build_ai_prompt(config.get("ai_filter_prompt", "")),
            }
            if image_bytes:
                kwargs["image_urls"] = [image_data_url(image_bytes)]
            if model:
                kwargs["model"] = model
            try:
                return await provider.text_chat(**kwargs)
            except TypeError:
                # 旧版 Provider 的 text_chat 不支持 model/func_tool 等参数，降级重试
                kwargs.pop("model", None)
                kwargs.pop("func_tool", None)
                return await provider.text_chat(**kwargs)

        try:
            timeout_seconds = min(
                60, max(1, int(config.get("ai_filter_timeout") or 20))
            )
        except (TypeError, ValueError):
            timeout_seconds = 20
        try:
            response = await asyncio.wait_for(call_model(), timeout=timeout_seconds)
            raw = str(getattr(response, "completion_text", "") or "")
            decision = parse_ai_decision(raw)
            if decision["filter"] and _KEEP_VERDICT_RE.search(decision["msg"]):
                logger.warning(
                    "[ContentSafety] AI 判定自相矛盾（filter=true 但理由为放行），"
                    f"按放行处理: {decision['msg']}"
                )
                return {
                    "filter": False,
                    "msg": f"AI 布尔与结论矛盾，已按放行处理（{decision['msg']}）",
                }
            return decision
        except asyncio.TimeoutError:
            logger.warning(f"[ContentSafety] AI 过滤超时（{timeout_seconds}s），已放行")
            return {"filter": False, "msg": "AI 过滤超时，已放行"}
        except Exception as exc:
            logger.warning(f"[ContentSafety] AI 过滤不可用: {type(exc).__name__}")
            return {"filter": False, "msg": "AI 过滤不可用，已放行"}
