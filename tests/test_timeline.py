"""The interval arithmetic every audio edit rests on.

A cut recording is described by the spans that are kept, and both the ffmpeg
command and the new segment timestamps are derived from those spans. So this is
the layer where an off-by-a-fraction shows up as audio missing from a file the
user cannot get back — it is tested on its own, without ffmpeg or a database.
"""

from __future__ import annotations

from verba.services import timeline

WHOLE = [(0.0, 20.0)]


# ── normalize ─────────────────────────────────────────────────────────


def test_spans_come_back_sorted_and_merged():
    spans = timeline.normalize([(10.0, 12.0), (0.0, 5.0), (4.0, 6.0)])
    assert spans == [(0.0, 6.0), (10.0, 12.0)]


def test_a_reversed_drag_is_read_as_a_span():
    """Dragging right to left hands over end < start."""
    assert timeline.normalize([(8.0, 3.0)]) == [(3.0, 8.0)]


def test_a_span_is_clamped_to_the_recording():
    assert timeline.normalize([(-4.0, 25.0)], duration=20.0) == [(0.0, 20.0)]


def test_a_mis_drag_of_a_millisecond_is_dropped():
    assert timeline.normalize([(3.0, 3.002)]) == []


def test_spans_that_all_but_touch_become_one():
    """Two selections dragged edge to edge must not leave a sliver behind."""
    assert timeline.normalize([(0.0, 5.0), (5.005, 9.0)]) == [(0.0, 9.0)]


# ── set operations ────────────────────────────────────────────────────


def test_removing_a_passage_leaves_the_two_sides():
    assert timeline.subtract(WHOLE, [(5.0, 8.0)]) == [(0.0, 5.0), (8.0, 20.0)]


def test_removing_at_the_front_leaves_the_remainder():
    assert timeline.subtract(WHOLE, [(0.0, 5.0)]) == [(5.0, 20.0)]


def test_removing_several_passages_at_once():
    assert timeline.subtract(WHOLE, [(2.0, 4.0), (10.0, 12.0)]) == [
        (0.0, 2.0),
        (4.0, 10.0),
        (12.0, 20.0),
    ]


def test_removing_a_passage_twice_changes_nothing_the_second_time():
    once = timeline.subtract(WHOLE, [(5.0, 8.0)])
    assert timeline.subtract(once, [(5.0, 8.0)]) == once


def test_keeping_only_the_selection_drops_everything_else():
    assert timeline.intersect(WHOLE, [(5.0, 8.0)]) == [(5.0, 8.0)]


def test_keeping_several_selections_keeps_exactly_those():
    assert timeline.intersect(WHOLE, [(2.0, 4.0), (10.0, 12.0)]) == [(2.0, 4.0), (10.0, 12.0)]


def test_keeping_a_selection_inside_an_already_cut_recording():
    """The second edit works on what the first one left, not on the original."""
    after_cut = timeline.subtract(WHOLE, [(5.0, 8.0)])
    assert timeline.intersect(after_cut, [(4.0, 10.0)]) == [(4.0, 5.0), (8.0, 10.0)]


def test_the_kept_time_adds_up():
    assert timeline.total([(0.0, 5.0), (8.0, 20.0)]) == 17.0


# ── is there anything to do at all ────────────────────────────────────


def test_an_untouched_recording_needs_no_cut():
    assert timeline.covers_all(WHOLE, 20.0) is True


def test_a_selection_dragged_to_the_very_edge_still_counts_as_untouched():
    assert timeline.covers_all([(0.005, 19.995)], 20.0) is True


def test_a_recording_with_a_hole_in_it_needs_a_cut():
    assert timeline.covers_all([(0.0, 5.0), (8.0, 20.0)], 20.0) is False


def test_an_unreadable_duration_never_claims_there_is_nothing_to_cut():
    """A container ffprobe cannot read must not become an uneditable file."""
    assert timeline.covers_all(WHOLE, None) is False


# ── original position → position after the cuts ───────────────────────

CUT = [(0.0, 5.0), (8.0, 20.0)]  # 5–8 removed


def test_a_position_before_the_cut_stays_where_it_is():
    assert timeline.map_time(CUT, 3.0) == 3.0


def test_a_position_after_the_cut_moves_up_by_what_was_removed():
    assert timeline.map_time(CUT, 10.0) == 7.0


def test_a_position_inside_the_removed_passage_has_no_place():
    assert timeline.map_time(CUT, 6.0) is None


def test_the_seam_belongs_to_the_span_that_ends_there():
    assert timeline.map_time(CUT, 5.0) == 5.0
    assert timeline.map_time(CUT, 8.0) == 5.0


def test_two_cuts_shift_by_both():
    keeps = timeline.subtract(WHOLE, [(2.0, 4.0), (10.0, 12.0)])
    assert timeline.map_time(keeps, 15.0) == 11.0


# ── segments follow the audio ─────────────────────────────────────────


def test_a_segment_after_the_cut_moves_up_whole():
    assert timeline.map_span(CUT, 10.0, 12.0) == (7.0, 9.0)


def test_a_segment_the_cut_runs_through_keeps_what_is_left_of_it():
    """4–12 of the original: 5–8 goes, so 4–5 and 8–12 survive as 4–9."""
    assert timeline.map_span(CUT, 4.0, 12.0) == (4.0, 9.0)


def test_a_segment_inside_the_removed_passage_is_gone():
    assert timeline.map_span(CUT, 6.0, 7.0) is None


def test_a_segment_of_which_a_sliver_survives_is_gone_too():
    """Less than MIN_SPAN left is not a segment any more, it is a rounding error."""
    assert timeline.map_span(CUT, 4.999, 7.0) is None
