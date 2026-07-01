## Long Section

This is the first paragraph of a very long section. It introduces the topic with some meaningful content that will be used to test chunking behavior when the section exceeds the maximum token limit. The purpose of this document is to provide enough text to force the parser into producing multiple chunks using the greedy paragraph-packing algorithm.

The second paragraph continues the discussion with additional details and context. We need enough text here to ensure the total section length is well over the max_tokens threshold to trigger multi-chunk behavior. Each paragraph adds another layer of information, building up the overall word count required for the test.

The third paragraph adds more information about the subject matter. This is getting longer and should contribute to the overall token count. We are building up a substantial amount of text that will be used to verify the chunking logic works correctly with realistic content.

Now we are on the fourth paragraph. Each paragraph is deliberately kept compact so the tokenizer sees them as separate units. This lets the chunking algorithm split on paragraph boundaries rather than mid-paragraph, which is the required behavior for the overlap and splitting logic.

The fifth paragraph continues building the document length. We are aiming for a total token count well above 500 to guarantee we get at least three chunks when the default max_tokens of 500 is used. The overlapping test is important to verify that context carries across chunk boundaries.

Sixth paragraph here. Still marching toward the token goal. The overlap test requires that the last overlap_tokens of each chunk appear at the start of the next chunk, rounded to paragraph boundaries. This ensures that context is not lost between adjacent chunks during retrieval.

Seventh paragraph. Getting closer to the target now. This section needs to be long enough that the tokenizer produces multiple chunks when using the default parameters. The test suite validates both the splitting and the overlap behavior comprehensively.

Eighth paragraph. The text is intentionally repetitive to keep it simple while building up token count. The important thing is reaching the threshold and maintaining enough content to exercise all the edge cases in the parser implementation.

Ninth paragraph. Almost there. A few more paragraphs should put us well over 500 tokens and into multi-chunk territory. The parser should split this section into at least three distinct chunks for the tests to pass.

Tenth paragraph. This should be enough text to push us past the threshold. We are now comfortably in multi-chunk territory with room to spare. The tests will verify chunk counts, heading paths, and overlap behavior across all produced chunks.

Eleventh paragraph provides some extra headroom. The test will verify that chunks are split on paragraph boundaries and that overlap works correctly. Each chunk should have the correct heading path derived from the document structure.

Twelfth paragraph. With this many paragraphs the section should produce at least three chunks. Each chunk after the first should include the overlapping paragraphs from the end of the prior chunk. The overlap is paragraph-rounded to avoid mid-paragraph cuts.

Thirteenth paragraph. This paragraph continues adding content. The token count should now be well above the threshold. The parser must correctly group paragraphs greedily up to the max_tokens limit before starting a new chunk.

Fourteenth paragraph. Still adding content to ensure sufficient length. The test will check that chunk hashes are deterministic across multiple parse calls with the same input. This is a critical invariant for the hashing contract.

Fifteenth paragraph. More text to push the token count higher. The parser should handle long sections gracefully by splitting them into manageable chunks that preserve paragraph integrity.

Sixteenth paragraph. This is the sixteenth paragraph. Each paragraph is relatively short so the greedy packing algorithm has to combine multiple paragraphs per chunk. This exercises the paragraph aggregation logic in the chunker.

Seventeenth paragraph. Continuing the document to ensure robust test coverage. The test fixture needs to be long enough that even with overlap_tokens reducing the effective new content per chunk, we still get multiple chunks.

Eighteenth paragraph. Almost at the target length now. The overlap test requires at least 50 tokens of overlap between adjacent chunks. With multiple chunks, each overlap region should contain complete paragraphs from the end of the prior chunk.

Nineteenth paragraph. Just a few more paragraphs to go. The fixture needs to be self-contained with enough content to test all the chunking behaviors described in the test module.

Twentieth paragraph. This is the twentieth paragraph. We have now accumulated a significant amount of text that should trigger multi-chunk behavior consistently. The test framework will validate all aspects of the chunking output.

Twenty-first paragraph. Still writing content to ensure we cross the threshold. The parser should see this long section and decide to split it rather than emit a single oversized chunk.

Twenty-second paragraph. More content for the test fixture. The total document length is now substantial enough that chunking is guaranteed. Three or more chunks should be produced.

Twenty-third paragraph. This paragraph adds further length. The goal is to have at least 700 tokens of content so that even with overlapping reducing the effective size of later chunks, we still get the required number.

Twenty-fourth paragraph. The chunking algorithm should handle this gracefully by grouping paragraphs up to max_tokens and then starting fresh with the next group. Overlap ensures context continuity.

Twenty-fifth paragraph. This is the final paragraph of this testing fixture. It concludes the document and ensures the total token count is well above the 500 token threshold required for the multi-chunk tests to pass correctly.
