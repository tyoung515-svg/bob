package com.bobclaw.ui

/**
 * Pull the last fenced ```html / ```svg block out of an assistant reply, for rendering in the
 * canvas pane. Returns null if there's no renderable web artifact. (svg is wrapped so the browser
 * renders it as a page.)
 */
fun extractHtmlArtifact(text: String): String? {
    val match = Regex("```(html|svg)\\s*\\n(.*?)```", RegexOption.DOT_MATCHES_ALL)
        .findAll(text)
        .lastOrNull() ?: return null
    val lang = match.groupValues[1]
    val body = match.groupValues[2].trim()
    if (body.isEmpty()) return null
    return if (lang == "svg" && !body.contains("<html", ignoreCase = true)) {
        "<!doctype html><html><body style=\"margin:0\">$body</body></html>"
    } else {
        body
    }
}

/**
 * Pull a written file path out of a reply (the claude_code planner writes artifacts to its scratch
 * dir and says e.g. "Wrote `C:\Temp\bobclaw\cc\<conv>\hello.html`"). Returns the absolute Windows
 * path to a renderable file (.html/.htm/.svg), or null. The canvas loads it via a file:// URL.
 */
fun extractFileArtifact(text: String): String? =
    Regex("""[A-Za-z]:\\[^\s`'"<>|]+\.(?:html?|svg)""", RegexOption.IGNORE_CASE)
        .findAll(text)
        .lastOrNull()
        ?.value

