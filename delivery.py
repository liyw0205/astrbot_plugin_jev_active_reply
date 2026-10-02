"""Event-scoped delivery control; no global platform or Core monkeypatch."""

import copy
from types import SimpleNamespace


class DeliveryDeclined(Exception):
    pass


class EventBotProxy:
    def __init__(self, bot, sender):
        self._bot, self._sender = bot, sender

    def __getattr__(self, name):
        original = getattr(self._bot, name)
        if name in {
            "send",
            "send_msg",
            "send_group_msg",
            "send_private_msg",
            "send_group_forward_msg",
            "send_private_forward_msg",
        }:

            async def guarded(*args, **kwargs):
                return await self._sender(original, *args, **kwargs)

            return guarded
        if name == "call_action":

            async def action(action, *args, **kwargs):
                if str(action).startswith("send_"):
                    return await self._sender(original, action, *args, **kwargs)
                return await original(action, *args, **kwargs)

            return action
        return original


class ScopedContext:
    def __init__(self, original, event):
        self._original, self._event = original, event

    def __getattr__(self, name):
        return getattr(self._original, name)

    async def send_message(self, umo, chain, **kwargs):
        if str(umo) != str(self._event.unified_msg_origin):
            raise DeliveryDeclined("autonomous_cross_session_send")
        return await self._event.send(chain)


class SplitAdapters:
    """Copy only a marked split invocation; other plugin invocations are unchanged."""

    def __init__(self, mark):
        self.mark = mark
        self.installed = []

    def attach(self, plugin):
        if plugin is None:
            return
        for step in getattr(getattr(plugin, "pipeline", None), "_steps", []):
            if type(step).__name__ != "SplitStep" or any(
                item[0] is step for item in self.installed
            ):
                continue
            original = step.handle

            async def scoped(ctx, _step=step, _original=original):
                if not ctx.event.get_extra(self.mark):
                    return await _original(ctx)
                clone = copy.copy(_step)
                clone.plugin_config = copy.copy(_step.plugin_config)
                clone.plugin_config.context = ScopedContext(
                    _step.plugin_config.context, ctx.event
                )
                clone.context = clone.plugin_config.context
                try:
                    return await type(_step).handle(clone, ctx)
                except DeliveryDeclined:
                    ctx.chain.clear()
                    return SimpleNamespace(
                        ok=True, abort=True, msg="Autonomous delivery cancelled"
                    )

            step.handle = scoped
            self.installed.append((step, original, scoped))

    def restore(self):
        for step, original, replacement in self.installed:
            if step.handle is replacement:
                step.handle = original
        self.installed.clear()


class WakeAdapters:
    def __init__(self, managed):
        self.managed = managed
        self.installed = []

    def attach(self, plugin):
        if plugin is None:
            return False
        steps = getattr(getattr(plugin, "pipeline", None), "_steps", [])
        targets = [
            s
            for s in steps
            if type(s).__name__ in ("DebounceStep", "MentionStep", "WakeStep")
        ]
        if len(targets) != 3:
            return False
        for step in targets:
            if any(row[0] is step for row in self.installed):
                continue
            original = step.handle

            async def handle(ctx, _original=original):
                if (
                    not self.managed(ctx.event)
                    or ctx.event.is_at_or_wake_command
                    or getattr(ctx, "cmd", None)
                ):
                    return await _original(ctx)
                muted = (
                    getattr(ctx.group, "shutup_until", 0) > ctx.now
                    or getattr(ctx.member, "silence_until", 0) > ctx.now
                )
                return SimpleNamespace(
                    wake=False if muted else None,
                    abort=muted,
                    prolong=False,
                    msg="Jev owns autonomous waking in this session",
                )

            step.handle = handle
            self.installed.append((step, original, handle))
        return True

    def restore(self):
        for step, original, wrapper in self.installed:
            if step.handle is wrapper:
                step.handle = original
        self.installed.clear()
