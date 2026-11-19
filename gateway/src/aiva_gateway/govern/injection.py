"""Prompt-injection detection (LLD §7 prompt/response domain, §17).

Detectors return indicator names. On the prompt they feed the `injection_detected` policy fact
(GCT-INJ-003 denies); on evidence they are recorded and only restrict (evidence is data, never
instructions). See ADR-0011 for the benchmark that selects the production detector.
"""

import base64
import binascii
import re
import unicodedata
from typing import Protocol


class InjectionDetector(Protocol):
    version: str

    def detect(self, text: str) -> tuple[str, ...]: ...


PATTERNS_V1: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override-instructions",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\b.{0,30}\b(?:previous|prior|above|earlier|all"
            r"|system)\b.{0,20}\b(?:instructions?|prompts?|rules?)"
        ),
    ),
    (
        "reveal-system-prompt",
        re.compile(
            r"(?i)\b(?:reveal|show|print|repeat)\b.{0,20}"
            r"\b(?:system prompt|hidden instructions?|your instructions)"
        ),
    ),
    (
        "role-hijack",
        re.compile(r"(?i)\byou are now\b.{0,30}\b(?:developer mode|dan|unrestricted|jailbroken)"),
    ),
    (
        "bulk-exfiltration",
        re.compile(
            r"(?i)\b(?:dump|print|list|export|show)\b.{0,20}\b(?:all|every|entire)\b.{0,40}\b(?:msisdns?"
            r"|subscribers?|customers?|passwords?|credentials?|records?)"
        ),
    ),
    ("exfiltration-channel", re.compile(r"(?i)\b(?:send|post|upload|forward)\b.{0,40}\bhttps?://")),
)


class PatternInjectionDetector:
    """P0 baseline: five regular expressions over the raw text."""

    version = "injection-patterns-v1"

    def detect(self, text: str) -> tuple[str, ...]:
        return tuple(name for name, pattern in PATTERNS_V1 if pattern.search(text))


# --- v2: normalised views + broader indicators (ADR-0011) ---------------------------------------

# Common Cyrillic/Greek look-alikes of Latin letters (a subset of Unicode confusables), by code point.
_CONFUSABLES = {
    0x0430: "a",
    0x0435: "e",
    0x043E: "o",
    0x0440: "p",
    0x0441: "c",
    0x0443: "y",
    0x0445: "x",
    0x0456: "i",
    0x0458: "j",
    0x0455: "s",
    0x0501: "d",
    0x0261: "g",
    0x0491: "g",
    0x2170: "i",
    0x03BF: "o",
    0x03B1: "a",
    0x03B5: "e",
    0x03B9: "i",
    0x03BA: "k",
    0x03BD: "v",
    0x03C4: "t",
    0x03C5: "u",
    0x03C1: "p",
}
# Dashes and curly quotes, so patterns only need ASCII punctuation.
_PUNCTUATION = {
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0x2212: "-",
    0x2018: "'",
    0x2019: "'",
    0x201C: '"',
    0x201D: '"',
}
_NORMALISE = str.maketrans({**_CONFUSABLES, **_PUNCTUATION})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SPACED = re.compile(r"\b(?:[A-Za-z][ \t]){3,}[A-Za-z]\b")
_MIXED_TOKEN = re.compile(r"\b(?=\w*[A-Za-z])(?=\w*[013457])[\w@$]{3,}\b")
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{16,}={0,2}(?![A-Za-z0-9+/=])")

_RULES = (
    r"(?:instructions?|prompts?|rules?|directives?|guidelines?|polic(?:y|ies)|constraints?|restrictions?"
    r"|filters?|guardrails?|programming|safeguards?|limitations?|safety|directions)"
)
_SENSITIVE_DATA = (
    r"(?:(?:subscriber|customer|user|employee|client|account)s?'?\s*(?:records?|data(?:base)?|details|"
    r"list|profiles?|accounts?|phone numbers|names|directory|pii|personal)|credentials?|passwords?|api keys?|"
    r"(?:auth(?:entication)?|session|login|access) tokens?|secrets?|msisdns?|imsis?|database (?:dump|backup)|"
    r"(?:entire|whole|full|complete) (?:\w+ )?database|personal (?:details|data|emails?|information)|"
    r"transaction logs?|call recordings?|employee directory|subscriber ids?|phone numbers|sim ids?|iccids?|"
    r"backup of the (?:\w+ ){0,3}database)"
)

PATTERNS_V2: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override-instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass|set aside|suspend|abandon|drop|discard)\b"
            rf"[^.!?\n]{{0,40}}\b{_RULES}"
            r"|\b(?:forget|ignore|disregard)\s+(?:everything|anything|all)\s+(?:you(?:'ve| have| were)?\s+"
            r"(?:been\s+)?(?:told|taught|instructed)|above|before|so far)"
        ),
    ),
    (
        "reveal-system-prompt",
        re.compile(
            r"\b(?:reveal|show|print|repeat|disclose|output|display|tell me|give me|list|share|leak|dump"
            r"|what (?:are|is))\b"
            r"[^.!?\n]{0,40}\b(?:system (?:prompt|message)|hidden (?:rules?|instructions?|instruction set)|"
            r"(?:internal|core|initial|original|secret) (?:system prompt|instructions?|rules?|guardrails?"
            r"|directives?)|"
            r"your (?:instructions?|rules?|guidelines|configuration|guardrails?|directives?|programming"
            r"|system prompt)|"
            r"(?:policy|exact) directives you|instructions? you (?:were|are) given)"
            r"|\b(?:you were|you've been|you have been|you are)\s+(?:programmed|instructed|configured|"
            r"pre-?prompted)\s+(?:with|to)\b"
            r"|\b(?:defines?|governs?|controls?)\s+your\s+(?:behaviou?r|responses|content filters|limits)"
        ),
    ),
    (
        "role-hijack",
        re.compile(
            r"(?:\b(?:switch to|enter|activate|enable|turn on|go into)\s+(?:the\s+)?['\"]?(?:developer|god"
            r"|admin|"
            r"superuser|sudo|dan|jailbreak|unrestricted|unfiltered|debug|root)['\"]?\s+(?:mode|persona)\b"
            r"(?![^.!?\n]{0,30}\bon (?:my|the|a|your)?\s*(?:test |android |new )?(?:device|phone|handset"
            r"|tablet|laptop))"
            r"|\b(?:act|behave|respond)\s+(?:as|like)\s+(?:an?\s+)?(?:unrestricted|unfiltered|uncensored"
            r"|jailbroken|"
            r"open[- ]source model with no)\b"
            r"|\b(?:you are now|from now on you are|pretend (?:you are|to be)|assume the role of"
            r"|simulate)\b[^.!?\n]{0,60}"
            r"\b(?:unrestricted|no (?:constraints|limits|rules|restrictions|filters|ethical)|without (?:any"
            r" )?(?:limits|"
            r"rules|restrictions|filters|guardrails)|god[- ]like|jailbroken|dan\b|bypass any)"
            r"|\bno (?:ethical|safety|content) (?:guidelines|constraints|filters|limits|rules)\b"
            r"|\b(?:policy checks|safety filters|guardrails) (?:are |is )?disabled\b)"
        ),
    ),
    (
        "bulk-exfiltration",
        re.compile(
            r"\b(?:dump|export|list|give|send|provide|hand over|fetch|extract|retrieve|copy|download|share"
            r"|show|"
            r"print|display|need|want|require|get|pull)\b"
            r"[^.!?\n]{0,40}\b(?:all|every|entire|complete|full|bulk|whole)\b[^.!?\n]{0,40}"
            rf"\b{_SENSITIVE_DATA}"
            r"|\b(?:all|every|entire|complete)\b[^.!?\n]{0,20}\b(?:gct )?(?:customers?|subscribers?|users?|"
            r"accounts?)\b[^.!?\n]{0,30}\b(?:including|with)\b[^.!?\n]{0,30}\b(?:names|phone numbers"
            r"|addresses|"
            r"passwords|emails|national ids)"
            rf"|\b(?:all|every)\b[^.!?\n]{{0,20}}\b(?:active |internal )?{_SENSITIVE_DATA}"
            r"[^.!?\n]{0,40}\b(?:for all|in the system|currently active|in the platform)"
        ),
    ),
    (
        "credential-request",
        re.compile(
            r"\b(?:give|provide|reveal|share|tell|show|output|display|send|print|leak)\b[^.!?\n]{0,30}"
            r"\b(?:admin|administrator|root|internal|system|master|secret|service)\s+"
            r"(?:passwords?|credentials?|tokens?|api keys?|keys?|pins?)\b"
        ),
    ),
    (
        "exfiltration-channel",
        re.compile(
            r"\b(?:send|post|upload|forward|e-?mail|transfer|dispatch|push|exfiltrate|copy|sync|stream"
            r"|leak)\b"
            r"[^\n]{0,120}(?:\b(?:https?|ftp|sftp|s3|gs)://|\bwebhook\b"
            r"|\b[\w.+-]+@(?!gct\b)[\w-]+\.[a-z]{2,}\b|"
            r"\bexternal (?:endpoint|server|storage|bucket|address)\b|\bpastebin\b)"
        ),
    ),
    (
        "ai-directed-instruction",
        re.compile(
            r"\bai[ _-]?(?:note|directive|command|instruction|action|prompt|task|request)s?\b"
            r"|\b(?:note|message|instructions?|directive) (?:to|for) (?:the |any )?(?:ai|assistant|llm|model"
            r"|chatbot|"
            r"language model)\b"
            r"|\b(?:dear|attention|hey) (?:ai|assistant|llm|chatbot|language model)\b"
            r"|\bif you are an? (?:ai|assistant|llm|language model)\b"
            r"|<\|(?:im_start|im_end|system|assistant)\|>|\[/?inst\]|</?(?:system|evidence|instructions?)>"
            r"|(?:^|\n)\s*#{0,3}\s*(?:system|assistant)\s*(?:prompt)?\s*:"
        ),
    ),
    (
        "policy-evasion",
        re.compile(
            r"\b(?:skip|bypass|disable|ignore|turn off|switch off|circumvent|avoid|without (?:any |applying"
            r" )?)"
            r"[^.!?\n]{0,30}\b(?:dlp|data[- ]loss[- ]prevention|redaction|masking|privacy checks?|policy"
            r" enforcement|"
            r"content filter|filtering|sovereign(?:ty)? (?:route|path|polic(?:y|ies))|classification"
            r"|anonymi[sz]ation)\b"
            r"|\b(?:send|route|forward|pass|share)\b[^.!?\n]{0,60}\b(?:external|outside|third[- ]party"
            r"|public|"
            r"cloud)\s+(?:ai|model|llm|service|provider)s?\b[^.!?\n]{0,40}\b(?:without|no|unfiltered|as-is"
            r"|anyway|"
            r"despite|redaction|masking)"
            r"|\b(?:already|is) (?:public(?:ly)?|published|disclosed|on our (?:website|blog|public"
            r" forum))\b[^\n]{0,120}"
            r"\b(?:send|route|forward|share|without|skip|no need)\b"
            r"|\b(?:don't|do not|no) need (?:any |for )?(?:privacy checks?|dlp|redaction|masking|policy"
            r" checks?)"
            r"|\b(?:treat|classify|mark|label) (?:this|it|the data)[^.!?\n]{0,30}\bas (?:public|internal"
            r"|non[- ]confidential)\b"
        ),
    ),
    (
        "encoded-payload",
        re.compile(
            r"\b(?:decode|execute|run|follow|interpret|apply)\b[^.!?\n]{0,30}\b(?:base64|rot13"
            r"|hex(?:adecimal)?|"
            r"encoded|reversed|backwards)\b"
        ),
    ),
    (
        "unapproved-action",
        re.compile(
            r"\b(?:without|bypass(?:ing)?|skip(?:ping)?|no|ignore|ignoring)\b(?: any| the| a)?(?: usual|"
            r" standard| normal)?"
            r" (?:approvals?|change[- ]request(?: process)?|justification required|testing approval"
            r"|review(?: process)?"
            r"|logging (?:a )?request|sign-?off|authori[sz]ation)\b"
            r"|\bgrant\b[^.!?\n]{0,30}\b(?:full|admin|root|unrestricted) (?:access|rights"
            r"|privileges)\b[^.!?\n]{0,40}"
            r"\b(?:personal|my own|private)\b"
        ),
    ),
)


def _views(text: str) -> list[str]:
    """Normalised views of the text that undo common obfuscations. All views are lower-cased."""
    nfkc = unicodedata.normalize("NFKC", text)
    visible = "".join(ch for ch in nfkc if unicodedata.category(ch) != "Cf")
    plain = visible.translate(_NORMALISE)
    despaced = _SPACED.sub(lambda m: re.sub(r"[ \t]", "", m.group(0)), plain)
    deleet = _MIXED_TOKEN.sub(lambda m: m.group(0).translate(_LEET), despaced)
    views = [plain.lower(), deleet.lower(), plain[::-1].lower()]
    for blob in _BASE64.findall(plain):
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        if decoded.isprintable() and sum(ch.isalpha() for ch in decoded) >= len(decoded) / 2:
            views.append(decoded.lower())
    return views


class HeuristicInjectionDetector:
    """v2: the indicators above over normalised views (NFKC, zero-width removal, look-alike letters,
    spaced-out letters, leetspeak, reversed text and decoded base64)."""

    version = "injection-heuristics-v2"

    def detect(self, text: str) -> tuple[str, ...]:
        views = _views(text)
        return tuple(name for name, pattern in PATTERNS_V2 if any(pattern.search(view) for view in views))
