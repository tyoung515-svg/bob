package com.bobclaw.ui

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class ArtifactsTest {

    // ---- extractHtmlArtifact ----

    @Test
    fun html_artifact_plain_prose_returns_null() {
        assertNull(extractHtmlArtifact("Just some prose, no fence here."))
    }

    @Test
    fun html_artifact_single_html_block_returns_trimmed_body() {
        val text = "```html\n<div>hello</div>\n```"
        assertEquals("<div>hello</div>", extractHtmlArtifact(text))
    }

    @Test
    fun html_artifact_svg_without_html_is_wrapped() {
        val text = "```svg\n<svg></svg>\n```"
        assertEquals(
            "<!doctype html><html><body style=\"margin:0\"><svg></svg></body></html>",
            extractHtmlArtifact(text),
        )
    }

    @Test
    fun html_artifact_svg_already_containing_html_returned_as_is() {
        val text = "```svg\n<html><body><svg/></body></html>\n```"
        assertEquals(
            "<html><body><svg/></body></html>",
            extractHtmlArtifact(text),
        )
    }

    @Test
    fun html_artifact_two_html_blocks_returns_last() {
        val text = "```html\nfirst\n```\nprose\n```html\nsecond\n```"
        assertEquals("second", extractHtmlArtifact(text))
    }

    @Test
    fun html_artifact_whitespace_only_body_returns_null() {
        val text = "```html\n   \t  \n```"
        assertNull(extractHtmlArtifact(text))
    }

    @Test
    fun html_artifact_html_then_svg_takes_svg_wrapped() {
        val text = "```html\n<h1>x</h1>\n```\n```svg\n<circle/>\n```"
        assertEquals(
            "<!doctype html><html><body style=\"margin:0\"><circle/></body></html>",
            extractHtmlArtifact(text),
        )
    }

    // ---- extractFileArtifact ----

    @Test
    fun file_artifact_backticked_windows_html_path() {
        val text = "Wrote `C:\\projects\\demo\\out\\hello.html`"
        assertEquals("C:\\projects\\demo\\out\\hello.html", extractFileArtifact(text))
    }

    @Test
    fun file_artifact_no_path_returns_null() {
        assertNull(extractFileArtifact("no paths here at all"))
    }

    @Test
    fun file_artifact_two_paths_returns_last() {
        val text = "see C:\\a\\b.html and C:\\c\\d.svg"
        assertEquals("C:\\c\\d.svg", extractFileArtifact(text))
    }

    @Test
    fun file_artifact_htm_and_svg_extensions_match() {
        assertEquals("C:\\x\\y.htm", extractFileArtifact("file: C:\\x\\y.htm"))
        assertEquals("C:\\x\\z.svg", extractFileArtifact("file: C:\\x\\z.svg"))
    }

    @Test
    fun file_artifact_txt_extension_returns_null() {
        assertNull(extractFileArtifact("see C:\\x\\y.txt"))
    }

    @Test
    fun file_artifact_trailing_delimiter_does_not_bleed() {
        assertEquals("C:\\x\\y.svg", extractFileArtifact("`C:\\x\\y.svg`"))
        assertEquals("C:\\x\\y.svg", extractFileArtifact("\"C:\\x\\y.svg\""))
        assertEquals("C:\\x\\y.svg", extractFileArtifact("'C:\\x\\y.svg'"))
    }
}
