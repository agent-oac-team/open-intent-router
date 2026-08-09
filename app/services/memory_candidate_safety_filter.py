import re
from dataclasses import dataclass

from app.schemas.memory import MemoryCandidateOperation, MemoryFormationCandidate

_LANGUAGE_ALIASES = {
    "zh": {"chinese", "mandarin", "中文", "汉语", "普通话"},
    "en": {"english", "英文", "英语"},
    "fr": {"french", "français", "francais", "法语", "法文"},
    "es": {"spanish", "español", "espanol", "castellano", "西班牙语"},
    "de": {"german", "deutsch", "德语"},
    "ja": {"japanese", "日本語", "日语"},
    "ko": {"korean", "한국어", "韩语"},
    "pt": {"portuguese", "português", "portugues", "葡萄牙语"},
    "it": {"italian", "italiano", "意大利语"},
    "ru": {"russian", "русский", "俄语"},
    "ar": {"arabic", "العربية", "阿拉伯语"},
}
_LANGUAGE_ALIAS_TO_CODE = {
    alias.casefold(): code for code, aliases in _LANGUAGE_ALIASES.items() for alias in aliases
}
_ISO_639_1_CODES = frozenset(
    "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr "
    "cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv "
    "ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km "
    "kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na "
    "nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc "
    "sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt "
    "tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu".split()
)
_SUPPORTED_LANGUAGE_TAGS = frozenset(
    {
        "ar-sa",
        "de-at",
        "de-ch",
        "de-de",
        "en-au",
        "en-ca",
        "en-gb",
        "en-ie",
        "en-in",
        "en-nz",
        "en-sg",
        "en-us",
        "es-ar",
        "es-co",
        "es-es",
        "es-mx",
        "es-us",
        "fr-be",
        "fr-ca",
        "fr-ch",
        "fr-fr",
        "it-it",
        "ja-jp",
        "ko-kr",
        "pt-br",
        "pt-pt",
        "ru-ru",
        "zh-cn",
        "zh-hk",
        "zh-sg",
        "zh-tw",
    }
)
_QUOTED_SEGMENT_PATTERNS = (
    re.compile(r'"[^"\n]*"'),
    re.compile(r"(?<!\w)'[^'\n]*'(?!\w)"),
    re.compile(r"“[^”\n]*”"),
    re.compile(r"‘[^’\n]*’"),
    re.compile(r"「[^」\n]*」"),
    re.compile(r"『[^』\n]*』"),
    re.compile(r"`[^`\n]*`"),
)
_META_CONTEXT_PATTERNS = (
    re.compile(
        r"(?:记住|保存|写入|写进|加入|引用|复制|记录).*(?:提示词|模板|文档|句子|文字|文本|内容|规则|指令|示例|标题|标签)"
    ),
    re.compile(
        r"(?:提示词|模板|文档|句子|文字|文本|内容|规则|指令|示例|标题|标签).*(?:记住|保存|写入|写进|加入|引用|复制|记录)"
    ),
    re.compile(
        r"(?i)\b(?:remember|store|save|write|insert|quote|copy|include|put)\b.*\b(?:phrase|text|content|rule|instruction|example|prompt|template|document|guide|label|title)\b"
    ),
    re.compile(
        r"(?i)\b(?:phrase|text|content|rule|instruction|example|prompt|template|document|guide|label|title)\b.*\b(?:remember|store|save|write|insert|quote|copy|include|put)\b"
    ),
)
_META_PAYLOAD_PATTERNS = (
    re.compile(
        r"(?:记住|保存|写入|写进|加入|引用|复制|记录)[^。！？\n:：]{0,80}"
        r"(?:提示词|模板|文档|句子|文字|文本|内容|规则|指令|示例|标题|标签)"
        r"\s*(?:[:：,，;；]\s*(?:然后)?|然后)\s*[^。！？.!?\n]*"
    ),
    re.compile(
        r"(?i)\b(?:remember|store|save|write|insert|quote|copy|include|put)\b"
        r"[^.!?;\n:：]{0,80}\b(?:phrase|text|content|rule|instruction|example|prompt|template|document|guide|label|title)\b"
        r"\s*(?:[:：,;]\s*(?:(?:and|then)\s+)?|\s+(?:and|then)\s+)\s*[^.!?\n]*"
    ),
)
_CLAUSE_SEPARATOR = re.compile(
    r"(?:[。！？!?;；\n]|[,，]\s*(?:另外|此外|同时|并且|并)?|\s+(?:and|also|then)\s+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafetyFilterResult:
    downgrade: bool
    filter_id: str | None = None


class TemporaryLanguageSafetyFilter:
    """Known-incident guard. It can only downgrade a candidate, never authorize one."""

    def evaluate(self, candidate: MemoryFormationCandidate) -> SafetyFilterResult:
        semantic = candidate.semantic
        if (
            semantic is None
            or str(candidate.scope) != "user_preference"
            or semantic.slot != "response_language"
            or candidate.proposed_operation
            not in {MemoryCandidateOperation.ADD, MemoryCandidateOperation.UPDATE}
        ):
            return SafetyFilterResult(False)
        expected = _normalized_language(semantic.value)
        user_quotes = [
            ref.quote for ref in candidate.evidence_refs if ref.role == "user" and ref.quote
        ]
        actual = {_instruction_target_language(value) for value in user_quotes}
        actual.discard(None)
        return SafetyFilterResult(
            expected is None or actual != {expected},
            "temporary_response_language_guard",
        )


def _instruction_target_language(value: str) -> str | None:
    aliases = "|".join(
        re.escape(alias) for alias in sorted(_LANGUAGE_ALIAS_TO_CODE, key=len, reverse=True)
    )
    tags = "|".join(
        re.escape(tag) for tag in sorted(_SUPPORTED_LANGUAGE_TAGS, key=len, reverse=True)
    )
    language = rf"({aliases}|{tags}|[a-z]{{2}})(?![a-z0-9-])"
    patterns = (
        re.compile(rf"(?i)(?:使用|用|改用|切换到)\s*{language}\s*(?:来)?(?:回答|回复|答复|回应)"),
        re.compile(
            rf"(?i)(?:回答|回复|答复|回应)\s*(?:时)?\s*(?:请)?\s*(?:使用|用|改用|切换到)\s*{language}"
        ),
        re.compile(rf"(?i)(?:喜欢|偏好|想要)\s*{language}\s*(?:回答|回复|答复|回应)"),
        re.compile(
            rf"(?i)(?:希望|要求)\s*(?:你|系统)?\s*(?:请)?\s*(?:使用|用|给我)?\s*{language}\s*(?:回答|回复|答复|回应)"
        ),
        re.compile(rf"(?i)(?:请)?\s*(?:给我|向我)\s*{language}\s*(?:回答|回复|答复|回应)"),
        re.compile(
            rf"(?i)\b(?:please\s+)?(?:always\s+)?(?:respond|answer|reply)\s+(?:(?:to\s+)?(?:me|us)\s+)?in\s+{language}\b"
        ),
        re.compile(
            rf"(?i)\b(?:prefer|want|request|like)\s+{language}\s+(?:answers?|responses?|replies?)\b"
        ),
        re.compile(
            rf"(?i)\b(?:prefer|want|request|like)\s+(?:answers?|responses?|replies?)\s+(?:in|using)\s+{language}\b"
        ),
        re.compile(
            rf"(?i)\b(?:use|switch to)\s+{language}\s+(?:for\s+)?(?:answers?|responses?|replies?)\b"
        ),
        re.compile(rf"(?i)\b(?:r[eé]ponds?|r[eé]pondez|r[eé]pondre)\s+en\s+{language}\b"),
        re.compile(rf"(?i)\b(?:responde|responda|responded|responder)\s+en\s+{language}\b"),
        re.compile(rf"(?i)\b(?:antworte|antworten)\s+(?:auf|in)\s+{language}\b"),
    )
    evidence_value = _without_quoted_segments(value)
    for pattern in _META_PAYLOAD_PATTERNS:
        evidence_value = pattern.sub(" ", evidence_value)
    clauses = [
        clause
        for clause in _CLAUSE_SEPARATOR.split(evidence_value)
        if clause.strip() and not any(pattern.search(clause) for pattern in _META_CONTEXT_PATTERNS)
    ]
    matches = [
        (clause_index, match.start(1), match.group(1))
        for clause_index, clause in enumerate(clauses)
        for pattern in patterns
        for match in pattern.finditer(clause)
    ]
    return _normalized_language(max(matches)[2]) if matches else None


def _without_quoted_segments(value: str) -> str:
    def replacement(match: re.Match) -> str:
        inner = match.group(0)[1:-1].strip()
        return f" {inner} " if _normalized_language(inner) is not None else " "

    for pattern in _QUOTED_SEGMENT_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _normalized_language(value) -> str | None:
    normalized = str(value or "").strip().casefold()
    if normalized in _LANGUAGE_ALIAS_TO_CODE:
        return _LANGUAGE_ALIAS_TO_CODE[normalized]
    if normalized in _SUPPORTED_LANGUAGE_TAGS:
        return normalized.split("-", 1)[0]
    if normalized in _ISO_639_1_CODES:
        return normalized
    return None


__all__ = ["SafetyFilterResult", "TemporaryLanguageSafetyFilter"]
