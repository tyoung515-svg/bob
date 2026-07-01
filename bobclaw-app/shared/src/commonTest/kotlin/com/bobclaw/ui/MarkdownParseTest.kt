package com.bobclaw.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class MarkdownParseTest {

    // ---- parseBlocks ----

    @Test
    fun parse_empty_string_returns_empty_list() {
        assertEquals(emptyList<MdBlock>(), parseBlocks(""))
    }

    @Test
    fun parse_headings_levels_one_to_six() {
        assertEquals(MdBlock.Heading(1, "H1"), parseBlocks("# H1").single())
        assertEquals(MdBlock.Heading(2, "H2"), parseBlocks("## H2").single())
        assertEquals(MdBlock.Heading(3, "H3"), parseBlocks("### H3").single())
        assertEquals(MdBlock.Heading(4, "H4"), parseBlocks("#### H4").single())
        assertEquals(MdBlock.Heading(5, "H5"), parseBlocks("##### H5").single())
        assertEquals(MdBlock.Heading(6, "H6"), parseBlocks("###### H6").single())
    }

    @Test
    fun parse_hashtag_without_space_is_paragraph() {
        assertEquals(MdBlock.Paragraph("#hashtag"), parseBlocks("#hashtag").single())
    }

    @Test
    fun parse_seven_hashes_is_paragraph() {
        assertEquals(MdBlock.Paragraph("####### seven"), parseBlocks("####### seven").single())
    }

    @Test
    fun parse_closed_fenced_code_block() {
        val blocks = parseBlocks("```kotlin\nfoo()\nbar()\n```")
        assertEquals(1, blocks.size)
        assertEquals(MdBlock.Code("foo()\nbar()"), blocks[0])
    }

    @Test
    fun parse_unclosed_fenced_code_runs_to_end_without_throwing() {
        val blocks = parseBlocks("```python\nprint(1)\nprint(2)")
        assertEquals(1, blocks.size)
        assertEquals(MdBlock.Code("print(1)\nprint(2)"), blocks[0])
    }

    @Test
    fun parse_unordered_list_items() {
        assertEquals(MdBlock.ListItem("•", "dash"), parseBlocks("- dash").single())
        assertEquals(MdBlock.ListItem("•", "star"), parseBlocks("* star").single())
    }

    @Test
    fun parse_ordered_list_items() {
        assertEquals(MdBlock.ListItem("1.", "first"), parseBlocks("1. first").single())
        assertEquals(MdBlock.ListItem("2.", "second"), parseBlocks("2) second").single())
    }

    @Test
    fun parse_blockquote() {
        assertEquals(MdBlock.Quote("quote"), parseBlocks("> quote").single())
    }

    @Test
    fun parse_horizontal_rules() {
        assertEquals(MdBlock.Rule, parseBlocks("---").single())
        assertEquals(MdBlock.Rule, parseBlocks("***").single())
        assertEquals(MdBlock.Rule, parseBlocks("___").single())
    }

    @Test
    fun parse_double_dash_is_paragraph_not_rule() {
        assertEquals(MdBlock.Paragraph("--"), parseBlocks("--").single())
    }

    @Test
    fun parse_consecutive_non_blank_lines_join_into_one_paragraph() {
        assertEquals(
            MdBlock.Paragraph("line one line two"),
            parseBlocks("line one\nline two").single(),
        )
    }

    @Test
    fun parse_crlf_input_normalizes_without_stray_cr() {
        val blocks = parseBlocks("# Title\r\nbody")
        assertEquals(MdBlock.Heading(1, "Title"), blocks[0])
        assertEquals(MdBlock.Paragraph("body"), blocks[1])
        // CRLF join must not leave a stray '\r' in the merged paragraph text.
        assertEquals(MdBlock.Paragraph("a b"), parseBlocks("a\r\nb").single())
    }

    // ---- inlineAnnotated ----

    @Test
    fun inline_bold_span() {
        val s = inlineAnnotated("**bold**", Color.White, Color.Cyan)
        assertEquals("bold", s.text)
        assertEquals(1, s.spanStyles.size)
        assertEquals(0, s.spanStyles[0].start)
        assertEquals(4, s.spanStyles[0].end)
        assertEquals(SpanStyle(fontWeight = FontWeight.Bold), s.spanStyles[0].item)
    }

    @Test
    fun inline_italic_star_span() {
        val s = inlineAnnotated("*italic*", Color.White, Color.Cyan)
        assertEquals("italic", s.text)
        assertEquals(1, s.spanStyles.size)
        assertEquals(SpanStyle(fontStyle = FontStyle.Italic), s.spanStyles[0].item)
    }

    @Test
    fun inline_italic_underscore_span() {
        val s = inlineAnnotated("_italic_", Color.White, Color.Cyan)
        assertEquals("italic", s.text)
        assertEquals(1, s.spanStyles.size)
        assertEquals(SpanStyle(fontStyle = FontStyle.Italic), s.spanStyles[0].item)
    }

    @Test
    fun inline_code_span() {
        val s = inlineAnnotated("`code`", Color.White, Color.Cyan)
        assertEquals("code", s.text)
        assertEquals(1, s.spanStyles.size)
        assertEquals(FontFamily.Monospace, s.spanStyles[0].item.fontFamily)
    }

    @Test
    fun inline_link_span() {
        // The link color is now threaded in as an explicit param (the accent is a composable-only
        // accessor, unreadable from a non-composable test). Assert the span uses THAT color.
        val link = Color(0xFF2DD4BF)
        val s = inlineAnnotated("[label](http://example.com)", Color.White, link)
        assertEquals("label", s.text)
        assertEquals(1, s.spanStyles.size)
        assertEquals(link, s.spanStyles[0].item.color)
    }

    @Test
    fun inline_unmatched_star_is_literal() {
        val s = inlineAnnotated("a * b", Color.White, Color.Cyan)
        assertTrue(s.text.contains("*"))
        assertEquals(0, s.spanStyles.size)
    }

    @Test
    fun inline_empty_string_is_empty_annotated_string() {
        val s = inlineAnnotated("", Color.White, Color.Cyan)
        assertEquals("", s.text)
        assertTrue(s.spanStyles.isEmpty())
    }
}
