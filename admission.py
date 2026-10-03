"""Standalone input rules; no imports, state or callbacks from WakePro."""

import re


def normalize(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text).lower())


def guard_exempt(event, config):
    targets = {
        str(event.get_sender_id()),
        str(event.get_group_id()),
        event.unified_msg_origin,
    }
    return bool(
        targets.intersection(str(x) for x in config.get("guard_exempt_targets", []))
    )


def input_decision(event, config, recent_bot_text="", prefixes=()):
    sender, bot = str(event.get_sender_id()), str(event.get_self_id())
    targets = {sender, str(event.get_group_id()), event.unified_msg_origin}
    if sender == bot:
        return "skip", "self_message"
    if targets.intersection(str(x) for x in config.get("blocked_targets", [])):
        return "block", "blocked_target"
    exempt = guard_exempt(event, config)
    parts = event.get_messages()
    plain = str(
        event.get_extra("_gemini_stt_transcript")
        or "".join(
            str(getattr(p, "text", "")) for p in parts if type(p).__name__ == "Plain"
        )
        or event.get_message_str()
        or ""
    ).strip()
    first = plain.split(maxsplit=1)[0] if plain else ""
    command = first.lstrip("/!！")
    if command in {
        "jev状态",
        "jev诊断",
        "jev恢复",
        "jev静音",
        "jev开口",
        "jev拉黑",
        "jev取消拉黑",
        "jev黑名单",
        "拉黑",
        "取消拉黑",
        "黑名单",
    }:
        return "pass", "management_command"
    if not exempt:
        if sender in {str(x) for x in config.get("other_bot_ids", [])}:
            return "block", "known_bot"
        if config.get("block_official_qq_bots", True) and sender.isdigit():
            uid = int(sender)
            if any(
                low <= uid <= high
                for low, high in (
                    (3328144510, 3328144510),
                    (2854196301, 2854216399),
                    (66600000, 66600000),
                    (3889000000, 3889999999),
                    (4010000000, 4019999999),
                )
            ):
                return "block", "official_bot"
        if any(
            word and str(word) in plain for word in config.get("blocked_keywords", [])
        ):
            return "block", "blocked_keyword"
        if (
            config.get("block_repeated_input", True)
            and normalize(plain)
            and recent_bot_text
            and normalize(plain) == normalize(recent_bot_text)
        ):
            return "block", "repeated_bot_message"
        if config.get("block_builtin_commands", False) and command in config.get(
            "blocked_commands", []
        ):
            return "block", "blocked_command"
        if any(prefix and plain.startswith(prefix) for prefix in prefixes):
            registered = bool(event.get_extra("handlers_parsed_params"))
            if config.get(
                "block_prefixed_commands" if registered else "block_prefixed_llm", False
            ):
                return "block", "blocked_prefix"
    at_self = any(type(p).__name__ == "At" and str(p.qq) == bot for p in parts)
    reply_self = any(
        type(p).__name__ == "Reply" and str(getattr(p, "sender_id", "")) == bot
        for p in parts
    )
    directed_other = any(
        type(p).__name__ == "AtAll" or (type(p).__name__ == "At" and str(p.qq) != bot)
        for p in parts
    )
    reply_other = any(
        type(p).__name__ == "Reply" and str(getattr(p, "sender_id", "")) != bot
        for p in parts
    )
    if not (at_self or reply_self) and directed_other:
        return "skip", "addressed_elsewhere"
    if (
        not (at_self or reply_self)
        and reply_other
        and config.get("skip_reply_to_others", False)
    ):
        return "skip", "quoted_elsewhere"
    aliases = [str(n).strip() for n in config.get("wake_names", []) if str(n).strip()]
    if event.is_admin():
        aliases += [
            str(n).strip() for n in config.get("admin_wake_names", []) if str(n).strip()
        ]
    named = False
    remainder = plain
    for alias in sorted(set(aliases), key=len, reverse=True):
        match = re.match(
            r"^"
            + re.escape(alias)
            + r"(?=$|[\s,，:：!?！？。~～]|在|帮|你|能|可以|我|给|怎么|别|不要|闭嘴|停)",
            plain,
            re.IGNORECASE,
        )
        if match:
            named = True
            remainder = plain[match.end() :].strip(" ,，:：!！?？。~～")
            break
    explicit = at_self or reply_self or named
    if explicit and config.get("respect_quiet_requests", True):
        stop = r"(?:闭嘴|别说话|不要说话|别回复|不要回复|别吵|别插嘴|别打扰我|先安静|安静一会|停一下)(?:了|吧|好吗|谢谢)?[。.!！?？\s]*"
        resume = r"(?:可以说话了|继续聊|出来吧|回来吧|解除沉默)[。.!！?？\s]*"
        if re.fullmatch(stop, remainder, re.IGNORECASE):
            return "quiet", "explicit_quiet_request"
        if re.fullmatch(resume, remainder, re.IGNORECASE):
            return "resume", "explicit_resume_request"
    if at_self:
        return "direct", "explicit_at"
    if reply_self and config.get("reply_to_bot_wakes", True):
        return "direct", "reply_to_bot"
    if named:
        return "direct", "named_wake"
    if reply_self and not config.get("reply_to_bot_wakes", True):
        return "skip", "reply_wake_disabled"
    return "pass", "not_explicitly_addressed"
