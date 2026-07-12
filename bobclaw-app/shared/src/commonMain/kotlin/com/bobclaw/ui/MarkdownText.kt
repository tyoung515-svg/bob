package com.bobclaw.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/**
 * Hand-rolled markdown renderer for assistant chat replies. NO external library — we are pinned
 * to Compose Multiplatform 1.6.11 / Kotlin 2.0.21 and any extra Compose lib clashes at runtime.
 *
 * Two stages:
 *  1) [parseBlocks] walks the text line-by-line into a list of [MdBlock] (headings, fenced code,
 *     lists, blockquotes, horizontal rules, paragraphs). Total: any input — empty, malformed, or
 *     a streaming reply with an unclosed code fence — produces blocks and never throws.
 *  2) Each block renders with Compose Text/AnnotatedString. Inline spans (`**bold**`, `*italic*`,
 *     `_italic_`, `` `code` ``, `[label](url)`) are parsed by [inlineAnnotated], a single
 *     left-to-right scan that emits literal text for any unmatched marker.
 *
 * commonMain-safe: no java.* APIs.
 */
@Composable
fun MarkdownText(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = BoBClawColors.TextPrimary,
) {
    val blocks = remember(text) { parseBlocks(text) }
    // Resolve the link color AND the bundled mono face in composable scope (both are now
    // CompositionLocal-backed) and thread them down to the NON-composable inline parser as plain params.
    val linkColor = LocalBoBClawColors.accent
    val codeFont = BoBClawType.mono
    Column(modifier = modifier) {
        blocks.forEachIndexed { index, block ->
            if (index > 0) Spacer(Modifier.height(6.dp))
            RenderBlock(block, color, linkColor, codeFont)
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Block model
// ---------------------------------------------------------------------------------------------

internal sealed interface MdBlock {
    data class Heading(val level: Int, val text: String) : MdBlock
    data class Paragraph(val text: String) : MdBlock
    data class Code(val code: String) : MdBlock
    data class Quote(val text: String) : MdBlock
    /** One list item; ordered [marker] is "N." rendered as-is, unordered is the bullet "•". */
    data class ListItem(val marker: String, val text: String) : MdBlock
    object Rule : MdBlock
}

// ---------------------------------------------------------------------------------------------
// Block parser
// ---------------------------------------------------------------------------------------------

/**
 * Split [text] into blocks. Total function — never throws, handles empty string and a trailing
 * UNCLOSED ``` fence (streaming) by treating the rest of the text as code through end-of-input.
 */
internal fun parseBlocks(text: String): List<MdBlock> {
    if (text.isEmpty()) return emptyList()
    // Normalize CRLF/CR so line handling is uniform.
    val lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    val blocks = ArrayList<MdBlock>()
    var i = 0
    while (i < lines.size) {
        val raw = lines[i]
        val trimmed = raw.trim()

        // --- fenced code block (``` optionally followed by a lang tag) ---
        if (trimmed.startsWith("```")) {
            val codeLines = ArrayList<String>()
            i++ // consume the opening fence (lang tag, if any, is dropped)
            while (i < lines.size) {
                if (lines[i].trim().startsWith("```")) {
                    i++ // consume the closing fence
                    break
                }
                codeLines.add(lines[i])
                i++
            }
            // An unclosed fence (streaming) simply runs to end-of-text — text is never dropped.
            blocks.add(MdBlock.Code(codeLines.joinToString("\n")))
            continue
        }

        // --- blank line: block separator ---
        if (trimmed.isEmpty()) {
            i++
            continue
        }

        // --- horizontal rule (--- / *** / ___, 3+ of the same char) ---
        if (isHorizontalRule(trimmed)) {
            blocks.add(MdBlock.Rule)
            i++
            continue
        }

        // --- heading (#..###### then a space) ---
        val heading = parseHeading(trimmed)
        if (heading != null) {
            blocks.add(heading)
            i++
            continue
        }

        // --- blockquote (> ) ---
        if (trimmed.startsWith(">")) {
            blocks.add(MdBlock.Quote(trimmed.removePrefix(">").trimStart()))
            i++
            continue
        }

        // --- unordered list (- or * followed by a space) ---
        if (isUnorderedItem(trimmed)) {
            blocks.add(MdBlock.ListItem(marker = "•", text = trimmed.substring(2).trimStart()))
            i++
            continue
        }

        // --- ordered list (digits then . or ) then a space) ---
        val ordered = parseOrderedItem(trimmed)
        if (ordered != null) {
            blocks.add(ordered)
            i++
            continue
        }

        // --- paragraph: gather consecutive non-blank, non-structural lines ---
        val paraLines = ArrayList<String>()
        while (i < lines.size) {
            val t = lines[i].trim()
            if (t.isEmpty()) break
            if (t.startsWith("```")) break
            if (isHorizontalRule(t)) break
            if (parseHeading(t) != null) break
            if (t.startsWith(">")) break
            if (isUnorderedItem(t)) break
            if (parseOrderedItem(t) != null) break
            paraLines.add(t)
            i++
        }
        if (paraLines.isNotEmpty()) {
            blocks.add(MdBlock.Paragraph(paraLines.joinToString(" ")))
        }
    }
    return blocks
}

internal fun isHorizontalRule(t: String): Boolean {
    if (t.length < 3) return false
    val c = t[0]
    if (c != '-' && c != '*' && c != '_') return false
    return t.all { it == c }
}

internal fun parseHeading(t: String): MdBlock.Heading? {
    if (!t.startsWith("#")) return null
    var level = 0
    while (level < t.length && t[level] == '#') level++
    if (level < 1 || level > 6) return null
    // Require a space after the # run (otherwise it's literal text like "#hashtag").
    if (level >= t.length || t[level] != ' ') return null
    return MdBlock.Heading(level = level, text = t.substring(level + 1).trim())
}

internal fun isUnorderedItem(t: String): Boolean =
    (t.startsWith("- ") || t.startsWith("* ")) && t.length > 2

internal fun parseOrderedItem(t: String): MdBlock.ListItem? {
    var j = 0
    while (j < t.length && t[j].isDigit()) j++
    if (j == 0) return null // no leading digits
    if (j >= t.length) return null
    val sep = t[j]
    if (sep != '.' && sep != ')') return null
    if (j + 1 >= t.length || t[j + 1] != ' ') return null
    val num = t.substring(0, j)
    return MdBlock.ListItem(marker = "$num.", text = t.substring(j + 2).trimStart())
}

// ---------------------------------------------------------------------------------------------
// Block rendering
// ---------------------------------------------------------------------------------------------

@Composable
private fun RenderBlock(block: MdBlock, color: Color, linkColor: Color, codeFont: FontFamily) {
    when (block) {
        is MdBlock.Heading -> {
            val size = when (block.level) {
                1 -> 22.sp
                2 -> 20.sp
                3 -> 18.sp
                4 -> 16.sp
                5 -> 14.sp
                else -> 13.sp
            }
            Text(
                text = inlineAnnotated(block.text, color, linkColor, codeFont),
                color = color,
                fontSize = size,
                fontWeight = FontWeight.Bold,
            )
        }
        is MdBlock.Paragraph -> {
            Text(text = inlineAnnotated(block.text, color, linkColor, codeFont), color = color, fontSize = 13.sp)
        }
        is MdBlock.Code -> {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(BoBClawColors.GlassFill, RoundedCornerShape(8.dp))
                    .padding(10.dp),
            ) {
                Text(
                    text = block.code,
                    color = color,
                    fontSize = 12.sp,
                    fontFamily = codeFont,
                )
            }
        }
        is MdBlock.Quote -> {
            // Indented + dimmer. A left-padding indent (rather than a full-height bar) keeps the
            // layout robust across Compose versions — no IntrinsicSize needed.
            Row(modifier = Modifier.fillMaxWidth().padding(start = 12.dp)) {
                Text(
                    text = inlineAnnotated(block.text, BoBClawColors.TextSecondary, linkColor, codeFont),
                    color = BoBClawColors.TextSecondary,
                    fontSize = 13.sp,
                    fontStyle = FontStyle.Italic,
                )
            }
        }
        is MdBlock.ListItem -> {
            Row(modifier = Modifier.fillMaxWidth().padding(start = 4.dp)) {
                Text(
                    text = block.marker + " ",
                    color = color,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium,
                )
                Text(
                    text = inlineAnnotated(block.text, color, linkColor, codeFont),
                    color = color,
                    fontSize = 13.sp,
                    modifier = Modifier.weight(1f),
                )
            }
        }
        is MdBlock.Rule -> {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(1.dp)
                    .background(BoBClawColors.BorderSubtle),
            )
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Inline parser
// ---------------------------------------------------------------------------------------------

/** Subtle background for inline `code` spans. */
private val InlineCodeBg = Color(0x33000000)

/**
 * Parse inline markers into one AnnotatedString. Single left-to-right scan. Any marker that does
 * not find its closing partner is emitted literally — never throws on malformed or empty input.
 *
 * Supported: `**bold**`, `*italic*`, `_italic_`, `` `code` ``, `[label](url)`.
 * Links render `label` in [linkColor] (the URL itself is dropped — the desktop chat has no
 * in-place navigation for inline links).
 *
 * This is a NON-composable parser, so the accent-driven link color is THREADED IN as [linkColor]
 * rather than read from the (now composable-only) `BoBClawColors.AccentGreen` accessor — the
 * composable caller [MarkdownText] passes `LocalBoBClawColors.accent`.
 *
 * [baseColor] is accepted for symmetry/future use; spans set their own color where needed and
 * otherwise inherit the caller's Text color.
 */
internal fun inlineAnnotated(
    text: String,
    baseColor: Color,
    linkColor: Color,
    codeFont: FontFamily = FontFamily.Monospace,
): AnnotatedString = buildAnnotatedString {
    if (text.isEmpty()) return@buildAnnotatedString
    var i = 0
    val n = text.length
    while (i < n) {
        val c = text[i]
        when {
            // ---- inline code: `...` (no nesting; literal until the next backtick) ----
            c == '`' -> {
                val close = text.indexOf('`', startIndex = i + 1)
                if (close > i) {
                    withStyle(
                        SpanStyle(fontFamily = codeFont, background = InlineCodeBg)
                    ) {
                        append(text.substring(i + 1, close))
                    }
                    i = close + 1
                } else {
                    append(c); i++
                }
            }

            // ---- bold: **...** ----
            c == '*' && i + 1 < n && text[i + 1] == '*' -> {
                val close = text.indexOf("**", startIndex = i + 2)
                if (close > i + 1) {
                    withStyle(SpanStyle(fontWeight = FontWeight.Bold)) {
                        append(text.substring(i + 2, close))
                    }
                    i = close + 2
                } else {
                    append("**"); i += 2
                }
            }

            // ---- italic: *...* ----
            c == '*' -> {
                val close = text.indexOf('*', startIndex = i + 1)
                if (close > i) {
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) {
                        append(text.substring(i + 1, close))
                    }
                    i = close + 1
                } else {
                    append(c); i++
                }
            }

            // ---- italic: _..._ ----
            c == '_' -> {
                val close = text.indexOf('_', startIndex = i + 1)
                if (close > i) {
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) {
                        append(text.substring(i + 1, close))
                    }
                    i = close + 1
                } else {
                    append(c); i++
                }
            }

            // ---- link: [label](url) → render label in the accent link color ----
            c == '[' -> {
                val labelEnd = text.indexOf(']', startIndex = i + 1)
                if (labelEnd > i && labelEnd + 1 < n && text[labelEnd + 1] == '(') {
                    val urlEnd = text.indexOf(')', startIndex = labelEnd + 2)
                    if (urlEnd > labelEnd + 1) {
                        val label = text.substring(i + 1, labelEnd)
                        withStyle(SpanStyle(color = linkColor)) {
                            append(label)
                        }
                        i = urlEnd + 1
                    } else {
                        append(c); i++
                    }
                } else {
                    append(c); i++
                }
            }

            else -> {
                append(c); i++
            }
        }
    }
}
