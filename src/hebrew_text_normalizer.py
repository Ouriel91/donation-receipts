import re


# Hebrew Unicode block (letters, niqqud, Hebrew punctuation such as the maqaf)
# plus the Hebrew presentation forms block.
_HEBREW_RUN = re.compile(r"[֐-׿יִ-ﭏ]+")


def _normalize_line(line: str) -> str:
    if not _HEBREW_RUN.search(line):
        return line

    # Reverse the characters inside each contiguous Hebrew run, leaving every
    # non-Hebrew run (numbers, dates, currency, Latin, punctuation) untouched
    # and in place.
    return _HEBREW_RUN.sub(lambda match: match.group(0)[::-1], line)


def normalize_hebrew_text(text: str) -> str:
    lines = text.split("\n")
    return "\n".join(_normalize_line(line) for line in lines)
