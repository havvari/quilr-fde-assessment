"""Redact PII from a text stream without ever holding the whole stream.

Both obvious implementations are wrong:

* **Regex per chunk.** A provider splits `4111-1111-1111-1111` into
  `...Card: 4111-1111-1` and `111-1111. Let me...` whenever it feels like it.
  Neither chunk contains a card number. Nothing is redacted, and the card ships.
* **Buffer everything, then redact.** Correct, and it destroys the product: no
  token reaches the client until the model has finished, so TTFT becomes total
  generation time, and memory grows with the response.

What this does instead: keep a **held-back tail** of `hold` characters. Text
older than that can no longer be part of a match that future input might extend,
so it is safe to emit. `hold` is derived from the patterns themselves
(`max_pattern_length()`), not guessed -- too small silently misses matches
straddling a boundary, too large delays every token by that many characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTED = "[REDACTED]"

# Bounded quantifiers everywhere, deliberately. An unbounded `+` would make the
# longest possible match unbounded, and the hold length is derived from it -- an
# unbounded pattern means there is no safe boundary and no correct hold size.
#
# `(?<![\w.@-])` / `(?![\w.@-])` keep us from matching a fragment of a longer
# token: without them, `not-an-ssn-999-11-1111-x` would be partly redacted.
EMAIL = (
    r"(?<![\w.@+-])[A-Za-z0-9._%+-]{1,64}"
    r"@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,4}\.[A-Za-z]{2,24}"
    # Only alphanumerics and `-` may follow: a trailing `.` is a sentence ending,
    # not part of the address. `(?![\w.-])` looks right and silently fails to
    # redact every address that ends a sentence.
    r"(?![A-Za-z0-9_-])"
)

# 13-19 digits, optionally separated by single spaces or hyphens. Written as
# "4 digits then 9-15 more" rather than as four groups of four, because Amex is
# 4-6-5 and a groups-of-four pattern misses it entirely.
CARD = r"(?<!\d)\d{4}(?:[ -]?\d){9,15}(?!\d)"

# US SSN, hyphen- or space-separated. A bare 9-digit run is not included: it
# collides with order numbers and zip+4, and this is a text stream, not a form.
SSN = r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)"

# One compiled alternation, not three passes. Order is leftmost-then-first-
# alternative, so email wins over card for a string that could start either way.
PATTERN = re.compile(f"(?P<email>{EMAIL})|(?P<card>{CARD})|(?P<ssn>{SSN})")

# Longest string any alternative above can match:
#   email 64 local + 1 '@' + 63 domain + 4x64 subdomains + 1 '.' + 24 TLD = 409
#   card  19 digits + 15 separators                                      =  34
#   ssn   9 digits + 2 separators                                        =  11
# The hold is the max of these. Nothing shorter is safe: a match exactly this
# long could straddle the boundary with only the last character still to arrive.
#
# 409 characters of held-back text would be a poor TTFT on its own, so it is a
# worst-case cap rather than a fixed delay: `_unsafe_start()` below usually
# releases text far sooner. See the README.
MAX_EMAIL = 64 + 1 + 63 + (4 * 64) + 1 + 24
MAX_CARD = 19 + 15
MAX_SSN = 9 + 2


def max_pattern_length() -> int:
    return max(MAX_EMAIL, MAX_CARD, MAX_SSN)


DEFAULT_HOLD = max_pattern_length()


def luhn_ok(digits: str) -> bool:
    """The Luhn checksum, used to decide whether a digit run is really a card."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass
class RedactionStats:
    """Enough to prove the memory claim in a test, and to alert on in production."""

    emitted_chars: int = 0
    redactions: int = 0
    peak_buffer: int = 0


class StreamingRedactor:
    """Feed deltas in, get safe-to-emit text out. Call `flush()` at end of stream.

    Not thread-safe and not reusable across responses: one instance per stream.
    """

    def __init__(self, *, hold: int | None = None, require_luhn: bool = False) -> None:
        # `hold` is overridable so a test can shrink it and watch correctness
        # break, which is the only honest way to show the constant is load-bearing.
        self.hold = DEFAULT_HOLD if hold is None else hold
        self.require_luhn = require_luhn
        self._buffer = ""
        self.stats = RedactionStats()

    def feed(self, text: str) -> str:
        """Absorb a delta; return whatever is now provably safe to emit."""
        if not text:
            return ""
        self._buffer += text
        self.stats.peak_buffer = max(self.stats.peak_buffer, len(self._buffer))
        return self._emit_safe_prefix()

    def flush(self) -> str:
        """End of stream: no more input can extend a match, so emit everything.

        Without this, a pattern sitting in the tail when the provider stops would
        be silently dropped -- which is worse than not redacting, because the
        client is missing text and nobody is told.
        """
        remaining = self._buffer
        self._buffer = ""
        if not remaining:
            return ""
        out = self._substitute(remaining)
        self.stats.emitted_chars += len(out)
        return out

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def _emit_safe_prefix(self) -> str:
        buffer = self._buffer
        safe = self._unsafe_start(buffer)
        if safe <= 0:
            return ""

        pieces: list[str] = []
        cursor = 0
        for match in PATTERN.finditer(buffer):
            if match.start() >= safe:
                # Entirely inside the held tail; leave it for a later call.
                break
            if match.end() > safe:
                # Straddles the boundary. More input could extend this match, so
                # hold the whole thing rather than emitting a redaction that a
                # longer match would have rendered differently.
                safe = match.start()
                break
            if not self._is_real(match):
                continue
            pieces.append(buffer[cursor : match.start()])
            pieces.append(REDACTED)
            self.stats.redactions += 1
            cursor = match.end()

        pieces.append(buffer[cursor:safe])
        self._buffer = buffer[safe:]
        out = "".join(pieces)
        self.stats.emitted_chars += len(out)
        return out

    def _unsafe_start(self, buffer: str) -> int:
        """The earliest index a match crossing the end of the buffer could start.

        The floor is `len - hold`: nothing older than one maximum-length match
        can be affected by future input. But holding a fixed 409 characters would
        put 409 characters of latency on every stream, and almost all of that is
        unnecessary -- a match is a contiguous run of characters from a small
        alphabet, so any run that ends at a character no pattern can contain
        cannot be extended across it.

        So: walk back from the end over match-eligible characters and stop at the
        first one that is not. In ordinary prose that is the previous space, so
        the delay is one word rather than 409 characters. In the middle of a card
        number it is the whole number, which is exactly when we want to wait.
        """
        floor = max(0, len(buffer) - self.hold)
        index = len(buffer)
        while index > floor and self._can_continue_a_match(buffer, index - 1):
            index -= 1
        return index

    @staticmethod
    def _can_continue_a_match(buffer: str, index: int) -> bool:
        """Could `buffer[index]` be part of a match that extends past the end?"""
        char = buffer[index]
        if char.isalnum() or char in "._%+-@":
            return True
        if char != " ":
            return False
        # A space is only ever a separator *inside* a card or SSN, so it counts
        # only between digits. This is what keeps prose from being held: the
        # spaces between words break the run immediately.
        if index == 0 or not buffer[index - 1].isdigit():
            return False
        # A trailing space with a digit before it might yet be followed by one.
        return index + 1 >= len(buffer) or buffer[index + 1].isdigit()

    def _substitute(self, text: str) -> str:
        """Redact every match in `text`, which is known to be complete."""
        pieces: list[str] = []
        cursor = 0
        for match in PATTERN.finditer(text):
            if not self._is_real(match):
                continue
            pieces.append(text[cursor : match.start()])
            pieces.append(REDACTED)
            self.stats.redactions += 1
            cursor = match.end()
        pieces.append(text[cursor:])
        return "".join(pieces)

    def _is_real(self, match: re.Match[str]) -> bool:
        """Optional Luhn gate on card matches only.

        Off by default, and that default is the point: this is a guardrail, so a
        false positive costs a redacted order number while a false negative
        leaks a card. Over-redaction is the cheaper error. Turn it on where the
        text is known to be full of long digit runs that are not cards.
        """
        if not self.require_luhn or match.lastgroup != "card":
            return True
        return luhn_ok(re.sub(r"\D", "", match.group()))


def redact(text: str) -> str:
    """Whole-string redaction. Only for tests and non-streaming responses."""
    redactor = StreamingRedactor()
    return redactor.feed(text) + redactor.flush()
