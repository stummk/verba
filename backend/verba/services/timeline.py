"""Interval arithmetic over a recording's timeline — the one place that math lives.

Cutting audio, playing back a selection and moving the segments along with a
cut are the same problem seen three times: a recording is described by the
spans of it that are *kept*, and every edit is a set operation on those spans.

    keep      [====]      [========]        the file after the cuts
    original  0    3      7        15       positions in the untouched file
    new       0    3      3        11       positions in the cut file

So this module knows nothing about ffmpeg, the database or seconds-as-strings:
it normalises, subtracts and intersects spans, and it maps a position from the
original timeline into the cut one (`map_time`) — which is exactly what the
segments need when the audio under them gets shorter.

Spans are half-open `(start, end)` tuples in seconds, `start < end`, sorted and
non-overlapping once they come out of `normalize`. Anything shorter than
`MIN_SPAN` is dropped: a "cut" of a millisecond is a mis-drag, not an edit, and
letting it through would produce an ffmpeg filter graph for nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

Span = tuple[float, float]

MIN_SPAN = 0.01
# Two spans that all but touch are one span: dragging a selection to where the
# last one ended leaves a gap of a pixel's worth of time, and a cut along that
# gap would leave an inaudible sliver of audio standing.
JOIN_GAP = 0.02


def normalize(spans: Iterable[Sequence[float]], duration: float | None = None) -> list[Span]:
    """Clamp to the recording, drop the empty ones, sort, merge what overlaps."""
    cleaned: list[Span] = []
    for span in spans:
        start, end = float(span[0]), float(span[1])
        if start > end:
            start, end = end, start
        start = max(0.0, start)
        if duration is not None:
            end = min(float(duration), end)
            start = min(start, float(duration))
        if end - start >= MIN_SPAN:
            cleaned.append((start, end))
    cleaned.sort()

    merged: list[Span] = []
    for start, end in cleaned:
        if merged and start - merged[-1][1] <= JOIN_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def total(spans: Iterable[Sequence[float]]) -> float:
    """How much time the spans cover together (they must not overlap)."""
    return sum(float(end) - float(start) for start, end in spans)


def intersect(spans: Sequence[Span], others: Sequence[Span]) -> list[Span]:
    """The parts both sides have — used to keep only what is selected."""
    result: list[Span] = []
    for start, end in spans:
        for other_start, other_end in others:
            low, high = max(start, other_start), min(end, other_end)
            if high - low >= MIN_SPAN:
                result.append((low, high))
    return normalize(result)


def subtract(spans: Sequence[Span], removed: Sequence[Span]) -> list[Span]:
    """What is left of `spans` once `removed` is taken out of them."""
    result: list[Span] = []
    for start, end in spans:
        pieces = [(start, end)]
        for cut_start, cut_end in removed:
            next_pieces: list[Span] = []
            for piece_start, piece_end in pieces:
                if cut_end <= piece_start or cut_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < cut_start:
                    next_pieces.append((piece_start, cut_start))
                if cut_end < piece_end:
                    next_pieces.append((cut_end, piece_end))
            pieces = next_pieces
        result.extend(pieces)
    return normalize(result)


def covers_all(keeps: Sequence[Span], duration: float | None) -> bool:
    """Whether the spans still describe the whole recording — nothing to cut.

    The comparison is deliberately loose at both ends: a selection dragged to
    the very edge of the waveform lands a few milliseconds short of it, and
    re-encoding a whole file to remove that is work for nothing.

    An unknown duration (a container ffprobe could not read) answers False:
    nothing can be ruled out then, and refusing the cut would leave the file
    uneditable for good.
    """
    if not duration or duration <= 0:
        return False
    return len(keeps) == 1 and keeps[0][0] <= JOIN_GAP and keeps[0][1] >= duration - JOIN_GAP


def map_time(keeps: Sequence[Span], position: float) -> float | None:
    """Where a position of the original recording ends up after the cuts.

    None for a position inside a removed part — it has no place in the result.
    A position exactly on a keep's end belongs to that keep (its last instant),
    so a segment that ends where a cut begins keeps its end.
    """
    elapsed = 0.0
    for start, end in keeps:
        if position < start:
            return None
        if position <= end:
            return elapsed + (position - start)
        elapsed += end - start
    return None


def map_span(keeps: Sequence[Span], start: float, end: float) -> Span | None:
    """A span of the original recording, expressed in the cut one.

    None when nothing of it survives. What survives may have been several
    pieces with removed audio in between — in the result that audio is gone,
    so the pieces are contiguous and the span is simply first-to-last. A span
    that lies wholly inside a removed passage keeps nothing: its audio is gone,
    and so is the segment that sat on it.
    """
    surviving = intersect([(start, end)], list(keeps))
    if not surviving:
        return None
    new_start = map_time(keeps, surviving[0][0])
    new_end = map_time(keeps, surviving[-1][1])
    if new_start is None or new_end is None or new_end - new_start < MIN_SPAN:
        return None
    return (new_start, new_end)
