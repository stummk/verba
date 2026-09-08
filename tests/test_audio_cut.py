"""Cutting a recording in place: the file, the transcript, and the way back.

This is the one operation in Verba that destroys user material, so what is
tested here is not the ffmpeg call (that is `build_keep_command`, covered in
tests/test_api_segments.py) but everything around it: that the untouched
recording and its transcript are kept aside before the first cut, that the
segments follow the audio instead of pointing at moments that no longer exist,
and that "restore the original" really puts both back.

ffmpeg is replaced by a stub that writes the file it is told to write — a real
one would make these tests depend on a downloaded binary and on decoding time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verba import config, db
from verba.services import audio, timeline, transcripts, workspace

SEGMENTS = [
    (0.0, 4.0, "eins"),  # before the cut
    (6.0, 7.0, "raus"),  # inside the removed passage
    (10.0, 14.0, "drei"),  # after the cut
]
KEEPS = [(0.0, 5.0), (8.0, 20.0)]  # 5–8 is removed


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.general.audio_backup = True
    config.save_settings(settings)
    db.init_db()

    # ffmpeg stands in for itself: the command it is given is checked in
    # tests/test_api_segments.py, here only its effect matters — a file where
    # the command says one should appear
    monkeypatch.setattr(audio, "_run", lambda cmd: Path(cmd[-1]).write_bytes(b"cut audio"))
    monkeypatch.setattr(audio, "_ffmpeg", lambda: "ffmpeg")
    # the new duration is read back from the container, which the stub above
    # does not write — so it is answered here
    monkeypatch.setattr(audio, "probe_duration", lambda path: timeline.total(KEEPS))
    return tmp_path


@pytest.fixture()
def file_row(env, monkeypatch):
    monkeypatch.setattr("verba.services.workspace.probe_duration", lambda path: 20.0)
    project = workspace.create_project("Schnitt")
    source = env / "rede.mp3"
    source.write_bytes(b"original audio")
    [row] = workspace.import_paths(project, [str(source)])
    for start, end, text in SEGMENTS:
        transcripts.create_segment(row["id"], start, end, text=text)
    with db.get_conn() as conn:
        conn.execute("UPDATE files SET duration = 20 WHERE id = ?", (row["id"],))
    return workspace.get_file(row["id"])


def texts_and_spans(file_id: int) -> list[tuple[str, float, float]]:
    return [
        (row["text"], row["start_s"], row["end_s"]) for row in transcripts.list_segments(file_id)
    ]


# ── the cut ───────────────────────────────────────────────────────────


def test_the_cut_rewrites_the_recording_and_makes_no_second_file(file_row):
    audio_dir = workspace.file_path(file_row).parent
    audio.apply_keeps(file_row, KEEPS)
    assert [p.name for p in sorted(audio_dir.glob("*.mp3"))] == ["rede.mp3"]
    assert workspace.file_path(file_row).read_bytes() == b"cut audio"


def test_no_temporary_file_is_left_behind(file_row):
    audio.apply_keeps(file_row, KEEPS)
    audio_dir = workspace.file_path(file_row).parent
    assert list(audio_dir.glob("*cut-tmp*")) == []


def test_the_duration_follows_the_cut(file_row):
    updated = audio.apply_keeps(file_row, KEEPS)
    assert updated["duration"] == pytest.approx(17.0)


def test_the_segments_move_with_the_audio(file_row):
    """The removed passage takes its segment; what follows moves up by 3s."""
    audio.apply_keeps(file_row, KEEPS)
    assert texts_and_spans(file_row["id"]) == [
        ("eins", 0.0, 4.0),
        ("drei", 7.0, 11.0),
    ]


def test_the_segments_are_renumbered_without_a_gap(file_row):
    audio.apply_keeps(file_row, KEEPS)
    assert [row["idx"] for row in transcripts.list_segments(file_row["id"])] == [0, 1]


def test_the_workspace_copy_is_rewritten(file_row):
    audio.apply_keeps(file_row, KEEPS)
    project = workspace.get_project(file_row["project_id"])
    copy = workspace.project_dir(project) / "transcripts" / "rede.json"
    written = json.loads(copy.read_text(encoding="utf-8"))
    assert [s["text"] for s in written["segments"]] == ["eins", "drei"]


def test_a_cut_that_removes_nothing_leaves_the_file_alone(file_row):
    audio.apply_keeps(file_row, [(0.0, 20.0)])
    assert workspace.file_path(file_row).read_bytes() == b"original audio"


def test_a_recording_cannot_be_cut_down_to_nothing(file_row):
    with pytest.raises(ValueError):
        audio.apply_keeps(file_row, [])


# ── the way back ──────────────────────────────────────────────────────


def test_the_untouched_recording_is_kept_aside_before_the_first_cut(file_row):
    audio.apply_keeps(file_row, KEEPS)
    kept = workspace.file_path(file_row).parent / audio.ORIGINAL_DIR / "rede.mp3"
    assert kept.read_bytes() == b"original audio"
    assert audio.has_original(workspace.get_file(file_row["id"])) is True


def test_a_second_cut_does_not_overwrite_the_kept_original(file_row):
    """Otherwise the "original" would soon be the result of the first cut."""
    audio.apply_keeps(file_row, KEEPS)
    audio.apply_keeps(workspace.get_file(file_row["id"]), [(0.0, 10.0)])
    kept = workspace.file_path(file_row).parent / audio.ORIGINAL_DIR / "rede.mp3"
    assert kept.read_bytes() == b"original audio"


def test_restoring_brings_back_the_audio_and_the_transcript(file_row):
    audio.apply_keeps(file_row, KEEPS)
    restored = audio.restore_original(workspace.get_file(file_row["id"]))

    assert workspace.file_path(file_row).read_bytes() == b"original audio"
    assert texts_and_spans(file_row["id"]) == [
        ("eins", 0.0, 4.0),
        ("raus", 6.0, 7.0),  # the segment the cut had dropped
        ("drei", 10.0, 14.0),
    ]
    assert restored["duration"] == pytest.approx(20.0)


def test_restoring_leaves_nothing_to_restore_a_second_time(file_row):
    audio.apply_keeps(file_row, KEEPS)
    audio.restore_original(workspace.get_file(file_row["id"]))
    assert audio.has_original(workspace.get_file(file_row["id"])) is False


def test_restoring_an_untouched_recording_is_refused(file_row):
    with pytest.raises(RuntimeError):
        audio.restore_original(file_row)


def test_with_the_backup_switched_off_nothing_is_kept(file_row):
    settings = config.get_settings()
    settings.general.audio_backup = False
    config.save_settings(settings)

    audio.apply_keeps(file_row, KEEPS)

    folder = workspace.file_path(file_row).parent / audio.ORIGINAL_DIR
    assert not folder.exists()
    assert audio.has_original(workspace.get_file(file_row["id"])) is False


def test_a_cut_that_fails_leaves_neither_a_backup_nor_a_changed_file(file_row, monkeypatch):
    """Otherwise a missing ffmpeg would offer to "restore" an untouched file."""
    monkeypatch.setattr(
        audio, "_ffmpeg", lambda: (_ for _ in ()).throw(RuntimeError("ffmpeg fehlt"))
    )

    with pytest.raises(RuntimeError):
        audio.apply_keeps(file_row, KEEPS)

    assert workspace.file_path(file_row).read_bytes() == b"original audio"
    assert not (workspace.file_path(file_row).parent / audio.ORIGINAL_DIR).exists()
    assert audio.has_original(workspace.get_file(file_row["id"])) is False


def test_a_cut_whose_replace_fails_leaves_no_backup_marker(file_row, monkeypatch):
    """The recording is unchanged, so nothing may offer to restore it."""

    def fail_replace(src, dst):
        raise PermissionError("the file is open elsewhere")

    monkeypatch.setattr(audio.os, "replace", fail_replace)

    with pytest.raises(PermissionError):
        audio.apply_keeps(file_row, KEEPS)

    assert workspace.file_path(file_row).read_bytes() == b"original audio"
    assert audio.has_original(workspace.get_file(file_row["id"])) is False
    assert not (workspace.file_path(file_row).parent / audio.ORIGINAL_DIR).exists()


def test_a_failing_second_cut_keeps_the_backup_of_the_first(file_row, monkeypatch):
    """Only a backup made by the failed run itself is taken back."""
    audio.apply_keeps(file_row, KEEPS)  # succeeds, and keeps the original

    def fail_replace(src, dst):
        raise PermissionError("the file is open elsewhere")

    monkeypatch.setattr(audio.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        audio.apply_keeps(workspace.get_file(file_row["id"]), [(0.0, 4.0)])

    kept = workspace.file_path(file_row).parent / audio.ORIGINAL_DIR / "rede.mp3"
    assert kept.read_bytes() == b"original audio"
    assert audio.has_original(workspace.get_file(file_row["id"])) is True


def test_deleting_the_file_takes_the_kept_original_with_it(file_row):
    """A full copy of the recording must not stay behind unreferenced."""
    audio.apply_keeps(file_row, KEEPS)
    folder = workspace.file_path(file_row).parent / audio.ORIGINAL_DIR
    assert list(folder.iterdir())  # the backup and its segment snapshot

    workspace.delete_file(file_row["id"])

    assert not folder.exists()
    assert workspace.get_file(file_row["id"]) is None
