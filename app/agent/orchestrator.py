"""Turn router: chat (no tools) vs LLM-first tool loop.

1. Route — `is_chat_only` → no tools; else ReAct run.
2. Intent — `plan_with_agent` once (skill_id + params).
3. Loop — `plan_next_step` → tool → observations → repeat.
4. Harness — confirm_write, deploy ordering, failure recovery (not keyword pipelines).

LLM context is built in `app.agent.context_builder`: current user_goal, trimmed history,
tool observations, and planner_params — not a single concatenated transcript string.
"""

import re

# Pure social turns. If any ops marker is present, this is a tool turn.
_CHAT_EXACT = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "yo",
        "thanks",
        "thank you",
        "ok",
        "okay",
        "سلام",
        "درود",
        "ممنون",
        "مرسی",
        "خوبی",
        "چطوری",
        "چطورى",
        "help",
        "کمک",
    }
)

_OPS_MARKERS = (
    "liara",
    "لیارا",
    "deploy",
    "دیپلوی",
    "disk",
    "دیسک",
    "log",
    "لاگ",
    "balance",
    "موجودی",
    "اعتبار",
    "cpu",
    "domain",
    "دامنه",
    "env",
    "متغیر",
    "restart",
    "ریستارت",
    "plan",
    "پلن",
    "scale",
    "app",
    "اپ",
    "github",
)


def is_chat_only(user_message: str) -> bool:
    """True when the user is greeting or chatting and did not ask for a Liara action."""
    raw = (user_message or "").strip()
    if not raw or len(raw) > 80:
        return False
    low = raw.lower().strip(" !?.،")
    if any(m in low or m in raw for m in _OPS_MARKERS):
        return False
    if low in _CHAT_EXACT:
        return True
    if re.fullmatch(
        r"(hi|hello|hey|سلام|درود)([\s,!.]*)(there|agent|دوست)?",
        low,
        re.I,
    ):
        return True
    if re.search(r"^(who are you|what can you do|چی کار می ?کنی|کی هستی)\b", low):
        return True
    return False
