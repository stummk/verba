// Interval arithmetic over the recording's timeline — the browser half of
// backend/verba/services/timeline.py, and deliberately the same functions with
// the same names: selecting several passages, marking passages for removal and
// playing back what is left are all set operations on spans of seconds.
//
// A span is `[start, end]` with start < end. Everything here works in the
// coordinates of the file *as it currently is* — the pending cuts have not been
// written yet, so there is only one timeline to think about; the backend
// re-times the segments once a cut is actually applied.

const MIN_SPAN = 0.01;
// Two spans that all but touch are one span: a selection dragged to where the
// last one ended leaves a gap of a pixel's worth of time behind.
const JOIN_GAP = 0.02;

/** Clamp to the recording, drop the empty ones, sort, merge what overlaps. */
export function normalize(spans, duration = null) {
  const cleaned = [];
  for (const span of spans) {
    let [start, end] = [Number(span[0]), Number(span[1])];
    if (start > end) [start, end] = [end, start];
    start = Math.max(0, start);
    if (duration !== null && Number.isFinite(duration)) {
      end = Math.min(duration, end);
      start = Math.min(start, duration);
    }
    if (end - start >= MIN_SPAN) cleaned.push([start, end]);
  }
  cleaned.sort((a, b) => a[0] - b[0] || a[1] - b[1]);

  const merged = [];
  for (const [start, end] of cleaned) {
    const last = merged[merged.length - 1];
    if (last && start - last[1] <= JOIN_GAP) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

/** How much time the spans cover together (they must not overlap). */
export function total(spans) {
  return spans.reduce((sum, [start, end]) => sum + (end - start), 0);
}

/** The parts both sides have — "keep only what is selected". */
export function intersect(spans, others) {
  const result = [];
  for (const [start, end] of spans) {
    for (const [otherStart, otherEnd] of others) {
      const low = Math.max(start, otherStart);
      const high = Math.min(end, otherEnd);
      if (high - low >= MIN_SPAN) result.push([low, high]);
    }
  }
  return normalize(result);
}

/** What is left of `spans` once `removed` is taken out of them. */
export function subtract(spans, removed) {
  const result = [];
  for (const span of spans) {
    let pieces = [span];
    for (const [cutStart, cutEnd] of removed) {
      const next = [];
      for (const [start, end] of pieces) {
        if (cutEnd <= start || cutStart >= end) {
          next.push([start, end]);
          continue;
        }
        if (start < cutStart) next.push([start, cutStart]);
        if (cutEnd < end) next.push([cutEnd, end]);
      }
      pieces = next;
    }
    result.push(...pieces);
  }
  return normalize(result);
}

/** Whether the spans still describe the whole recording — nothing is cut. */
export function coversAll(spans, duration) {
  if (!duration || !Number.isFinite(duration)) return spans.length === 0;
  return spans.length === 1 && spans[0][0] <= JOIN_GAP && spans[0][1] >= duration - JOIN_GAP;
}

/** Which span a position falls into, or -1 — "what did the user click on?". */
export function indexAt(spans, position) {
  return spans.findIndex(([start, end]) => position >= start && position <= end);
}
