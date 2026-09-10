"""Speaker recognition: who speaks when, and where a segment has to be cut.

Whisper hears words, not people. It writes a segment whenever it feels like
breaking, which means one segment routinely holds the end of one person's
sentence and the start of another's — a transcript that reads as one voice
although two were talking. This module is the second pass that takes that
apart.

Two ONNX networks do the work, run by sherpa-onnx on the processor:

- the *segmentation* model says when somebody is speaking (and when two people
  are speaking at once). There is one, it is not a choice.
- the *speaker embedding* model turns each of those stretches into a
  fingerprint of the voice; clustering the fingerprints is what turns "someone
  speaks here" into "the same someone as over there". This one is a choice,
  because size buys accuracy (`config.SPEAKER_MODELS`).

Neither cares which language is being spoken — a voice is a voice — and
neither needs a token, an account or a network connection once downloaded.

What comes back are *turns*: `speaker 2 from 41.6 s to 47.2 s`. Turning those
into a transcript is the other half of this module, and the interesting half:

- A segment whose whole span belongs to one speaker only gains a name. Its row
  keeps its id, its text and its word timings.
- A segment in which the speaker changes is cut at that change. Where the
  transcription recorded word timings (it does exactly when the type asks for
  this recognition, see services/whisper.py) the cut lands between two words,
  which is the point of recording them.
- Without word timings — an old transcript, or one whose text has been edited
  — the cut is placed by time and snapped to the nearest sentence end, which is
  a guess. It is still better than a segment attributed to the wrong person,
  and the user guide says as much.

The names are placeholders: nothing here can know that speaker 2 is Frau
Berger, so it says "Sprecher 2" in the interface language and leaves the
renaming to the editor (`transcripts.rename_speaker`).
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..core.jobs import JobCancelled, job_queue
from ..events import hub
from . import audio, project_types, transcripts, workspace
from .media import format_clock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    """A stretch of speech attributed to one voice.

    `speaker` is a cluster number, not a person: the mapping to a name happens
    once per file, by order of first appearance, so "Sprecher 1" is whoever
    opens the recording.
    """

    start: float
    end: float
    speaker: int


#: Shorter than this, a stretch attributed to somebody else is not a turn but
#: jitter — a single word caught by the neighbouring voice's fingerprint. It is
#: given back to whichever neighbour has more to say. Deliberately a shade
#: above the recognition's own `min_duration_on` (0.3 s): what survives that
#: filter as speech may still be too short to be worth cutting a sentence for.
MIN_TURN_S = 0.35
#: A short stretch of another voice *between two stretches of the same one*
#: (A A B A A) is that voice's neighbour, not a turn: a speaker change does
#: not change back a word later. Up to this length such an island is absorbed.
ISLAND_MAX_S = 0.8
#: What the speakers are called, per interface language. The transcript is the
#: place a user reads these, so they follow the interface, not the recording.
SPEAKER_PREFIX = {"de": "Sprecher", "en": "Speaker", "ru": "Говорящий"}
#: How far from the computed position a text cut may wander to land on a
#: sentence end, as a share of the segment's text.
SNAP_SHARE = 0.25
_SENTENCE_END = ".!?…"

# ── the models ────────────────────────────────────────────────────────


def segmentation_path(settings: config.Settings | None = None) -> Path:
    return config.speakers_dir(settings) / config.SEGMENTATION_FILE


def model_path(name: str, settings: config.Settings | None = None) -> Path:
    return config.speakers_dir(settings) / name


def library_available() -> bool:
    """Whether sherpa-onnx is installed (its own feature group).

    Asked through `setup_check`, which is the one place that knows what
    "installed" means here — a frozen build installs feature groups into a
    directory of its own, and a half-removed package must not look present.
    """
    from ..setup_check import group_by_key, group_installed

    group = group_by_key("diarize")
    return group is not None and group_installed(group)


def status() -> dict[str, Any]:
    """What the settings page needs to show: library, models, downloads."""
    settings = config.get_settings()
    directory = config.speakers_dir(settings)
    return {
        "available": library_available(),
        # the *selected* model has to be there, not just any of them — that is
        # what decides whether a recognition can start
        "ready": ready(),
        "directory": str(directory),
        "segmentation": segmentation_path(settings).exists(),
        "segmentation_mb": config.SEGMENTATION_ARCHIVE_MB,
        "selected": settings.diarization.model,
        "threshold": settings.diarization.threshold,
        "downloading": sorted(_active_downloads),
        "models": [
            {
                "name": entry.name,
                "label": entry.label,
                "size_mb": entry.size_mb,
                "speed": entry.speed,
                "installed": model_path(entry.name, settings).exists(),
            }
            for entry in config.SPEAKER_MODELS
        ],
    }


def ready() -> bool:
    """Whether a recognition could run right now."""
    settings = config.get_settings()
    return (
        library_available()
        and segmentation_path(settings).exists()
        and model_path(settings.diarization.model, settings).exists()
    )


_downloads_lock = threading.Lock()
_active_downloads: set[str] = set()

#: The name the segmentation model is downloaded under — it is not one of the
#: catalog entries, but it shares their download machinery and their UI.
SEGMENTATION_KEY = "segmentation"


def start_download(name: str) -> bool:
    """Fetch one model in the background; False if it is already on its way.

    `name` is a catalog entry's file name or `SEGMENTATION_KEY`. Progress goes
    out as `speaker.model` events, the same way the whisper models report.

    An unknown name is refused here rather than inside the thread, so the
    caller gets an answer instead of an event nobody is listening for yet.
    """
    if name != SEGMENTATION_KEY and config.speaker_model(name).name != name:
        raise ValueError(f"Unbekanntes Sprechermodell: {name}")
    with _downloads_lock:
        if name in _active_downloads:
            return False
        _active_downloads.add(name)

    def run() -> None:
        try:
            if name == SEGMENTATION_KEY:
                _download_segmentation()
            else:
                _download_model(name)
            _publish_download(name, "done")
        except Exception as exc:  # noqa: BLE001 — reported, never raised into the thread
            logger.exception("speaker model download failed: %s", name)
            _publish_download(name, "error", str(exc))
        finally:
            with _downloads_lock:
                _active_downloads.discard(name)

    threading.Thread(target=run, daemon=True, name=f"speaker-download-{name}").start()
    return True


def _publish_download(name: str, state: str, detail: str = "", percent: int = 0) -> None:
    hub.publish(
        "speaker.model", {"name": name, "state": state, "detail": detail, "percent": percent}
    )


def _size_cap(size_mb: int) -> int:
    """The refuse-it-above size for a download of about `size_mb`.

    The catalog number is what the settings page *shows* — rounded, and
    rounded down for a 28.2 MB file. It must not double as the ceiling the
    download is refused above, or the model would be refused at 96 % for
    being the size it actually is (which is what happened). Hence the
    headroom: enough that a re-uploaded release grows into it, little enough
    that a wrong URL serving something huge is still caught.
    """
    return (size_mb + 16) * 1024 * 1024


def _emit_for(name: str) -> Any:
    def emit(percent: int, message: str) -> None:
        _publish_download(name, "running", message, percent)

    return emit


def _download_model(name: str) -> None:
    """One embedding model — a plain .onnx file, written where it belongs."""
    from . import download

    entry = config.speaker_model(name)
    if entry.name != name:
        raise ValueError(f"Unbekanntes Sprechermodell: {name}")
    target = model_path(name)
    download.fetch(
        config.SPEAKER_MODEL_URL + name,
        target,
        _size_cap(entry.size_mb),
        _emit_for(name),
    )
    logger.info("speaker embedding model '%s' downloaded to %s", name, target)


def _download_segmentation() -> None:
    """The segmentation model, which ships as a tar.bz2 holding model.onnx.

    Unpacked into the models directory under a name of its own, so the
    directory holds two plain files and nothing has to remember the layout of
    an archive.
    """
    from . import download

    target = segmentation_path()
    with tempfile.TemporaryDirectory(prefix="verba-seg-") as tmp:
        archive = Path(tmp) / "segmentation.tar.bz2"
        download.fetch(
            config.SEGMENTATION_URL,
            archive,
            _size_cap(config.SEGMENTATION_ARCHIVE_MB),
            _emit_for(SEGMENTATION_KEY),
        )
        extracted = _extract_model_onnx(archive, Path(tmp) / "out")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(target))
    logger.info("speaker segmentation model downloaded to %s", target)


def _extract_model_onnx(archive: Path, into: Path) -> Path:
    """Pull `model.onnx` out of the archive — and nothing else.

    A member is taken by name, not by pattern, and written flat: an archive
    from the internet does not get to decide where files land
    (`../../` in a member name is how that goes wrong).
    """
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        member = next(
            (m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "model.onnx"),
            None,
        )
        if member is None:
            raise RuntimeError("Das Archiv enthält kein Segmentierungsmodell (model.onnx)")
        source = tar.extractfile(member)
        if source is None:
            raise RuntimeError("Das Segmentierungsmodell kann nicht gelesen werden")
        target = into / "model.onnx"
        with source, target.open("wb") as sink:
            shutil.copyfileobj(source, sink)
    return target


def delete_model(name: str) -> None:
    """Remove one downloaded model; the recognition then asks for it again."""
    settings = config.get_settings()
    target = segmentation_path(settings) if name == SEGMENTATION_KEY else model_path(name, settings)
    if config.speakers_dir(settings).resolve() != target.resolve().parent:
        raise ValueError("Path is outside the speaker model directory")
    if not target.exists():
        raise FileNotFoundError("Dieses Modell ist nicht installiert")
    target.unlink()
    logger.info("speaker model deleted: %s", target)


# ── the recognition itself ────────────────────────────────────────────


def _diarizer() -> Any:
    """The configured recognition, or a German refusal naming what is missing."""
    if not library_available():
        raise RuntimeError(
            "Die Sprechererkennung ist nicht installiert. "
            "Bitte in den Einstellungen die Komponente einrichten."
        )
    import sherpa_onnx

    settings = config.get_settings()
    segmentation = segmentation_path(settings)
    embedding = model_path(settings.diarization.model, settings)
    for path, label in ((segmentation, "Segmentierungsmodell"), (embedding, "Sprechermodell")):
        if not path.exists():
            raise RuntimeError(
                f"Das {label} fehlt ({path.name}). Bitte es in den Einstellungen herunterladen."
            )
    # Four threads is where an ONNX model of this size stops getting faster,
    # and the machine may well be transcribing the next file at the same time.
    threads = max(1, min(4, os.cpu_count() or 1))
    options = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation)
            ),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding), num_threads=threads
        ),
        # -1: how many people are talking is worked out from the recording,
        # never stated — and then the threshold is what decides how different
        # two voices have to sound to be two people.
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=-1, threshold=settings.diarization.threshold
        ),
    )
    if not options.validate():
        raise RuntimeError(
            "Die Sprechererkennung ist falsch konfiguriert — bitte die Modelle prüfen."
        )
    return sherpa_onnx.OfflineSpeakerDiarization(options)


def _read_wave(path: Path, expected_rate: int) -> Any:
    """A 16 kHz mono WAV as float32 samples in [-1, 1].

    Filled into one preallocated array rather than converted from a second
    buffer: an hour of audio is 58 million samples, and holding it twice is
    a quarter of a gigabyte for nothing.
    """
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError("Die Audiodatei ist nicht 16-Bit-Mono")
        if handle.getframerate() != expected_rate:
            raise RuntimeError(
                f"Die Audiodatei hat {handle.getframerate()} Hz, erwartet werden {expected_rate}"
            )
        total = handle.getnframes()
        samples = np.empty(total, dtype=np.float32)
        position = 0
        block = 1 << 20
        while position < total:
            raw = handle.readframes(min(block, total - position))
            if not raw:
                break
            chunk = np.frombuffer(raw, dtype="<i2")
            samples[position : position + chunk.size] = chunk / 32768.0
            position += chunk.size
    return samples[:position]


def run(
    audio_path: Path,
    *,
    cancel: threading.Event,
    report: Any,
    label: str = "",
    progress: tuple[int, int] = (0, 100),
) -> list[Turn]:
    """Recognise the speakers of one recording; returns their turns in order.

    The recording is converted once and then held as samples for the length of
    the call — the clustering has to compare the whole thing with itself, so
    there is no streaming version of this. That is 4 bytes a sample: about
    230 MB for an hour, which is the reason the temporary WAV is deleted before
    the models start working rather than after.
    """
    diarizer = _diarizer()
    low, high = progress
    with tempfile.TemporaryDirectory(prefix="verba-diarize-") as tmp:
        wav = Path(tmp) / "audio.wav"
        audio.to_mono_16k(audio_path, wav, rate=diarizer.sample_rate)
        if cancel.is_set():
            raise JobCancelled()
        samples = _read_wave(wav, diarizer.sample_rate)
    seconds = len(samples) / diarizer.sample_rate

    def on_progress(done: int, total: int) -> int:
        if cancel.is_set():
            return 1  # sherpa-onnx stops as soon as the callback says non-zero
        share = min(done, total) / max(1, total)
        report(low + int((high - low) * share), f"{label}: {format_clock(seconds * share)}")
        return 0

    result = diarizer.process(samples, callback=on_progress)
    # The abort above only ends the run; whether it *was* aborted is decided
    # here, before a partial result could be written over a transcript.
    if cancel.is_set():
        raise JobCancelled()
    return [
        Turn(start=float(item.start), end=float(item.end), speaker=int(item.speaker))
        for item in result.sort_by_start_time()
    ]


# ── turns → transcript ────────────────────────────────────────────────


def speaker_prefix(language: str = "") -> str:
    """What a recognised speaker is called, in the interface language."""
    code = (language or config.get_settings().general.ui_language or "de").lower()[:2]
    return SPEAKER_PREFIX.get(code, SPEAKER_PREFIX["de"])


def speaker_names(turns: list[Turn], prefix: str) -> dict[int, str]:
    """Cluster numbers → names, numbered by who speaks first."""
    order: list[int] = []
    for turn in sorted(turns, key=lambda item: (item.start, item.end)):
        if turn.speaker not in order:
            order.append(turn.speaker)
    return {speaker: f"{prefix} {index + 1}" for index, speaker in enumerate(order)}


def plan_speakers(
    segments: list[dict[str, Any]], turns: list[Turn], *, prefix: str = "Sprecher"
) -> list[dict[str, Any]]:
    """What the recognition means for the transcript, segment by segment.

    Returns one entry per segment it has something to say about:
    `{"id": …, "pieces": [{start_s, end_s, text, speaker, words}, …]}`. One
    piece means "this segment is one voice, here is its name"; several mean the
    segment has to be cut. A segment no turn overlaps gets no entry at all —
    silence, music, or a passage the segmentation heard nobody in, and the
    speaker it already carries is better than one made up here.

    Pure on purpose: this is where the behaviour worth testing lives, and it
    must be testable without two ONNX models on disk.
    """
    ordered = sorted(turns, key=lambda item: (item.start, item.end))
    names = speaker_names(ordered, prefix)
    finder = _Overlaps(ordered)
    plan: list[dict[str, Any]] = []
    for segment in segments:
        pieces = _pieces(segment, finder, names)
        if pieces:
            plan.append({"id": segment["id"], "pieces": pieces})
    return plan


class _Overlaps:
    """Which voice owns a moment — asked once per word, so kept cheap.

    The turns are sorted by start and the questions arrive in time order, so
    the scan starts where the last one left off instead of at the beginning.
    Turns may overlap (two people talking at once is what the segmentation
    model is for), which is why this is a scan and not a lookup.
    """

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = turns
        self._first = 0

    def dominant(self, start: float, end: float) -> int | None:
        """The speaker with the most of [start, end] — None if nobody has any."""
        while self._first < len(self.turns) and self.turns[self._first].end <= start:
            self._first += 1
        best: int | None = None
        best_overlap = 0.0
        for turn in self.turns[self._first :]:
            if turn.start >= end:
                break
            overlap = min(end, turn.end) - max(start, turn.start)
            if overlap > best_overlap:
                best_overlap, best = overlap, turn.speaker
        return best

    def rewind(self) -> None:
        """Start the scan over — the caller is going back in time."""
        self._first = 0


def _whole(segment: dict[str, Any], speaker: str) -> dict[str, Any]:
    """The segment as it is, with a name: nothing to cut here."""
    return {
        "start_s": segment["start_s"],
        "end_s": segment["end_s"],
        "text": segment["text"],
        "speaker": speaker,
        "words": segment.get("words", ""),
    }


def _pieces(
    segment: dict[str, Any], finder: _Overlaps, names: dict[int, str]
) -> list[dict[str, Any]]:
    words = transcripts.decode_words(segment.get("words", ""))
    finder.rewind()
    if words and _words_match(words, segment["text"]):
        runs = _word_runs(words, finder)
        if not runs:
            return []
        if len(runs) == 1:
            return [_whole(segment, names[runs[0][0]])]
        return _split_at_words(segment, words, runs, names)
    spans = _time_runs(segment["start_s"], segment["end_s"], finder)
    if not spans:
        return []
    if len(spans) == 1:
        return [_whole(segment, names[spans[0][2]])]
    return _split_by_time(segment, spans, names)


#: How much of a segment's text its word timings have to account for before
#: they are allowed to cut it. They are written by the transcription and the
#: text is what the transcription said, so they normally account for all of it
#: — but a text corrected by hand, or a recognition that dropped a word, would
#: otherwise have the cut throw characters away.
WORDS_COVERAGE = 0.9


def _words_match(words: list[transcripts.Word], text: str) -> bool:
    """Whether these timings still describe this text."""
    stripped = "".join(word for _start, _end, word in words).strip()
    if not text.strip():
        return False
    return len(stripped) >= WORDS_COVERAGE * len(text.strip())


def _word_runs(words: list[transcripts.Word], finder: _Overlaps) -> list[tuple[int, int, int]]:
    """Group the words by who said them: `(speaker, first, last + 1)`.

    A word nobody claims (a pause the segmentation cut away, a breath) is
    handed to its neighbours rather than to a speaker of its own — it carries
    no evidence, and a run of its own would cut the sentence in two.
    """
    owners: list[int | None] = [finder.dominant(start, end) for start, end, _text in words]
    if all(owner is None for owner in owners):
        return []
    last: int | None = None
    for index, owner in enumerate(owners):
        if owner is None:
            owners[index] = last
        else:
            last = owner
    following: int | None = None
    for index in range(len(owners) - 1, -1, -1):
        if owners[index] is None:
            owners[index] = following
        else:
            following = owners[index]
    return _smooth_runs(words, [int(owner) for owner in owners])  # type: ignore[arg-type]


def _group(speakers: list[int]) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    for index, speaker in enumerate(speakers):
        if runs and runs[-1][0] == speaker:
            start = runs[-1][1]
            runs[-1] = (speaker, start, index + 1)
        else:
            runs.append((speaker, index, index + 1))
    return runs


def _absorbable(index: int, count: int, same_neighbours: bool, seconds: float) -> bool:
    """Whether this run is the recognition wobbling rather than a turn.

    Two cases, and the second is the common one. A run shorter than a turn can
    be (`MIN_TURN_S`) is noise wherever it sits. And a short run *between two
    stretches of the same other voice* — A A B A A — is an island: a real
    change of speaker does not change back one word later, so this is one word
    caught by the neighbour's fingerprint.

    The cost of the island rule is a genuine short interjection ("Ja." in the
    middle of somebody's answer), which is absorbed into the answer. That is
    the better mistake: it keeps a sentence whole, where the other way round
    tears a monologue into three segments for one word.
    """
    if seconds < MIN_TURN_S:
        return True
    interior = 0 < index < count - 1
    return interior and same_neighbours and seconds < ISLAND_MAX_S


def _smooth_runs(words: list[transcripts.Word], speakers: list[int]) -> list[tuple[int, int, int]]:
    """Give a run that is not a turn to its more talkative neighbour.

    Repeats until nothing is left to absorb or only one run remains; each pass
    removes at least one run, so it terminates.
    """
    while True:
        runs = _group(speakers)
        if len(runs) < 2:
            return runs
        candidates = [
            index
            for index in range(len(runs))
            if _absorbable(
                index,
                len(runs),
                0 < index < len(runs) - 1 and runs[index - 1][0] == runs[index + 1][0],
                _run_seconds(words, runs[index]),
            )
        ]
        if not candidates:
            return runs
        shortest = min(candidates, key=lambda index: _run_seconds(words, runs[index]))
        left = runs[shortest - 1] if shortest > 0 else None
        right = runs[shortest + 1] if shortest + 1 < len(runs) else None
        winner = _louder(words, left, right)
        for index in range(runs[shortest][1], runs[shortest][2]):
            speakers[index] = winner


def _run_seconds(words: list[transcripts.Word], run: tuple[int, int, int]) -> float:
    return max(0.0, words[run[2] - 1][1] - words[run[1]][0])


def _louder(
    words: list[transcripts.Word],
    left: tuple[int, int, int] | None,
    right: tuple[int, int, int] | None,
) -> int:
    if left is None and right is not None:
        return right[0]
    if right is None and left is not None:
        return left[0]
    assert left is not None and right is not None
    return left[0] if _run_seconds(words, left) >= _run_seconds(words, right) else right[0]


def _split_at_words(
    segment: dict[str, Any],
    words: list[transcripts.Word],
    runs: list[tuple[int, int, int]],
    names: dict[int, str],
) -> list[dict[str, Any]]:
    """One piece per run — cut between two words, which is the whole point."""
    pieces: list[dict[str, Any]] = []
    for index, (speaker, first, stop) in enumerate(runs):
        block = words[first:stop]
        # joined, not re-spaced: every token carries the space in front of it,
        # so this is the original wording — including a language that writes
        # without spaces at all
        text = "".join(word for _s, _e, word in block).strip()
        if not text:
            continue
        pieces.append(
            {
                # the outer edges stay the segment's own: the first word may
                # start a little after it, and losing that lead-in would leave
                # a gap in the timeline for no reason
                "start_s": segment["start_s"] if index == 0 else block[0][0],
                "end_s": segment["end_s"] if index == len(runs) - 1 else block[-1][1],
                "text": text,
                "speaker": names[speaker],
                "words": transcripts.encode_words(block),
            }
        )
    if len(pieces) < 2:
        return [_whole(segment, names[runs[0][0]])]
    return pieces


def _time_runs(start: float, end: float, finder: _Overlaps) -> list[tuple[float, float, int]]:
    """Who owns which stretch of a segment, without knowing its words.

    The turn boundaries inside the segment are the only candidates for a cut,
    so they are the intervals this looks at.
    """
    inner = sorted(
        {point for turn in finder.turns for point in (turn.start, turn.end) if start < point < end}
    )
    points = [start, *inner, end]
    spans: list[tuple[float, float, int | None]] = []
    for left, right in zip(points, points[1:], strict=False):
        if right - left <= 0:
            continue
        spans.append((left, right, finder.dominant(left, right)))
    if all(owner is None for _l, _r, owner in spans):
        return []
    filled = _fill_owners(spans)
    return _smooth_spans(filled)


def _fill_owners(
    spans: list[tuple[float, float, int | None]],
) -> list[tuple[float, float, int]]:
    last: int | None = None
    forward: list[tuple[float, float, int | None]] = []
    for left, right, owner in spans:
        if owner is None:
            owner = last
        else:
            last = owner
        forward.append((left, right, owner))
    following: int | None = None
    for index in range(len(forward) - 1, -1, -1):
        left, right, owner = forward[index]
        if owner is None:
            forward[index] = (left, right, following)
        else:
            following = owner
    return [(left, right, int(owner)) for left, right, owner in forward if owner is not None]


def _merge_spans(spans: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    merged: list[tuple[float, float, int]] = []
    for left, right, owner in spans:
        if merged and merged[-1][2] == owner:
            merged[-1] = (merged[-1][0], right, owner)
        else:
            merged.append((left, right, owner))
    return merged


def _smooth_spans(spans: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """The same "too short to be a turn" rule, on stretches instead of words."""
    current = _merge_spans(spans)
    while len(current) > 1:
        candidates = [
            index
            for index in range(len(current))
            if _absorbable(
                index,
                len(current),
                0 < index < len(current) - 1 and current[index - 1][2] == current[index + 1][2],
                current[index][1] - current[index][0],
            )
        ]
        if not candidates:
            break
        shortest = min(candidates, key=lambda i: current[i][1] - current[i][0])
        left, right, _owner = current[shortest]
        before = current[shortest - 1] if shortest > 0 else None
        after = current[shortest + 1] if shortest + 1 < len(current) else None
        if before is None:
            winner = after[2]  # type: ignore[index]
        elif after is None:
            winner = before[2]
        else:
            winner = before[2] if (before[1] - before[0]) >= (after[1] - after[0]) else after[2]
        current[shortest] = (left, right, winner)
        current = _merge_spans(current)
    return current


def _split_by_time(
    segment: dict[str, Any], spans: list[tuple[float, float, int]], names: dict[int, str]
) -> list[dict[str, Any]]:
    """Cut a segment without word timings — by time, snapped to a sentence.

    A guess, and the honest place to say so: the position comes from the share
    of the segment's *duration* that has passed, and is then moved to the
    nearest sentence end (or, failing that, the nearest space) so the cut at
    least falls between two words. Where that cannot be done without leaving a
    piece without text, the segment stays whole and only gets the name of the
    voice that dominates it.
    """
    text = segment["text"]
    start, end = float(segment["start_s"]), float(segment["end_s"])
    cuts: list[int] = []
    for span in spans[1:]:
        cut = _snap(text, _proportional(text, start, end, span[0]))
        if cuts and cut <= cuts[-1]:
            continue
        cuts.append(cut)
    bounds = [0, *cuts, len(text)]
    parts = [text[left:right].strip() for left, right in zip(bounds, bounds[1:], strict=False)]
    if len(parts) != len(spans) or not all(parts):
        return [_whole(segment, names[max(spans, key=lambda s: s[1] - s[0])[2]])]
    return [
        {
            "start_s": start if index == 0 else spans[index][0],
            "end_s": end if index == len(spans) - 1 else spans[index][1],
            "text": part,
            "speaker": names[spans[index][2]],
            "words": "",  # the timings described the whole segment, not this piece
        }
        for index, part in enumerate(parts)
    ]


def _proportional(text: str, start: float, end: float, moment: float) -> int:
    span = end - start
    if span <= 0:
        return len(text) // 2
    return round(len(text) * (moment - start) / span)


def _snap(text: str, target: int) -> int:
    """Move a cut position to the nearest sentence end, else the nearest space.

    Both candidates are positions of a space, so the cut always falls between
    two words; the pieces are stripped afterwards. Searching outward from the
    computed position means the closest sentence end wins, and an earlier one
    beats an equally distant later one.
    """
    window = max(6, int(len(text) * SNAP_SHARE))
    for wanted_sentence_end in (True, False):
        for offset in range(window + 1):
            for index in (target - offset, target + offset):
                if not 0 < index < len(text) or text[index] != " ":
                    continue
                if not wanted_sentence_end or text[index - 1] in _SENTENCE_END:
                    return index
    return min(max(target, 1), max(1, len(text) - 1))


# ── applying it, and the job around it ───────────────────────────────


def apply_to_file(file_id: int, turns: list[Turn]) -> dict[str, int]:
    """Write the recognition into the transcript; returns what it changed."""
    segments = transcripts.list_segments(file_id)
    plan = plan_speakers(segments, turns, prefix=speaker_prefix())
    splits = transcripts.apply_speaker_plan(file_id, plan)
    named = sum(1 for entry in plan for _piece in entry["pieces"])
    return {
        "speakers": len({turn.speaker for turn in turns}),
        "segments": named,
        "splits": splits,
    }


def wanted_for(file_row: dict[str, Any]) -> bool:
    """Whether this file's transcript type asks for the speaker recognition."""
    project = workspace.get_project(file_row["project_id"])
    return project_types.diarizes(project_types.for_file(project, file_row))


def enqueue(file_id: int, *, session_id: str = "", chain: bool = False) -> dict[str, Any]:
    """Queue a recognition.

    `chain` marks the run that follows a transcription: only that one goes on
    to the LLM steps afterwards, because only there is the transcript new.
    """
    payload: dict[str, Any] = {"chain": True} if chain else {}
    file_row = workspace.get_file(file_id)
    return job_queue.enqueue(
        "diarize",
        payload=payload,
        file_id=file_id,
        project_id=(file_row or {}).get("project_id"),
        session_id=session_id,
    )


def handle_diarize_job(job: dict[str, Any], cancel: threading.Event, report: Any) -> None:
    file_id = job["file_id"]
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise RuntimeError(f"File {file_id} no longer exists")
    audio_path = workspace.file_path(file_row)
    if not audio_path.exists():
        raise RuntimeError(f"Audio file is missing: {audio_path}")
    payload = job.get("payload") or {}
    name = file_row["filename"]

    report(0, f"Erkenne Sprecher in {name} ...")
    turns = run(
        audio_path,
        cancel=cancel,
        report=report,
        label=f"Sprecher in {name}",
        progress=(2, 90),
    )
    if turns:
        report(92, f"{name}: Sprecher werden zugeordnet ...")
        # `transcripts.sync_after_change` announces the new segments; what
        # this run *found* is the job's own progress line, like every other step
        result = apply_to_file(file_id, turns)
        detail = f"{result['speakers']} Sprecher erkannt"
        if result["splits"]:
            detail += f", {result['splits']} Segmente unterteilt"
    else:
        # Silence, music, or a recording the segmentation heard nobody in. The
        # transcript stays as it is — and the steps below still have to run:
        # they were held back for this job, and a recording without a
        # recognisable voice must not cost them.
        detail = "keine Sprecher erkannt"

    _hand_on(file_id, payload, job.get("session_id") or "")
    report(100, f"{name}: {detail}")


def _hand_on(file_id: int, payload: dict[str, Any], session_id: str) -> None:
    """Let the steps that were waiting for this run.

    Both of them read the segments this job rewrites, which is why the
    transcription hands them over instead of starting them itself
    (services/whisper.py). The LLM steps only follow the *chained* run: a
    recognition somebody started by hand in the editor is not a request to
    process the file.
    """
    from .pipeline import maybe_enqueue_auto_process
    from .vectorstore import maybe_enqueue_index

    maybe_enqueue_index(file_id, session_id=session_id)
    if payload.get("chain"):
        maybe_enqueue_auto_process(file_id, session_id=session_id)
