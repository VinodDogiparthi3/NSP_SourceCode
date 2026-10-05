import re


AGGREGATE_PATTERNS = [
    r"\bhow many\b",
    r"\bcount\b",
    r"\btotal\b",
    r"\baverage\b",
    r"\bbreakdown\s+by\b",
    r"\bكم عدد\b",
    r"\bإجمالي\b",
]


def classify_question(question: str) -> str:
    normalized = question.casefold()
    if any(re.search(pattern, normalized) for pattern in AGGREGATE_PATTERNS):
        return "aggregate"
    return "semantic"

