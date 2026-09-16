# Learning from corrections — implementation plan

Verba transcribes, cleans up and translates; the user corrects the result. Today
that correction is worth nothing beyond the one file it was made in: the next
recording starts from zero, and the only way to teach Verba anything is to type
it into a transcript type's prompt by hand.

This plan closes that loop. What the user would otherwise write into a type
prompt is derived from their corrections in the background, proposed to them,
and — once accepted — applied to later recordings. The user never has to write a
prompt, and can still overrule everything.

Status: **phases 1 to 4 done, phase 5 planned.** Phases are meant to ship one
at a time; each is useful on its own.


## Design decisions

These are settled. The reasoning matters because several of them look like
avoidable restrictions until you see what they prevent.

**1. Learning happens out of band, never inside the pipeline.**
A background job distils rules and writes them down; the cleanup run then reads
a stored prompt exactly as it does today. The alternative — retrieving similar
passages and injecting them at run time — would make the same file come back
differently on a second run, because the corpus grew in between. The pipeline's
reproducibility is worth more than the marginal quality of per-chunk retrieval.

**2. Scope is the user, with an installation-wide switch.**
Learned material carries an `owner_id`; `0` means installation-wide. Without
user management (`auth.enabled` off — the normal case) there is exactly one
scope and everything is global, which is the point of the feature. With user
management on, each user learns across *all their own* projects by default, so
nothing derived from a private transcript reaches another user's prompt. An
admin can switch the installation to one shared scope
(`LearningSettings.shared`), which is a deliberate decision to make learned
spellings visible to everyone.

**3. Two tiers, because they leak differently.**

| Tier | Example | Nature |
|---|---|---|
| Vocabulary | `Jahwe` → `Yahweh` | Content. A proper noun out of someone's transcript |
| Rules | "Keep filler words" | Content-free. Generalises away from the material |

Vocabulary is stored as word pairs and never phrased by an LLM. Rules are
distilled by an LLM and must survive a content-free filter: a rule may only use
words that occur across many different files. Anything quoting a rare name
belongs in the vocabulary tier, not in a rule. Under a shared scope that filter
is the privacy boundary, not a quality nicety.

**4. Nothing applies before it is accepted.**
Everything learned starts as `proposed`. The distillation writes proposals; the
user accepts, edits or discards. This is the safety valve for the one failure
mode an LLM distiller cannot avoid on its own — confidently turning a one-off
edit ("deleted this paragraph, it was private") into a standing rule.

**5. Counter-evidence retires a rule.**
Every rule and term carries `evidence` and `counter_evidence`. A later
correction that contradicts a rule counts against it; more counter-evidence than
evidence retires it automatically. This is what protects against imprinting —
the first few recordings in a fresh installation otherwise decide everything,
and a manual "discard all" is a poor remedy for something noticed at file 12.


## Phase 1 — Capture the delta ✔

Nothing is learned yet. Worth shipping on its own: it is the basis for a
"changed by you" marker in the editor and for an undo.

**Migration** (additive, in `_SCHEMA` and `db._migrate`):

```
derived_texts.model_content  TEXT NOT NULL DEFAULT ''
derived_texts.edited_at      TEXT NOT NULL DEFAULT ''
segments.text_original       TEXT NOT NULL DEFAULT ''
segments.edited_at           TEXT NOT NULL DEFAULT ''
```

`segments.edited_at` is not symmetry for its own sake. Without it there is no
way to tell a row the transcription wrote from one the user typed
(`create_segment`, for a passage the recognition missed) — and correcting the
latter would keep the user's own first wording as "what the transcription
heard". A user-authored row carries `edited_at` from the start, so no freeze
ever happens on it.

**Freeze on the first edit only.** SQLite evaluates `SET` expressions against
the old row, so one statement does it — in `pipeline.update_text_content` and,
in the same shape, in `transcripts.update_segment`:

```sql
UPDATE derived_texts SET
  content       = ?,
  model_content = CASE WHEN edited_at = '' THEN content ELSE model_content END,
  edited_at     = datetime('now')
WHERE file_id = ? AND kind = ? AND language = ?
```

A speaker rename is not a text correction and must leave both markers alone —
otherwise the frozen "before" equals the "after" and no real correction of
that segment can ever be recorded afterwards.

**Clear it where the model writes again.** `pipeline.save_text` resets both
columns explicitly — a fresh model run carries no user edit, and pairing the
new text with an older model's version of a text that no longer exists would
be a delta between two strangers. On the segment side nothing had to be added:
everything that rewrites a transcript wholesale (`whisper._store_segments`,
`transcripts.replace_all_segments`, and the splitting half of
`apply_speaker_plan`) deletes and re-inserts, so the rows come back with empty
markers by construction. The single-piece path of `apply_speaker_plan` writes
only the speaker, so a correction there stays valid. The tests pin all of it,
which is what would catch a future refactor to updating in place.

**Tests** (`tests/test_learning_capture.py`, 14): the first edit freezes the
model version, a second leaves it alone, translations are tracked per
language, a re-run clears it, a speaker rename and a moved segment do not
count, a user-typed segment claims no transcription, re-transcribing and a
speaker split discard the pair, and an existing database gains the columns
with its rows intact.


**Done when:** for every corrected text and segment, both versions are on disk.


## Phase 2 — Vocabulary ✔

Still no LLM. The cheapest and safest half of the whole feature.

New module `backend/verba/services/learning.py`.

```
learned_terms(
    id, owner_id, language, source, target,
    evidence, counter_evidence, status, last_seen_at, created_at,
    UNIQUE (owner_id, language, source)
)
```

`owner_id` is `0` for the whole installation rather than `NULL`, because
SQLite counts NULLs as distinct in a UNIQUE constraint and the upsert would
never dedupe. There is no foreign key to match — `auth.delete_user` removes a
departing account's terms explicitly.

`language` is not in the original sketch and had to be added: without it a
Russian name is handed to a German recording as a hotword, which actively
makes the transcription worse. It comes from `files.language`; a term whose
recording declared none stays eligible everywhere.

**Derivation:** `difflib.SequenceMatcher` over the token lists of
`text_original` ↔ `text`, per edit rather than against the model's original,
so each correction contributes its own delta. Only 1:1 word replacements are
collected, and only where the corrected word still *resembles* the one it
replaces (`min_similarity`) — "Jahwe" → "Yahweh" is a mishearing, "sagt" →
"spricht" is a rewording and would be nonsense as a hint to the decoder.
Retyping a whole segment produces one block opcode and therefore teaches
nothing at all, which is better than the cap it was meant to need. Rows the
user created themselves are skipped: they carry no `text_original`.

**Contradiction, not just confirmation.** A term carries `evidence` and
`counter_evidence`. Correcting the same word differently counts against the
stored pair, and once contradicted as often as confirmed the newer wording
takes over; correcting it back the other way retires it, and confirming it
again brings it back. That, not the "discard everything" button, is what
keeps a spelling picked up from the first few recordings from being the
installation's truth for good.

**Application, in two places:**

1. **Whisper.** `model.transcribe()` is called today with `language` and
   `beam_size` only (`services/whisper.py`). Passing the learned target forms as
   `hotwords` means the name is *heard* correctly next time instead of being
   repaired afterwards by an LLM. Verify that the pinned faster-whisper version
   supports `hotwords`; `initial_prompt` is the fallback.
2. **Glossary.** Merged into `overview.Overview.as_glossary()`, which is how
   spellings become stable across files rather than being guessed per file.

**A cap at the point of use, not in storage.** A vocabulary grown over years,
handed to Whisper in full, makes transcription *worse* — the decoder is bent
towards hundreds of names that do not occur in this recording. The list is
truncated to the top N by evidence and recency when it is passed. This
requirement exists only because the store is global.

A term applies once it has been seen `min_evidence` times, or as soon as the
user accepts it. "rejected" is sticky: more evidence must never overrule a
person.

**UI:** not next to the transcript types after all — that view is
administrators only, and this vocabulary belongs to whoever corrected it. It
is a card in the settings instead, present in both the full and the
restricted variant, so every user reaches their own list. Reject, allow again,
forget.

**Done when:** a name corrected in file 1 is spelled correctly by Whisper in
file 2.

**Known gap, still open from phase 1:** a re-transcribed selection
(`POST /files/{id}/transcribe-range`) reaches the backend as an ordinary
segment edit and is learned from although both sides are machine-written. The
similarity filter catches the worst of it, but the route should say where the
text came from.


## Phase 3 — Distil rules ✔

Job kind `learn_rules`, added to `core/jobs.LLM_KINDS`, registered in
`main.create_app()` alongside the other handlers.

```
learned_rules(
    id, owner_id, step, text, norm,
    evidence, counter_evidence, status, created_at,
    UNIQUE (owner_id, step, norm)
)
    step   ∈ {cleanup, translate}
    status ∈ {proposed, accepted, rejected, retired}
```

The column is `step`, not `scope` as first sketched — "scope" already means
the `owner_id` here, and two meanings for one word in one table is how the
next reader gets it wrong. `norm` is the rule folded for comparison, so the
same rule found again is confirmed rather than stored twice; it travels with
the text when a user rewords a rule.

**What it learns from.** The corrected *derived texts*, not the segments: a
segment correction is about the transcription, which has no prompt to put a
rule into — that is what the vocabulary of phase 2 is for. So `cleanup` rules
come from corrected cleanups and `translate` rules from corrected
translations, which is exactly where phase 4 will inject them. Only the
paragraphs that came back *changed* are sent, capped in number and length; a
paragraph merely added or deleted has no counterpart to compare and on its own
is content rather than an instruction.

**Trigger:** `learning.request_rule_learning(file_row, kind)`, modelled on
`maintenance.request_vacuum()` and called from `pipeline.update_text_content`.
It does nothing until `min_corrected_files` distinct files have been corrected
since the last run, and nothing at all without a configured LLM.
`job_queue.has_active` is kind-wide, so `job_queue.active_jobs(kind)` was
added: one distillation per scope *and step*, rather than one per
installation.

**The call:** pairs of (model version ↔ user version) in, a JSON array of short
imperative rules out, read with the existing `llm.json_arrays`.

**Guard rails.** These are the substance of this phase:

| Risk | Countermeasure |
|---|---|
| A rule from a single edit | The model reports `seen` per rule and anything below `min_rule_evidence` is dropped |
| Content instead of a rule | A rule using a word of the material that occurs in fewer than `min_rule_files` distinct files is rejected |
| Rule sprawl | `max_rules` per scope and step; a new proposal displaces the weakest stored rule, and only with more evidence |
| Silent application | Status `proposed`. Nothing takes effect before the user accepts it |

Only the second of those is a measurement. `seen` is the model's own count —
a claim, not a proof — which is why it is never the only thing standing
between an answer and a prompt: the content check is weighed against the
material, and a person still has to accept the rule. Words the material does
not use at all are the model's ordinary language and pass; the check is aimed
at the one thing that can leak, which is the material's own vocabulary.

**UI:** above the spellings in the same settings card, under its own
subheading that says these are proposals. Accept, stop using, discard.

**Not yet wired in.** An accepted rule is recorded and does nothing until
phase 4 appends it to the prompts. Automatic counter-evidence for rules waits
for the same phase: nothing contradicts a rule until it has been applied and
the result corrected again. The column is already there.

**Done when:** after a handful of corrections a readable proposal stands in the
UI that can be accepted or dismissed.


## Phase 4 — Apply, and close the loop ✔

`learning.rules_block(owner_id, scope)` returns the accepted rules; it is
appended behind `pipeline.TYPE_CONTEXT_HEADING` in `cleanup_segments` and
`run_translation`.

- **Budget.** The block competes with the chunk for context. It gets a hard
  character cap and is the first thing dropped when `SizeLimit` already had to
  split — on a 1.7B local model that decides between a result and a
  `TruncatedAnswer`.
- **Traceability.** Which rules a run used is written alongside the result —
  `derived_texts.rules_used`, the ids rather than the hash the sketch called
  for. A hash answers "was it the same set"; the ids answer "which ones", and
  that is what the counter-evidence below needs anyway.
- **Counter-evidence, at last.** A correction of a text produced under rules
  counts against exactly those rules, and one written against as often as it
  was attested retires itself. Nothing could contradict a rule before it was
  applied, which is why design decision 5 had to wait for this phase.
- **The existing promise is untouched.** `pipeline._keeps_the_text` still
  guarantees that a verbatim type does not summarise. A learned rule cannot
  override that, and should not be able to.

**Alongside:** `project_types.translate_prompt` (migration, backfill marker,
field in `types.js`). The rationale in the sketch — "a baseline for the learned
layer to attach to" — turned out not to hold: the learned layer attaches to the
scope, not to the type. It is worth doing on its own account, because until now
no type could say anything about how its translations should sound.
`pipeline.TRANSLATE_SYSTEM_PROMPT` keeps only what every translation owes; the
register rule moved to `project_types.DEFAULT_TRANSLATE_PROMPT`, which is what
a type without one falls back to, so nothing changed for an existing
installation.

**A trap worth recording.** Inserting `translate_prompt` into the middle of
`create_type`/`update_type` shifted every positional caller silently — a test
went on building a type with `verbatim` where `keep_sections` belonged and
`condense` permanently off. Everything past the prompts is keyword-only now, so
the next insertion fails loudly instead.

**Done when:** an accepted rule demonstrably changes the next cleanup, and a
test shows it.


## Phase 5 — Switch off, explain, maintain

- `LearningSettings` in `config.py` exists since phase 2 (`enabled`, `shared`,
  `min_evidence`, `min_similarity`, `max_hotwords`, `max_glossary_chars`);
  what is missing is a way to reach it from the settings form. Switched off,
  the behaviour is exactly today's.
- "Discard everything learned", per scope.
- User guide in **all three** languages (`docs/user/{de,en,ru}.md`), same
  section at the same position, `{#slug}` marker, icon in `SECTION_ICONS`.


## Across all phases

- Focused tests first, then the full suite and both Ruff checks; Ubuntu **and**
  Windows green in CI before a phase counts as done.
- Every new route through `api/deps.py`; every cross-project query joins
  `auth.visibility_clause()`.
- Backend texts shown in the web UI in German, code, logs and comments in
  English, UI strings from the i18n catalog (de/en/ru).
- No `px` in styling; new frontend files added to the `SHELL` list in `sw.js`.


## Tuning

The thresholds are reasoned, not measured — like the search distance cutoffs,
they should be the first suspect when the feature behaves oddly:

| Threshold | First guess |
|---|---|
| Supporting deltas for a rule | 2 |
| Distinct files a rule's words must occur in | 3 |
| Corrected files before distilling | 3 |
| Changed characters before distilling | 2000 |
| Rules per scope | 10 |
| Character cap of the rules block | 800 |
| Hotwords passed to Whisper | 100 |

Two things make measuring them later cheap rather than a rewrite: they all live
in `LearningSettings` instead of being scattered through the code, and every
term and rule carries its `evidence` and `counter_evidence` counts, so the data
for a real measurement is already there when someone wants to do it.
