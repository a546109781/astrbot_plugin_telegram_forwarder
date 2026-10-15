import re

from astrbot.api import logger

# 替换规则字符串中「匹配内容」与「替换为」的分隔符
TEXT_REPLACE_SEPARATOR = "=>"
# 正则规则前缀（命中任一前缀即视为正则替换，其余按普通文本替换处理）
TEXT_REPLACE_REGEX_PREFIXES = ("regex:", "正则:")


def normalize_telegram_channel_name(raw: str) -> str:
    """标准化频道用户名、t.me 链接或数字 ID。

    支持：
    - 用户名：@channel → channel
    - URL：https://t.me/channel → channel
    - 数字 ID：#-3791533773 → -3791533773, -3791533773 → -3791533773
    """
    text = (raw or "").strip()
    if not text:
        return ""

    # 处理 # 前缀的数字频道 ID，避免被后续 URL fragment 剥离逻辑误删
    # 例如 #-3791533773 → -3791533773
    if text.startswith("#") and len(text) > 1 and (text[1] == "-" or text[1].isdigit()):
        text = text[1:]

    text = text.lstrip("@").strip()
    lower = text.lower()
    if lower.startswith("https://t.me/") or lower.startswith("http://t.me/"):
        text = text.split("://", 1)[1]
    if text.lower().startswith("t.me/"):
        text = text.split("/", 1)[1]

    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if "/" in text:
        text = text.split("/", 1)[0]
    return text.lstrip("@").strip()


def is_numeric_channel_id(name: str) -> bool:
    """判断频道名是否为数字 ID（而非用户名）。"""
    return bool(name) and name.lstrip("-").isdigit()


def to_telethon_entity(channel_name: str) -> str | int:
    """将存储的频道名转换为 Telethon 可解析的实体标识符。

    Telethon 解析数字 ID 需要整数类型，用户名则需要字符串。
    此函数将数字型频道名字符串（如 '-3791533773'）转为 int，
    用户名保持字符串不变。

    Args:
        channel_name: 存储的频道标识符（用户名或数字 ID 字符串）

    Returns:
        数字 ID 返回 int，用户名返回 str
    """
    if not channel_name:
        return channel_name
    stripped = channel_name.lstrip("-")
    if stripped.isdigit():
        return int(channel_name)
    return channel_name


def clean_telegram_text(text: str, strip_links: bool = False) -> str:
    """清洗 Telegram 消息文本"""
    if not text:
        return ""

    # 1. 移除特定的频道签名
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if "频道" in line and "@" in line:
            continue
        if line.strip().startswith("@") and len(line) < 20:
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    # 2. 正则内容清洗
    patterns = [
        r"[\*＊\-]?\s*此原图经过处理.*",
        r"投稿 by .*",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # 3. 去除粗体/斜体标记（可选保留，根据需求）
    text = text.replace("**", "").replace("__", "")

    # 4. 处理 Markdown 链接  ← 这里是重点修改
    if strip_links:
        # 只保留 [文本] 部分，丢弃 (链接)
        text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    else:
        # 原有行为：显示 文本: 链接
        text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1: \2", text)

    return text.strip()


def parse_text_replace_rules(raw_rules: object) -> list[dict]:
    """把配置中的替换规则字符串列表解析为结构化规则。

    规则格式（一条一行）：
    - 普通替换：``匹配内容 => 替换为``
    - 正则替换：``regex:正则表达式 => 替换为`` 或 ``正则:正则表达式 => 替换为``

    匹配内容中不能包含 ``=>``（按第一个 => 分隔）；替换为可为空（表示删除匹配内容）。
    正则规则的替换为支持 Python re 反向引用（如 ``\\1``）。
    非法条目会被跳过并记录日志，不影响其他规则。

    Args:
        raw_rules: 配置里的规则列表（容忍 list/str/None 等各种形态）。

    Returns:
        结构化规则列表 ``[{"type": "plain"|"regex", "pattern": str, "replacement": str}]``，
        保持配置中的先后顺序。
    """
    if not raw_rules:
        return []
    if isinstance(raw_rules, str):
        raw_rules = raw_rules.splitlines()
    if not isinstance(raw_rules, (list, tuple)):
        return []

    rules: list[dict] = []
    for raw in raw_rules:
        line = str(raw or "").strip()
        if not line:
            continue

        rule_type = "plain"
        lowered = line.lower()
        for prefix in TEXT_REPLACE_REGEX_PREFIXES:
            if lowered.startswith(prefix):
                rule_type = "regex"
                line = line[len(prefix) :].strip()
                break

        if TEXT_REPLACE_SEPARATOR not in line:
            logger.warning(
                f"[TextReplace] 替换规则缺少 '{TEXT_REPLACE_SEPARATOR}' 分隔符，已跳过: {raw!r}"
            )
            continue

        pattern, replacement = line.split(TEXT_REPLACE_SEPARATOR, 1)
        pattern = pattern.strip()
        replacement = replacement.strip()
        if not pattern:
            logger.warning(f"[TextReplace] 替换规则匹配内容为空，已跳过: {raw!r}")
            continue

        if rule_type == "regex":
            try:
                re.compile(pattern)
            except re.error as e:
                logger.error(
                    f"[TextReplace] 替换规则正则表达式非法，已跳过: {pattern!r} ({e})"
                )
                continue

        rules.append(
            {"type": rule_type, "pattern": pattern, "replacement": replacement}
        )
    return rules


def apply_text_replace_rules(
    text: str,
    rules: list[dict] | None,
    rule_reports: list | None = None,
) -> str:
    """按顺序对文本应用替换净化规则。

    正则规则默认以 ``re.MULTILINE`` 多行模式执行：``^`` / ``$`` 匹配行首行尾，
    需要匹配整段文本首尾时使用 ``\\A`` / ``\\Z``。

    Args:
        text: 待处理的文本（通常是 ``clean_telegram_text`` 清洗后的结果）。
        rules: ``parse_text_replace_rules`` 解析出的结构化规则。
        rule_reports: 可选的输出列表；传入时每条规则会追加一份命中报告
            ``{"type", "pattern", "replacement", "matched", "count", "error"}``，
            供 Web 预览调试使用。生产路径不传，行为与之前完全一致。

    Returns:
        处理后的文本；规则为空或执行异常时原样返回。
    """
    if not text or not rules:
        return text

    for rule in rules:
        rule_type = rule.get("type", "plain")
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        if not pattern:
            continue
        report = None
        if rule_reports is not None:
            report = {
                "type": rule_type,
                "pattern": pattern,
                "replacement": replacement,
                "matched": False,
                "count": 0,
                "error": "",
            }
            rule_reports.append(report)
        try:
            if rule_type == "regex":
                # 多行模式：频道消息是多行文本，^ / $ 按行首行尾匹配更符合
                # “按行净化”的常见意图；需要整段首尾时可用 \A \Z。
                if report is not None:
                    try:
                        report["count"] = len(
                            re.findall(pattern, text, flags=re.MULTILINE)
                        )
                    except re.error:
                        report["count"] = 0
                text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
            else:
                if report is not None:
                    report["count"] = text.count(pattern)
                text = text.replace(pattern, replacement)
            if report is not None:
                report["matched"] = report["count"] > 0
        except re.error as e:
            # 解析阶段已校验过正则；此处兜底捕获替换串中的非法转义（如 \q）
            logger.error(
                f"[TextReplace] 应用替换规则失败，已跳过: {pattern!r} ({e})"
            )
            if report is not None:
                report["error"] = str(e)
            continue
    return text
