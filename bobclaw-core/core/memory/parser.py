from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import frontmatter
import tiktoken
from markdown_it import MarkdownIt

log = logging.getLogger(__name__)

# allowlisted-model-name: cl100k_base — OpenAI tokenizer for GPT-3.5/4-class
# models; close enough to most local embedding tokenizers for our token-count
# purposes.
_ENCODING = "cl100k_base"
_TOKENIZER = tiktoken.get_encoding(_ENCODING)

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|]+)(\|[^\[\]]*)?\]\]")
_TAG_RE = re.compile(r"(?<![A-Za-z0-9_/])#([A-Za-z][A-Za-z0-9_/-]+)")


def _count_tokens(text: str) -> int:
    return len(_TOKENIZER.encode(text))


@dataclass(frozen=True)
class Chunk:
    heading_path: list[str]
    text: str
    chunk_hash: str
    source_fact_id: Optional[str] = None


@dataclass(frozen=True)
class ParsedDocument:
    frontmatter: dict
    chunks: list[Chunk]
    wikilinks: list[str]
    inline_tags: list[str]


def parse_markdown(
    path: Path,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
) -> ParsedDocument:
    raw = path.read_text(encoding="utf-8")

    post = frontmatter.loads(raw)
    body = post.content
    fm = dict(post.metadata) if post.metadata else {}

    stripped_body = body.strip()
    if not stripped_body:
        return ParsedDocument(frontmatter=fm, chunks=[], wikilinks=[], inline_tags=[])

    md = MarkdownIt()
    tokens = md.parse(stripped_body)

    sections = _build_sections(tokens, stripped_body)
    chunks = []
    for heading_path, section_text in sections:
        if not section_text.strip():
            continue
        for c in _chunk_section(section_text, heading_path, max_tokens, overlap_tokens, path):
            chunks.append(c)

    wikilinks = _extract_wikilinks(tokens)
    inline_tags = _extract_tags(tokens)

    return ParsedDocument(
        frontmatter=fm,
        chunks=chunks,
        wikilinks=wikilinks,
        inline_tags=inline_tags,
    )


def _build_sections(tokens, body: str) -> list[tuple[list[str], str]]:
    body_lines = body.split("\n")
    sections: list[tuple[list[str], int, int]] = []
    heading_hierarchy: list[tuple[int, str]] = []
    last_start = 0
    seen_headings = False

    for tok in tokens:
        if tok.type == "heading_open":
            seen_headings = True
            level = int(tok.tag[1])
            heading_text = _resolve_heading_text(tokens, tok)
            start_line, end_line = tok.map if tok.map else (0, 0)

            if start_line > last_start:
                sections.append((list(h[1] for h in heading_hierarchy), last_start, start_line))

            while heading_hierarchy and heading_hierarchy[-1][0] >= level:
                heading_hierarchy.pop()
            heading_hierarchy.append((level, heading_text))

            next_line = end_line if tok.map else start_line + 1
            last_start = next_line

    if last_start < len(body_lines) or not seen_headings:
        sections.append((
            list(h[1] for h in heading_hierarchy),
            last_start,
            len(body_lines),
        ))

    result = []
    for heading_path, start_line, end_line in sections:
        text = "\n".join(body_lines[start_line:end_line])
        if text.strip():
            result.append((heading_path, text))

    return result


def _resolve_heading_text(tokens, heading_open_tok) -> str:
    idx = tokens.index(heading_open_tok)
    for i in range(idx + 1, min(idx + 5, len(tokens))):
        if tokens[i].type == "inline":
            return tokens[i].content.strip()
        if tokens[i].type == "heading_close":
            continue
        break
    return f"h{heading_open_tok.tag[1]}"


def _chunk_section(
    text: str,
    heading_path: list[str],
    max_tokens: int,
    overlap_tokens: int,
    path: Path,
) -> list[Chunk]:
    token_count = _count_tokens(text)
    if token_count <= max_tokens:
        return [Chunk(
            heading_path=list(heading_path),
            text=text,
            chunk_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )]

    fences = _find_fence_spans(text)
    if fences:
        fence_texts = [text[s:e] for s, e in fences]
        fence_cut = _count_tokens("\n\n".join(fence_texts))
        if fence_cut > max_tokens:
            log.warning("oversized code fence in %s", path)
            return [Chunk(
                heading_path=list(heading_path),
                text=text,
                chunk_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )]

    paragraphs = re.split(r"\n\n+", text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: list[Chunk] = []
    current_paras: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = _count_tokens(para) + 1

        if current_len + para_len <= max_tokens:
            current_paras.append(para)
            current_len += para_len
        else:
            if current_paras:
                chunk_text = "\n\n".join(current_paras)
                chunks.append(Chunk(
                    heading_path=list(heading_path),
                    text=chunk_text,
                    chunk_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                ))

            current_paras = [para]
            current_len = para_len

    if current_paras:
        chunk_text = "\n\n".join(current_paras)
        chunks.append(Chunk(
            heading_path=list(heading_path),
            text=chunk_text,
            chunk_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        ))

    if overlap_tokens > 0 and len(chunks) > 1:
        overlapped: list[Chunk] = [chunks[0]]
        for i in range(1, len(chunks)):
            prior = chunks[i - 1]
            overlap_paras = _overlap_paragraphs(prior.text, overlap_tokens)
            new_text = "\n\n".join(overlap_paras + [chunks[i].text]) if overlap_paras else chunks[i].text
            overlapped.append(Chunk(
                heading_path=list(heading_path),
                text=new_text,
                chunk_hash=hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
            ))
        chunks = overlapped

    return chunks


def _find_fence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```"):
            fence_start = i
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                i += 1
            if i < len(lines):
                fence_end = i + 1
                start_char = sum(len(line) + 1 for line in lines[:fence_start])
                end_char = sum(len(line) + 1 for line in lines[:fence_end])
                spans.append((start_char, end_char - 1))
                i += 1
            else:
                i += 1
        else:
            i += 1
    return spans


def _overlap_paragraphs(text: str, overlap_tokens: int) -> list[str]:
    paragraphs = re.split(r"\n\n+", text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    result: list[str] = []
    token_count = 0
    for para in reversed(paragraphs):
        para_len = _count_tokens(para) + 1
        if token_count + para_len > overlap_tokens and result:
            break
        result.insert(0, para)
        token_count += para_len
    return result


def _is_code_token(tok) -> bool:
    return tok.type in ("fence", "code_block")


def _extract_wikilinks(tokens) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        if _is_code_token(tok):
            continue
        if tok.type != "inline" or not tok.content:
            continue
        for m in _WIKILINK_RE.finditer(tok.content):
            target = m.group(1).strip()
            if target and target not in seen:
                seen.add(target)
                result.append(target)
    return result


def _extract_tags(tokens) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        if _is_code_token(tok):
            continue
        if tok.type != "inline" or not tok.content:
            continue
        for m in _TAG_RE.finditer(tok.content):
            tag = m.group(1).strip()
            if tag and tag not in seen:
                seen.add(tag)
                result.append(tag)
    return result
