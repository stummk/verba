"""Keeping a file's section whole: the page break in a compilation export.

A compilation lets one file follow the next on the same page — which tears a
section apart wherever the page happens to end. A transcript type can ask for
the other layout (`keep_sections`): a file that no longer fits completely
starts on a new page, together with the translations that belong to it. What
it must not do is waste paper: sections that fit stay where they are, and a
section longer than a page is never preceded by an empty one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verba.services import pdf

LINE = (
    "Dies ist ein Satz, der in der Ausgabe etwa eine ganze Zeile breit wird und "
    "damit die Seite füllt. "
)


def doc(title: str, paragraphs: int, *, translation: bool = False) -> dict:
    """One file's document, sized in paragraphs of roughly three lines each."""
    entry = {
        "title": title,
        "date": "",
        "header_left": title,
        "header_middle": "",
        "header_right": "",
        "blocks": [{"kind": "paragraph", "text": LINE * 2} for _ in range(paragraphs)],
    }
    if translation:
        # what `_file_documents` marks a language version of the file before it
        entry.update({"header_left": "", "divider": True})
    return entry


@pytest.fixture()
def section_starts(monkeypatch, tmp_path):
    """Render and report which page each file's section really starts on.

    The measuring pass lays the same section out into a dummy document, so
    only the renders outside of it count — those are the ones on paper.
    """

    # taken once: a second run would otherwise wrap the first run's spy
    original_fits = pdf._Renderer._fits_here
    original_group = pdf._Renderer.group

    def run(docs: list[dict], keep_sections: bool | None, name: str = "out.pdf") -> list[int]:
        starts: list[int] = []
        measuring = 0

        def fits(self, group):
            nonlocal measuring
            measuring += 1
            try:
                return original_fits(self, group)
            finally:
                measuring -= 1

        def group(self, group_docs):
            if not measuring:
                starts.append(self.pdf.page)
            return original_group(self, group_docs)

        monkeypatch.setattr(pdf._Renderer, "_fits_here", fits)
        monkeypatch.setattr(pdf._Renderer, "group", group)
        target = Path(tmp_path) / name
        # None: called the way the export called it before this existed
        if keep_sections is None:
            pdf.render_pdf(docs, "paragraphs", target)
        else:
            pdf.render_pdf(docs, "paragraphs", target, keep_sections=keep_sections)
        assert target.read_bytes().startswith(b"%PDF")
        return starts

    return run


# ── which documents belong to one section ─────────────────────────────


def test_a_file_and_its_translations_are_one_section():
    docs = [doc("A", 1), doc("B", 1), doc("B-en", 1, translation=True), doc("C", 1)]
    groups = pdf.section_groups(docs)
    assert [[entry["title"] for entry in group] for group in groups] == [
        ["A"],
        ["B", "B-en"],
        ["C"],
    ]


def test_a_stray_translation_does_not_disappear():
    """Nothing is dropped even if the first document carries the marker."""
    groups = pdf.section_groups([doc("B-en", 1, translation=True), doc("C", 1)])
    assert [len(group) for group in groups] == [1, 1]


# ── the layout ────────────────────────────────────────────────────────


def test_without_the_option_a_section_starts_wherever_the_last_one_ended(section_starts):
    assert section_starts([doc("A", 14), doc("B", 10), doc("C", 15)], False) == [1, 1, 2]


def test_with_the_option_a_section_that_does_not_fit_starts_on_a_new_page(section_starts):
    assert section_starts([doc("A", 14), doc("B", 10), doc("C", 15)], True) == [1, 2, 3]


def test_sections_that_fit_still_share_a_page(section_starts):
    """The break is for sections that would be torn, not for every file."""
    assert section_starts([doc("A", 3), doc("B", 3), doc("C", 3)], True) == [1, 1, 1]


def test_a_translation_that_would_not_fit_moves_its_original_along(section_starts):
    """The case that asks for this: the transcript fits, the translation does not."""
    docs = [doc("A", 14), doc("B", 5), doc("B-en", 8, translation=True)]
    assert section_starts(docs, False) == [1, 1]
    assert section_starts(docs, True) == [1, 2]


def test_a_section_longer_than_a_page_gets_no_empty_page_in_front(section_starts):
    """It cannot fit anywhere, so breaking would only cost a sheet of paper."""
    assert section_starts([doc("A", 60)], True) == [1]
    # behind another section it still moves to the top of the next page
    assert section_starts([doc("A", 5), doc("B", 60)], True) == [1, 2]


def test_a_renderer_that_is_told_nothing_lays_out_as_it_always_did(section_starts):
    """The option is opt-in: without it, the layout is the one from before."""
    docs = [doc("A", 14), doc("B", 10), doc("C", 15)]
    assert section_starts(docs, None) == section_starts(docs, False, "again.pdf")
