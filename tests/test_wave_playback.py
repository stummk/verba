"""Playing several selections in the editor: the hop from one selected passage
to the next reacts to `timeupdate`, and the audio element reports the position
it is jumping away from while a seek is still pending. Acting on such a report
made the run start at the second selection and never play the first — so the
guard against it has to stay in front of the decision it would poison."""

from __future__ import annotations

import re
from pathlib import Path

EDITOR = Path(__file__).resolve().parents[1] / "frontend/js/views/editor.js"


def hop_body() -> str:
    source = EDITOR.read_text(encoding="utf-8")
    match = re.search(r"\n  function hopToNextSpan\(time\) \{(.*?)\n  \}\n", source, re.DOTALL)
    assert match, "hopToNextSpan nicht gefunden — Regex veraltet?"
    return match.group(1)


def test_a_pending_seek_is_not_a_position_report():
    body = hop_body()
    assert "isSeeking()" in body, (
        "hopToNextSpan prüft isSeeking() nicht mehr: ein timeupdate aus einem "
        "laufenden Seek meldet die alte Position, und die erste Auswahl wird "
        "übersprungen"
    )
    guard = body.index("isSeeking()")
    decision = body.index("time < span[1]")
    assert guard < decision, (
        "die isSeeking()-Prüfung muss vor dem Vergleich mit dem Spanende stehen"
    )


def test_the_run_only_hops_while_it_is_playing():
    """The other half of the same guard: `setTime` emits a `timeupdate` of its
    own, so a paused editor must not walk through the spans."""
    body = hop_body()
    assert "!wavesurfer.isPlaying()" in body, "hopToNextSpan reagiert wieder im Pausenzustand"
