"""observatory.py's rendering layer: the seven section renderers, the top
anchor table of contents, and render_page()'s full-document assembly (the
seven loaders themselves, and the island/back-compat-alias contracts they
carry, are tests/test_observatory.py's job -- this file is the render task
that follows them).

Every fixture in this file is FICTIONAL, continuing tests/test_observatory.py's
invented couple -- "Wren" (the companion) and "Ivy" (the person) -- with
fresh invented material of its own (a lantern festival, a broken umbrella,
a stray cat named Soot). No fixture quotes any real diary, conversation,
reflection, kept moment, or fact.

Tests call the private per-section renderers (`observatory._render_*`)
directly where that gives the most direct pin on one section's rules, the
same way tests/test_observatory.py already calls the private
`observatory._memory_db_uri` -- this module does not treat a leading
underscore as untestable. render_page() itself, the only new PUBLIC
symbol this task adds, gets its own assembly-level tests.
"""
import unittest

from everthine import observatory, portrait_viewer

XSS_PAYLOAD = "<script>alert(1)</script>"
XSS_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


# ---------------------------------------------------------------------
# Fixture helpers -- one small builder per loader's output shape
# ---------------------------------------------------------------------

def _portrait(updated, content, opinions=None, observations=None):
    return {"updated": updated, "content": content,
            "opinions": opinions or [], "observations": observations or []}


def _diary(entry_date, content, mood="", keywords=None):
    return {"date": entry_date, "content": content, "mood": mood,
            "keywords": keywords or []}


def _reflection(created_at, text):
    return {"created_at": created_at, "text": text}


def _kept(direction, speaker, message, timestamp=""):
    return {"direction": direction, "speaker": speaker, "message": message,
            "timestamp": timestamp}


def _fact(category, text, fact_date=""):
    return {"category": category, "date": fact_date, "text": text}


def _msg(msg_date, speaker, text, timestamp=""):
    return {"date": msg_date, "speaker": speaker, "text": text, "timestamp": timestamp}


def _stats(chunk_count, earliest_ts, latest_ts, db_size_bytes):
    return {"chunk_count": chunk_count, "earliest_ts": earliest_ts,
            "latest_ts": latest_ts, "db_size_bytes": db_size_bytes}


def _empty_sections():
    """A fully-shaped, all-empty sections_data bundle -- render_page's
    baseline fixture; tests override individual keys from here."""
    return {
        "portraits": [], "diary": [], "reflections": [], "album": [],
        "facts": [], "facts_cursor": None,
        "conversation": [], "elder_days": 0, "elder_msgs": 0,
        "memory_stats": None,
    }


# ---------------------------------------------------------------------
# portrait_viewer's three publicized renderers: the alias contract
# ---------------------------------------------------------------------

class PublicizedRendererAliasTest(unittest.TestCase):
    def test_render_content_alias_is_the_same_function(self):
        self.assertIs(portrait_viewer._render_content, portrait_viewer.render_content)

    def test_render_positions_alias_is_the_same_function(self):
        self.assertIs(portrait_viewer._render_positions, portrait_viewer.render_positions)

    def test_render_notes_alias_is_the_same_function(self):
        self.assertIs(portrait_viewer._render_notes, portrait_viewer.render_notes)


# ---------------------------------------------------------------------
# 1. Portrait section
# ---------------------------------------------------------------------

class RenderPortraitSectionTest(unittest.TestCase):
    def test_empty_state(self):
        html = observatory._render_portrait_section([])
        self.assertIn(observatory.EMPTY_PORTRAIT, html)
        self.assertNotIn("Version", html)

    def test_latest_card_uses_last_entry_and_reused_renderers(self):
        portraits = [
            _portrait("2026-07-01", "first take"),
            _portrait("2026-07-08", "I hum while Ivy reads",
                      opinions=[{"topic": "lanterns", "opinion": "worth the walk"}],
                      observations=["counts umbrellas at the door"]),
        ]
        html = observatory._render_portrait_section(portraits)
        self.assertIn("Version 2 · 2026-07-08", html)
        self.assertIn("I hum while Ivy reads", html)
        self.assertIn(portrait_viewer.SECTION_POSITIONS, html)
        self.assertIn(portrait_viewer.SECTION_NOTES, html)
        self.assertIn("counts umbrellas at the door", html)
        self.assertNotIn("first take", html)  # only the latest snapshot renders

    def test_history_line_omitted_when_only_one_version(self):
        html = observatory._render_portrait_section([_portrait("2026-07-01", "only one")])
        self.assertIn("Version 1 · 2026-07-01", html)
        self.assertNotIn("earlier version(s)", html)

    def test_history_line_present_with_correct_count(self):
        portraits = [_portrait(f"2026-07-0{i}", f"take {i}") for i in range(1, 4)]
        html = observatory._render_portrait_section(portraits)
        self.assertIn(
            "2 earlier version(s) — run python -m everthine.portrait_viewer "
            "for the full timeline.", html)

    def test_content_is_escaped(self):
        html = observatory._render_portrait_section([_portrait("2026-07-01", XSS_PAYLOAD)])
        self.assertNotIn("<script", html)
        self.assertIn(XSS_ESCAPED, html)


# ---------------------------------------------------------------------
# 2. Diary section
# ---------------------------------------------------------------------

class RenderDiarySectionTest(unittest.TestCase):
    def test_empty_state(self):
        html = observatory._render_diary_section([])
        self.assertIn(observatory.EMPTY_DIARY, html)

    def test_cards_render_in_the_order_given(self):
        entries = [_diary("2026-07-01", "first"), _diary("2026-07-02", "second")]
        html = observatory._render_diary_section(entries)
        self.assertLess(html.index("first"), html.index("second"))
        self.assertIn(">2026-07-01<", html)
        self.assertIn(">2026-07-02<", html)

    def test_mood_line_present_when_non_empty(self):
        html = observatory._render_diary_section([_diary("2026-07-01", "x", mood="wistful")])
        self.assertIn("Mood: wistful", html)

    def test_mood_line_absent_when_empty(self):
        html = observatory._render_diary_section([_diary("2026-07-01", "x", mood="")])
        self.assertNotIn("Mood:", html)

    def test_keywords_line_joins_with_middle_dot(self):
        html = observatory._render_diary_section(
            [_diary("2026-07-01", "x", keywords=["lantern", "festival", "umbrella"])])
        self.assertIn("Keywords: lantern · festival · umbrella", html)

    def test_keywords_line_absent_when_empty(self):
        html = observatory._render_diary_section([_diary("2026-07-01", "x", keywords=[])])
        self.assertNotIn("Keywords:", html)

    def test_content_rendered_via_render_content_paragraphs(self):
        html = observatory._render_diary_section(
            [_diary("2026-07-01", "line a\nline b\n\npara two")])
        self.assertIn("line a<br>line b", html)
        self.assertIn("<p>para two</p>", html)

    def test_content_and_keywords_are_escaped(self):
        html = observatory._render_diary_section(
            [_diary("2026-07-01", XSS_PAYLOAD, keywords=[XSS_PAYLOAD])])
        self.assertNotIn("<script", html)
        self.assertEqual(html.count(XSS_ESCAPED), 2)


# ---------------------------------------------------------------------
# 3. Reflections section
# ---------------------------------------------------------------------

class RenderReflectionsSectionTest(unittest.TestCase):
    def test_empty_state(self):
        html = observatory._render_reflections_section([])
        self.assertIn(observatory.EMPTY_REFLECTIONS, html)

    def test_eyebrow_is_first_ten_characters_of_created_at(self):
        html = observatory._render_reflections_section(
            [_reflection("2026-07-08T21:30:00+08:00", "a thought")])
        self.assertIn(">2026-07-08<", html)
        self.assertNotIn("21:30:00", html)

    def test_short_created_at_kept_as_is(self):
        html = observatory._render_reflections_section([_reflection("2026", "short clock")])
        self.assertIn(">2026<", html)

    def test_blank_created_at_kept_as_is(self):
        html = observatory._render_reflections_section([_reflection("", "no clock at all")])
        self.assertIn('<p class="eyebrow"></p>', html)

    def test_text_rendered_via_render_content_and_escaped(self):
        html = observatory._render_reflections_section([_reflection("2026-07-08", XSS_PAYLOAD)])
        self.assertNotIn("<script", html)
        self.assertIn(XSS_ESCAPED, html)

    def test_list_order_preserved(self):
        entries = [_reflection("2026-07-01", "first"), _reflection("2026-07-02", "second")]
        html = observatory._render_reflections_section(entries)
        self.assertLess(html.index("first"), html.index("second"))


# ---------------------------------------------------------------------
# 4. Keepsakes section
# ---------------------------------------------------------------------

class RenderKeepsakesSectionTest(unittest.TestCase):
    def test_empty_state(self):
        html = observatory._render_keepsakes_section([])
        self.assertIn(observatory.EMPTY_KEEPSAKES, html)

    def test_partner_flagged_label(self):
        html = observatory._render_keepsakes_section(
            [_kept("partner_flagged", "companion", "saved you the last lantern")])
        self.assertIn("You kept this", html)

    def test_companion_flagged_label(self):
        html = observatory._render_keepsakes_section(
            [_kept("companion_flagged", "user", "the umbrella broke, we laughed anyway")])
        self.assertIn("They kept this", html)

    def test_unknown_direction_shown_raw_and_escaped_no_crash(self):
        html = observatory._render_keepsakes_section(
            [_kept("mystery" + XSS_PAYLOAD, "companion", "kept anyway")])
        self.assertNotIn("<script", html)
        self.assertIn("mystery" + XSS_ESCAPED, html)

    def test_byline_present_with_speaker_and_timestamp(self):
        html = observatory._render_keepsakes_section(
            [_kept("partner_flagged", "companion", "kept moment",
                   timestamp="2026-07-08T20:00:00+08:00")])
        self.assertIn("— companion, 2026-07-08T20:00:00+08:00", html)

    def test_byline_omitted_when_speaker_blank(self):
        html = observatory._render_keepsakes_section(
            [_kept("partner_flagged", "", "kept without a speaker on record",
                   timestamp="2026-07-08T20:00:00+08:00")])
        self.assertNotIn("—", html)
        self.assertNotIn("2026-07-08T20:00:00+08:00", html)

    def test_message_rendered_via_render_content_and_escaped(self):
        html = observatory._render_keepsakes_section(
            [_kept("partner_flagged", "companion", XSS_PAYLOAD)])
        self.assertNotIn("<script", html)
        self.assertIn(XSS_ESCAPED, html)

    def test_speaker_and_timestamp_are_escaped(self):
        html = observatory._render_keepsakes_section(
            [_kept("partner_flagged", XSS_PAYLOAD, "kept", timestamp=XSS_PAYLOAD)])
        self.assertNotIn("<script", html)
        self.assertEqual(html.count(XSS_ESCAPED), 2)


# ---------------------------------------------------------------------
# 5. Facts section
# ---------------------------------------------------------------------

class RenderFactsSectionTest(unittest.TestCase):
    def test_empty_state(self):
        html = observatory._render_facts_section([], None)
        self.assertIn(observatory.EMPTY_FACTS, html)

    def test_known_categories_render_in_fixed_order_regardless_of_storage_order(self):
        # Seeded out of FACT_CATEGORY_ORDER on purpose (conflict, then
        # interest, then mood): output must follow the fixed group order,
        # not the storage order.
        facts = [
            _fact("conflict", "a small disagreement about the umbrella"),
            _fact("interest", "likes lantern festivals"),
            _fact("mood", "brighter after the festival"),
        ]
        html = observatory._render_facts_section(facts, None)
        self.assertLess(html.index("interest"), html.index("mood"))
        self.assertLess(html.index("mood"), html.index("conflict"))

    def test_unknown_category_sorted_after_all_known_groups(self):
        facts = [_fact("interest", "likes tea"), _fact("gift_idea", "wants a new umbrella")]
        html = observatory._render_facts_section(facts, None)
        self.assertLess(html.index(">interest<"), html.index(">gift_idea<"))

    def test_multiple_unknown_categories_kept_in_first_seen_order(self):
        facts = [_fact("zeta_bucket", "z fact"), _fact("alpha_bucket", "a fact")]
        html = observatory._render_facts_section(facts, None)
        self.assertLess(html.index("zeta_bucket"), html.index("alpha_bucket"))

    def test_group_keeps_storage_order_within_itself(self):
        facts = [_fact("interest", "second-written but kept in order", fact_date="2026-07-02"),
                 _fact("interest", "first-written", fact_date="2026-07-01")]
        html = observatory._render_facts_section(facts, None)
        self.assertLess(html.index("second-written"), html.index("first-written"))

    def test_date_span_present_when_date_non_empty(self):
        html = observatory._render_facts_section(
            [_fact("interest", "likes tea", fact_date="2026-07-01")], None)
        self.assertIn('<span class="fact-date">(2026-07-01)</span>', html)

    def test_date_span_omitted_when_date_empty(self):
        html = observatory._render_facts_section([_fact("interest", "likes tea")], None)
        self.assertIn("<li>likes tea</li>", html)
        self.assertNotIn("fact-date", html)

    def test_cursor_line_shown_when_truthy(self):
        html = observatory._render_facts_section(
            [_fact("interest", "x")], "2026-07-08T21:00:00+08:00")
        self.assertIn("Last gathered: 2026-07-08T21:00:00+08:00", html)

    def test_cursor_line_omitted_when_none(self):
        html = observatory._render_facts_section([_fact("interest", "x")], None)
        self.assertNotIn("Last gathered", html)

    def test_cursor_line_omitted_when_never_extracted_sentinel(self):
        # Controller's 2026-07-12 refinement: "" is load_facts_cursor's own
        # never-extracted sentinel, not a failure -- but it still means no
        # "Last gathered" line, same as None.
        html = observatory._render_facts_section([_fact("interest", "x")], "")
        self.assertNotIn("Last gathered", html)

    def test_cursor_line_independent_of_empty_facts_list(self):
        # A cursor can exist (extraction has run) even when nothing was
        # judged worth keeping yet -- the footer is not folded into the
        # empty-state branch.
        html = observatory._render_facts_section([], "2026-07-08T21:00:00+08:00")
        self.assertIn(observatory.EMPTY_FACTS, html)
        self.assertIn("Last gathered: 2026-07-08T21:00:00+08:00", html)

    def test_text_category_and_cursor_are_escaped(self):
        html = observatory._render_facts_section(
            [_fact(XSS_PAYLOAD, XSS_PAYLOAD, fact_date=XSS_PAYLOAD)], XSS_PAYLOAD)
        self.assertNotIn("<script", html)
        self.assertEqual(html.count(XSS_ESCAPED), 4)


# ---------------------------------------------------------------------
# 6. Conversation section
# ---------------------------------------------------------------------

class RenderConversationSectionTest(unittest.TestCase):
    def test_empty_state(self):
        html = observatory._render_conversation_section([], 0, 0)
        self.assertIn(observatory.EMPTY_CONVERSATION, html)

    def test_days_grouped_under_h3_headers_in_order_given(self):
        window = [_msg("2026-07-07", "user", "first day"),
                  _msg("2026-07-08", "companion", "second day")]
        html = observatory._render_conversation_section(window, 0, 0)
        self.assertIn("<h3>2026-07-07</h3>", html)
        self.assertIn("<h3>2026-07-08</h3>", html)
        self.assertLess(html.index("first day"), html.index("second day"))

    def test_multiple_lines_same_day_share_one_h3(self):
        window = [_msg("2026-07-07", "user", "hi"), _msg("2026-07-07", "companion", "hello")]
        html = observatory._render_conversation_section(window, 0, 0)
        self.assertEqual(html.count("<h3>2026-07-07</h3>"), 1)

    def test_speaker_labels_user_and_companion(self):
        window = [_msg("2026-07-07", "user", "hi"), _msg("2026-07-07", "companion", "hello")]
        html = observatory._render_conversation_section(window, 0, 0)
        self.assertIn('<span class="who">You</span> hi', html)
        self.assertIn('<span class="who">Them</span> hello', html)

    def test_unknown_speaker_shown_raw(self):
        window = [_msg("2026-07-07", "narrator", "an aside")]
        html = observatory._render_conversation_section(window, 0, 0)
        self.assertIn('<span class="who">narrator</span> an aside', html)

    def test_elder_notice_absent_when_elder_days_zero(self):
        html = observatory._render_conversation_section(
            [_msg("2026-07-07", "user", "hi")], 0, 5)
        self.assertNotIn("Earlier:", html)

    def test_elder_notice_shown_when_elder_days_positive(self):
        html = observatory._render_conversation_section(
            [_msg("2026-07-07", "user", "hi")], 3, 12)
        self.assertIn("Earlier: 3 more day(s), 12 more line(s) — not shown here.", html)

    def test_elder_notice_shown_even_when_window_empty(self):
        html = observatory._render_conversation_section([], 3, 12)
        self.assertIn("Earlier: 3 more day(s), 12 more line(s) — not shown here.", html)
        self.assertIn(observatory.EMPTY_CONVERSATION, html)

    def test_speaker_and_text_are_escaped(self):
        window = [_msg("2026-07-07", XSS_PAYLOAD, XSS_PAYLOAD)]
        html = observatory._render_conversation_section(window, 0, 0)
        self.assertNotIn("<script", html)
        self.assertEqual(html.count(XSS_ESCAPED), 2)

    def test_date_heading_is_escaped(self):
        window = [_msg(XSS_PAYLOAD, "user", "hi")]
        html = observatory._render_conversation_section(window, 0, 0)
        self.assertNotIn("<script", html)
        self.assertIn(XSS_ESCAPED, html)


# ---------------------------------------------------------------------
# 7. Memory room section
# ---------------------------------------------------------------------

class RenderMemorySectionTest(unittest.TestCase):
    def test_unavailable_when_none(self):
        html = observatory._render_memory_section(None)
        self.assertIn(observatory.MEMORY_UNAVAILABLE, html)

    def test_three_lines_when_present(self):
        stats = _stats(128, "2026-06-01T00:00:00+08:00", "2026-07-08T00:00:00+08:00", 2_048_000)
        html = observatory._render_memory_section(stats)
        self.assertIn("Remembered fragments: 128", html)
        self.assertIn("Covering: 2026-06-01T00:00:00+08:00 → 2026-07-08T00:00:00+08:00", html)
        self.assertIn("Index size: 2,048,000 bytes", html)

    def test_covering_line_omitted_when_timestamps_none(self):
        # The honest all-zero dict load_memory_stats returns for an
        # empty-but-valid chunks table (controller's 2026-07-12 note):
        # chunk_count 0 is still real data and still renders; only the
        # Covering line -- gated on both timestamps -- disappears.
        stats = _stats(0, None, None, 4096)
        html = observatory._render_memory_section(stats)
        self.assertIn("Remembered fragments: 0", html)
        self.assertNotIn("Covering", html)
        self.assertIn("Index size: 4,096 bytes", html)

    def test_covering_line_omitted_when_only_one_timestamp_none(self):
        stats = _stats(3, "2026-06-01T00:00:00+08:00", None, 4096)
        html = observatory._render_memory_section(stats)
        self.assertNotIn("Covering", html)

    def test_db_size_bytes_thousands_separator(self):
        html = observatory._render_memory_section(_stats(1, "a", "b", 999))
        self.assertIn("Index size: 999 bytes", html)

    def test_timestamps_are_escaped(self):
        stats = _stats(1, XSS_PAYLOAD, XSS_PAYLOAD, 10)
        html = observatory._render_memory_section(stats)
        self.assertNotIn("<script", html)
        self.assertEqual(html.count(XSS_ESCAPED), 2)


# ---------------------------------------------------------------------
# render_page(): full-document assembly
# ---------------------------------------------------------------------

class RenderPageTest(unittest.TestCase):
    def test_all_seven_section_ids_present(self):
        html = observatory.render_page(_empty_sections())
        for section_id in ("portrait", "diary", "reflections", "keepsakes",
                            "facts", "conversation", "memory"):
            self.assertIn(f'<section id="{section_id}">', html)

    def test_toc_has_seven_links_matching_section_ids(self):
        html = observatory.render_page(_empty_sections())
        for section_id, title in observatory.SECTION_ORDER:
            self.assertIn(f'<nav class="toc">', html)
            self.assertIn(f'<a href="#{section_id}">{title}</a>', html)

    def test_h2_titles_match_toc_labels(self):
        html = observatory.render_page(_empty_sections())
        for _section_id, title in observatory.SECTION_ORDER:
            self.assertIn(f"<h2>{title}</h2>", html)

    def test_sections_appear_in_the_d2_reading_order(self):
        html = observatory.render_page(_empty_sections())
        positions = [html.index(f'<section id="{sid}">') for sid, _ in observatory.SECTION_ORDER]
        self.assertEqual(positions, sorted(positions))

    def test_page_chrome_title_and_subtitle(self):
        html = observatory.render_page(_empty_sections())
        self.assertIn("<title>Observatory</title>", html)
        self.assertIn("<h1>Observatory</h1>", html)
        self.assertIn(
            '<p class="subtitle">A quiet window on their inner life — kept on '
            'this computer, for your eyes only.</p>', html)

    def test_missing_key_raises_key_error(self):
        incomplete = _empty_sections()
        del incomplete["memory_stats"]
        with self.assertRaises(KeyError):
            observatory.render_page(incomplete)

    def test_no_script_tag_anywhere(self):
        html = observatory.render_page(_empty_sections())
        self.assertNotIn("<script", html)

    def test_no_external_references_on_empty_page(self):
        html = observatory.render_page(_empty_sections())
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("url(", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("src=", html)

    def test_no_external_references_with_populated_sections(self):
        sections = _empty_sections()
        sections.update({
            "portraits": [_portrait("2026-07-01", "a quiet week",
                                     opinions=[{"topic": "tea", "opinion": "warm"}],
                                     observations=["hums at dusk"])],
            "diary": [_diary("2026-07-01", "the lantern festival ran late",
                              mood="content", keywords=["lanterns"])],
            "reflections": [_reflection("2026-07-01T21:00:00+08:00", "a quiet thought")],
            "album": [_kept("partner_flagged", "companion", "kept this",
                             timestamp="2026-07-01T20:00:00+08:00")],
            "facts": [_fact("interest", "likes lantern festivals", fact_date="2026-07-01")],
            "facts_cursor": "2026-07-01T21:00:00+08:00",
            "conversation": [_msg("2026-07-01", "user", "hello Wren")],
            "elder_days": 2, "elder_msgs": 9,
            "memory_stats": _stats(10, "2026-06-01T00:00:00+08:00",
                                    "2026-07-01T00:00:00+08:00", 40960),
        })
        html = observatory.render_page(sections)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("url(", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("src=", html)

    def test_full_page_smoke_all_populated_sections_render_their_content(self):
        sections = _empty_sections()
        sections.update({
            "portraits": [_portrait("2026-07-01", "a quiet week")],
            "diary": [_diary("2026-07-01", "the lantern festival ran late")],
            "reflections": [_reflection("2026-07-01T21:00:00+08:00", "a quiet thought")],
            "album": [_kept("partner_flagged", "companion", "kept this")],
            "facts": [_fact("interest", "likes lantern festivals")],
            "conversation": [_msg("2026-07-01", "user", "hello Wren")],
            "memory_stats": _stats(10, "2026-06-01T00:00:00+08:00",
                                    "2026-07-01T00:00:00+08:00", 40960),
        })
        html = observatory.render_page(sections)
        self.assertIn("a quiet week", html)
        self.assertIn("the lantern festival ran late", html)
        self.assertIn("a quiet thought", html)
        self.assertIn("kept this", html)
        self.assertIn("likes lantern festivals", html)
        self.assertIn("hello Wren", html)
        self.assertIn("Remembered fragments: 10", html)

    def test_empty_page_shows_all_seven_empty_sentences(self):
        html = observatory.render_page(_empty_sections())
        for message in (observatory.EMPTY_PORTRAIT, observatory.EMPTY_DIARY,
                        observatory.EMPTY_REFLECTIONS, observatory.EMPTY_KEEPSAKES,
                        observatory.EMPTY_FACTS, observatory.EMPTY_CONVERSATION,
                        observatory.MEMORY_UNAVAILABLE):
            self.assertIn(message, html)

    def test_doctype_and_meta_present(self):
        html = observatory.render_page(_empty_sections())
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)


if __name__ == "__main__":
    unittest.main()
