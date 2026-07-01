from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.memory.parser import Chunk, ParsedDocument, parse_markdown

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parser"


class TestSampleWithFrontmatter:
    def test_frontmatter_parsed(self):
        doc = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        assert doc.frontmatter["title"] == "Sample Document"
        assert doc.frontmatter["author"] == "BoBClaw"
        assert "tags" in doc.frontmatter

    def test_three_chunks(self):
        doc = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        assert len(doc.chunks) == 3

    def test_heading_paths(self):
        doc = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        assert doc.chunks[0].heading_path == ["First Section"]
        assert doc.chunks[1].heading_path == ["Second Section"]
        assert doc.chunks[2].heading_path == ["Third Section"]

    def test_chunk_hash_determinism(self):
        doc1 = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        doc2 = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        for c1, c2 in zip(doc1.chunks, doc2.chunks):
            assert c1.chunk_hash == c2.chunk_hash

    def test_chunk_hash_shape(self):
        doc = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        for c in doc.chunks:
            assert isinstance(c, Chunk)
            assert len(c.chunk_hash) == 64
            assert all(ch in "0123456789abcdef" for ch in c.chunk_hash)

    def test_chunk_text_nonempty(self):
        doc = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        for c in doc.chunks:
            assert len(c.text.strip()) > 0


class TestNoFrontmatter:
    def test_empty_frontmatter(self):
        doc = parse_markdown(FIXTURES / "no_frontmatter.md")
        assert doc.frontmatter == {}

    def test_chunks_produced(self):
        doc = parse_markdown(FIXTURES / "no_frontmatter.md")
        assert len(doc.chunks) >= 1


class TestLongSection:
    def test_multiple_chunks(self):
        doc = parse_markdown(FIXTURES / "long_section.md", max_tokens=500)
        assert len(doc.chunks) >= 3

    def test_overlap_present(self):
        doc = parse_markdown(FIXTURES / "long_section.md", max_tokens=500, overlap_tokens=50)
        assert len(doc.chunks) >= 2
        for i in range(1, len(doc.chunks)):
            prior_end = doc.chunks[i - 1].text.split("\n\n")[-1]
            current_start = doc.chunks[i].text.split("\n\n")[0]
            assert prior_end in current_start or current_start in prior_end

    def test_all_chunks_have_heading(self):
        doc = parse_markdown(FIXTURES / "long_section.md")
        for c in doc.chunks:
            assert len(c.heading_path) > 0


class TestCodeFenceOversized:
    def test_single_chunk(self, caplog):
        caplog.set_level(logging.WARNING)
        doc = parse_markdown(FIXTURES / "code_fence_oversized.md", max_tokens=500)
        assert len(doc.chunks) == 1

    def test_warning_emitted(self, caplog):
        caplog.set_level(logging.WARNING)
        parse_markdown(FIXTURES / "code_fence_oversized.md", max_tokens=500)
        assert any("oversized code fence" in msg for msg in caplog.messages)

    def test_post_fence_content_present(self, caplog):
        caplog.set_level(logging.WARNING)
        doc = parse_markdown(FIXTURES / "code_fence_oversized.md", max_tokens=500)
        assert "This text comes after the large code block" in doc.chunks[0].text


class TestWikilinksAndTags:
    def test_wikilinks_extracted(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert "Foo" in doc.wikilinks
        assert "Bar" in doc.wikilinks

    def test_wikilinks_order_preserved(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert doc.wikilinks == ["Foo", "Bar"]

    def test_wikilinks_inside_code_block_ignored(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert "NotALink" not in doc.wikilinks

    def test_tags_extracted(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert "tag1" in doc.inline_tags
        assert "nested/tag" in doc.inline_tags

    def test_tags_inside_code_block_ignored(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert "nottag" not in doc.inline_tags

    def test_tags_order_preserved(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert doc.inline_tags == ["tag1", "nested/tag"]

    def test_dedup(self):
        doc = parse_markdown(FIXTURES / "wikilinks_and_tags.md")
        assert doc.wikilinks.count("Foo") == 1
        assert doc.inline_tags.count("tag1") == 1


class TestChunkHashShape:
    def test_sha256_hex(self):
        doc = parse_markdown(FIXTURES / "no_frontmatter.md")
        for c in doc.chunks:
            assert len(c.chunk_hash) == 64
            assert all(ch in "0123456789abcdef" for ch in c.chunk_hash)

    def test_different_content_different_hash(self):
        a = parse_markdown(FIXTURES / "no_frontmatter.md")
        b = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        for ca in a.chunks:
            for cb in b.chunks:
                if ca.text != cb.text:
                    assert ca.chunk_hash != cb.chunk_hash


class TestEdgeCases:
    def test_frontmatter_only(self):
        p = FIXTURES / "no_frontmatter.md"
        doc = parse_markdown(p, max_tokens=500)
        assert doc.frontmatter == {}
        assert len(doc.chunks) >= 1

    def test_empty_body_after_frontmatter_strip(self, tmp_path):
        tmp = tmp_path / "empty.md"
        tmp.write_text("---\nkey: val\n---\n   \n\n  ", encoding="utf-8")
        doc = parse_markdown(tmp)
        assert doc.frontmatter == {"key": "val"}
        assert doc.chunks == []
        assert doc.wikilinks == []
        assert doc.inline_tags == []

    def test_inline_tags_not_in_frontmatter(self):
        doc = parse_markdown(FIXTURES / "sample_with_frontmatter.md")
        assert "test" not in doc.inline_tags
        assert "sample" not in doc.inline_tags
