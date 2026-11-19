"""Small automatic recall splice; full evidence stays available on demand."""

def prior_context(lines):
    from core.config import config
    content = 'Prior context:\n' + '\n'.join('- ' + str(text) for text in lines[:5])
    limit = config.MEMORY_CONTEXT_MAX_CHARS
    if len(content) <= limit:
        return content
    marker = '\n[Recall shortened; use memory_recall or conversation tools for more.]'
    return content[:limit - len(marker)] + marker
