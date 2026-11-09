"""Conservative title prefilter; no network, paid calls or user-status changes."""

import re

TARGET_ROLE = re.compile(
    r"\b(?:AI|ML|LLM|NLP|VLM|MLOps|LLMOps)\b|machine\s+learning|deep\s+learning|"
    r"data\s+scientist|computer\s+vision|speech\s+(?:engineer|scientist)|"
    r"машинн\w*\s+обучен|компьютерн\w*\s+зрен|искусственн\w*\s+интеллект|"
    r"\bИИ\b|дата[-\s]*сай[еэ]нтист", re.IGNORECASE,
)
OTHER_ROLE = re.compile(
    r"project\s+manager|product\s+(?:manager|owner)|business\s+analyst|"
    r"fraud\s+analyst|антифрод|проектн\w*\s+менеджер|бизнес[-\s]*аналитик|"
    r"руководител|начальник|директор|\b(?:senior|lead|head|principal|staff)\b|"
    r"старш\w*|ведущ\w*|data\s+analyst|data\s+label|annotat|разметчик|"
    r"\bdirector\b|engineering\s+manager|technical\s+program\s+manager|\bAI\s+creator\b", re.IGNORECASE,
)


def title_is_relevant(title: str) -> bool:
    """Broad AI/ML titles only; generic Python/analyst roles need separate review."""
    return bool(TARGET_ROLE.search(title)) and not OTHER_ROLE.search(title)
