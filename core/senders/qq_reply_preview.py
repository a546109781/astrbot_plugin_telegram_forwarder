"""QQ 转发场景下的回复预览辅助函数。

用于把 Telegram 的回复上下文压缩成适合 QQ 展示的简短预览文本，
避免在发送主流程中混入过多“回复引用”相关的细节处理。
"""

from telethon.tl.types import Message

from astrbot.api import logger

from ...common.text_tools import (
    apply_text_replace_rules,
    clean_telegram_text,
    to_telethon_entity,
)


def get_sender_display_name(msg: Message) -> str:
    post_author = getattr(msg, "post_author", None)
    if post_author:
        return str(post_author)
    sender = getattr(msg, "sender", None)
    if not sender:
        return ""
    for attr in ("first_name", "title", "username"):
        value = getattr(sender, attr, None)
        if value:
            return str(value)
    return ""


def reply_media_label(msg: Message) -> str:
    if getattr(msg, "photo", None):
        return "[图片]"
    if getattr(msg, "video", None):
        return "[视频]"
    if getattr(msg, "audio", None) or getattr(msg, "voice", None):
        return "[音频]"
    if getattr(msg, "file", None) or getattr(msg, "document", None):
        return "[文件]"
    return "[消息]"


def build_reply_preview(
    reply_msg: Message,
    strip_links: bool = False,
    text_replace_rules: list | None = None,
) -> str:
    sender_name = get_sender_display_name(reply_msg)
    if getattr(reply_msg, "text", None):
        preview = clean_telegram_text(reply_msg.text, strip_links=strip_links)
        preview = apply_text_replace_rules(preview, text_replace_rules)
        preview = " ".join(part for part in preview.splitlines() if part).strip()
        if len(preview) > 100:
            preview = preview[:100].rstrip() + "..."
    else:
        preview = reply_media_label(reply_msg)
    if not preview:
        preview = reply_media_label(reply_msg)
    if sender_name:
        return f"↩ 回复 {sender_name}:\n{preview}"
    return f"↩ 回复:\n{preview}"


def _reply_fetch_entity(reply_header, default_entity):
    """确定被引用消息所在的会话实体。

    跨会话引用（讨论组回复频道原帖、回复其他频道/群的消息）的
    `reply_to_peer_id` 指向原会话；普通回复没有该字段，回落到当前源频道。
    若忽略该字段、一律从源频道拉取，跨会话引用会取到 None，预览丢失。
    """
    peer = getattr(reply_header, "reply_to_peer_id", None)
    return peer if peer is not None else default_entity


def _peer_group_key(entity) -> tuple | str | int:
    """把会话实体转成可哈希的分组键，避免依赖 Telethon TLObject 的哈希实现。"""
    if isinstance(entity, (str, int)):
        return entity
    for attr in ("channel_id", "chat_id", "user_id"):
        value = getattr(entity, attr, None)
        if value is not None:
            return (type(entity).__name__, value)
    return repr(entity)


async def prefetch_reply_previews(
    *,
    msgs: list[Message],
    src_channel: str,
    client,
    strip_links: bool = False,
    text_replace_rules: list | None = None,
) -> dict[int, str]:
    existing_ids = {getattr(msg, "id", None) for msg in msgs}
    src_entity = to_telethon_entity(src_channel)
    # 按被引用消息所在会话分组：跨会话引用必须去原会话拉取。
    fetch_groups: dict[object, tuple[object, list[int]]] = {}
    for msg in msgs:
        reply_header = getattr(msg, "reply_to", None)
        reply_id = getattr(reply_header, "reply_to_msg_id", None)
        if not reply_id or reply_id in existing_ids:
            continue
        peer_entity = _reply_fetch_entity(reply_header, src_entity)
        _, group_ids = fetch_groups.setdefault(
            _peer_group_key(peer_entity), (peer_entity, [])
        )
        if reply_id not in group_ids:
            group_ids.append(reply_id)

    if not fetch_groups:
        return {}

    if client is None:
        return {}

    preview_cache: dict[int, str] = {}
    for peer_entity, reply_ids in fetch_groups.values():
        try:
            # PeerChannel/PeerUser/PeerChat 可直接作为 entity：
            # Telethon 会经 session 实体缓存解析（讨论组消息的响应里包含
            # 关联频道，正常情况下均已缓存），解析失败则本组降级跳过。
            reply_msgs = await client.get_messages(peer_entity, ids=reply_ids)
        except Exception as e:
            logger.warning(
                f"[QQSender] 获取 reply 预览失败 (peer={peer_entity!r}): {e}"
            )
            continue

        if not reply_msgs:
            continue

        if not isinstance(reply_msgs, list):
            reply_msgs = [reply_msgs]

        for reply_msg in reply_msgs:
            if not reply_msg or getattr(reply_msg, "id", None) is None:
                continue
            preview_cache[reply_msg.id] = build_reply_preview(
                reply_msg,
                strip_links=strip_links,
                text_replace_rules=text_replace_rules,
            )
    return preview_cache
