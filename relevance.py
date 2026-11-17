"""Conservative title prefilter; no network, paid calls or user-status changes."""

import re

TARGET_ROLE = re.compile(
    r"\b(?:AI|ML|LLM|NLP|VLM|MLOps|LLMOps)\b|machine\s+learning|deep\s+learning|"
    r"data\s+scientist|computer\s+vision|speech\s+(?:engineer|scientist)|"
    r"машинн\w*\s+обучен|компьютерн\w*\s+зрен|искусственн\w*\s+интеллект|"
    r"\bИИ\b|дата[-\s]*сай[еэ]нтист|нейросет\w*|нейронн\w*\s+сет|"
    r"data\s+scien\w*|\bgen\s*ai\b|\bRAG\b|prompt\s+engineer\w*", re.IGNORECASE,
)
OTHER_ROLE = re.compile(
    r"project\s+manager|product\s+(?:manager|owner)|business\s+analyst|"
    r"fraud\s+analyst|антифрод|проектн\w*\s+менеджер|бизнес[-\s]*аналитик|"
    r"руководител|начальник|директор|\b(?:senior|lead|head|principal|staff)\b|"
    r"старш\w*|ведущ\w*|data\s+analyst|data\s+label|annotat|разметчик|"
    r"\bdirector\b|engineering\s+manager|technical\s+program\s+manager|\bAI\s+creator\b|"
    # Never an engineering role, whatever else the title says.
    r"продаж\w*|\bsales\b|presale\w*|юрист\w*|lawyer|экономист\w*|бухгалт\w*|преподават\w*|учител\w*|teacher|"
    r"тренер\w*|лектор\w*|спикер\w*|\bQA\b|тестировщ\w*|рекрутер\w*|recruit\w*|копирайт\w*|copywrit\w*|"
    r"\bSMM\b|shopify", re.IGNORECASE,
)
# A bare "AI"/"ИИ" is in many non-engineering titles (AI marketing, content, design, support). These words also
# occur in engineering titles («ML Engineer, Content Understanding»), so they reject only titles without one.
NOISE_ROLE = re.compile(
    r"маркет\w*|market\w*|\bSEO\b|\bGEO\b|контент\w*|content|креат\w*|creat\w*|дизайн\w*|design\w*|"
    r"видео(?:мейкер|креатор|монтаж|граф)\w*|монтаж\w*|менеджер\w*|manager|финанс\w*|financ\w*|legal|"
    r"методолог\w*|methodolog\w*|ассистент\w*|assistant|оператор\w*|поддержк\w*|support", re.IGNORECASE,
)
ENGINEERING_ROLE = re.compile(
    r"engineer|инженер\w*|разработчик\w*|developer|scientist|researcher|исследовател\w*|программист\w*",
    re.IGNORECASE,
)


def title_is_relevant(title: str) -> bool:
    """Broad AI/ML titles only; generic Python/analyst roles need separate review."""
    if not TARGET_ROLE.search(title) or OTHER_ROLE.search(title):return False
    return not NOISE_ROLE.search(title) or bool(ENGINEERING_ROLE.search(title))
