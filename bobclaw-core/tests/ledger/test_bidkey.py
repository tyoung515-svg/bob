import hashlib
import pytest
from core.ledger.bidkey import normalize_subject, predicate_lemma, round_numeric, bid_key
from core.ledger.types import BID_NUMERIC_NDIGITS


# ---------------------------------------------------------------------------
# normalize_subject
# ---------------------------------------------------------------------------

def test_normalize_subject_whitespace_collapse_and_strip():
    """Leading/trailing whitespace stripped; internal whitespace collapsed."""
    assert normalize_subject("  hello   world  ") == "hello world"


def test_normalize_subject_casefold():
    """Case is folded (case-insensitive identity)."""
    assert normalize_subject("Hello World") == normalize_subject("HELLO WORLD")


def test_normalize_subject_nfkc():
    """NFKC normalisation composes/decomposes as required."""
    # 'ﬁ' (ﬁ) decomposes to fi under NFKC
    assert normalize_subject("ﬁne") == "fine"


def test_normalize_subject_leading_article_the():
    """Leading 'the ' (casefold-insensitive) is removed."""
    assert normalize_subject("The quick fox") == "quick fox"
    assert normalize_subject("the   cat") == "cat"


def test_normalize_subject_leading_article_a():
    """Leading 'a ' removed."""
    assert normalize_subject("A book") == "book"
    assert normalize_subject("a  apple") == "apple"


def test_normalize_subject_leading_article_an():
    """Leading 'an ' removed."""
    assert normalize_subject("An hour") == "hour"
    assert normalize_subject("an   egg") == "egg"


def test_normalize_subject_leading_article_not_alone():
    """Article removal does not strip the word if it's the entire string."""
    # If subject is "the", removing "the " leaves empty, but spec not explicit.
    # Assuming it removes only when followed by space; "the" alone stays.
    assert normalize_subject("the") == "the"
    assert normalize_subject("a") == "a"
    assert normalize_subject("an") == "an"


def test_normalize_subject_surrounding_punctuation_removed():
    """Surrounding punctuation (non-alphanumeric except '-'?_?) is stripped."""
    # Using common punctuation: . , ! ? : ; " ' ( ) [ ] { } etc.
    assert normalize_subject("  \"hello\"!") == "hello"
    assert normalize_subject("(world)") == "world"
    assert normalize_subject("...test...") == "test"


def test_normalize_subject_punctuation_not_removed_inside():
    """Internal punctuation is preserved (except whitespace collapse)."""
    assert normalize_subject("don't stop") == "don't stop"
    assert normalize_subject("well-known") == "well-known"


def test_normalize_subject_empty_string():
    """Empty string remains empty (or minimal after processing)."""
    assert normalize_subject("") == ""


def test_normalize_subject_only_punctuation_and_whitespace():
    """String of punctuation/spaces becomes empty."""
    assert normalize_subject("  !!!  ") == ""


def test_normalize_subject_deterministic():
    """Calling twice on same input yields identical result."""
    a = normalize_subject("ThE   quick   fox!")
    b = normalize_subject("ThE   quick   fox!")
    assert a == b


# ---------------------------------------------------------------------------
# predicate_lemma
# ---------------------------------------------------------------------------

def test_predicate_lemma_casefold_collapse_strip():
    """Casefold, strip, collapse whitespace."""
    assert predicate_lemma("  SCORES   ") == predicate_lemma("scores")
    assert predicate_lemma("  high   jump  ") == "high jump"


def test_predicate_lemma_synonym_map_example():
    """Synonym map transforms known verb forms to lemma."""
    # The example map includes these transformations.
    assert predicate_lemma("scores") == "score"
    assert predicate_lemma("scored") == "score"
    assert predicate_lemma("scoring") == "score"
    assert predicate_lemma("achieves") == "achieve"
    assert predicate_lemma("achieved") == "achieve"
    assert predicate_lemma("achieving") == "achieve"


def test_predicate_lemma_synonym_case_insensitive():
    """Synonym lookup is case-insensitive after casefold."""
    assert predicate_lemma("SCORES") == predicate_lemma("scores")
    assert predicate_lemma("ScoRes") == "score"


def test_predicate_lemma_inflection_stripping_ing():
    """Strip trailing 'ing' if stem length >= 3."""
    # "running" -> after map nothing, strip "ing" -> "runn" (len 4) -> kept
    # Expected: "runn" (since lemma may not be natural, but spec says strip)
    # Also "ring" -> strip "ing" -> "r" (len 1) -> not stripped because guard.
    assert predicate_lemma("running") == "runn"
    assert predicate_lemma("ring") == "ring"          # guard: stem too short


def test_predicate_lemma_inflection_stripping_ed():
    """Strip trailing 'ed' with guard."""
    assert predicate_lemma("jumped") == "jump"        # stem "jump" len 4
    assert predicate_lemma("bed") == "bed"            # "b" too short


def test_predicate_lemma_inflection_stripping_es():
    """Strip trailing 'es' with guard."""
    assert predicate_lemma("watches") == "watch"      # stem "watch" len 5
    assert predicate_lemma("yes") == "yes"            # "y" too short


def test_predicate_lemma_inflection_stripping_s():
    """Strip trailing 's' with guard."""
    assert predicate_lemma("dogs") == "dog"
    assert predicate_lemma("gas") == "gas"            # "ga" too short (stem len 2)


def test_predicate_lemma_mixed_transform():
    """Synonym map applied before inflection stripping."""
    # "scored" maps to "score", then no further stripping (score ends with e, not ing/ed/es/s)
    assert predicate_lemma("scored") == "score"


def test_predicate_lemma_empty_string():
    """Empty predicate becomes empty."""
    assert predicate_lemma("") == ""


def test_predicate_lemma_only_whitespace():
    """Whitespace-only becomes empty."""
    assert predicate_lemma("   ") == ""


def test_predicate_lemma_deterministic():
    """Deterministic across calls."""
    assert predicate_lemma("  Runs ") == predicate_lemma("  Runs ")


# ---------------------------------------------------------------------------
# round_numeric
# ---------------------------------------------------------------------------

def test_round_numeric_float():
    """Float input rounded to significant figures, minimal string."""
    # 80.40 -> 4 sig figs -> 80.4
    assert round_numeric(80.40, 4) == "80.4"
    # 77.80 -> 77.8
    assert round_numeric(77.80, 4) == "77.8"
    # 100.0 -> 100
    assert round_numeric(100.0, 4) == "100"
    # 123.4567 -> 4 sig figs -> 123.5
    assert round_numeric(123.4567, 4) == "123.5"


def test_round_numeric_string():
    """String with number, possibly trailing chars and whitespace."""
    assert round_numeric("80.40", 4) == "80.4"
    assert round_numeric("  77.8  ", 4) == "77.8"
    assert round_numeric("80.4%", 4) == "80.4"
    assert round_numeric("%80.4", 4) == "80.4"   # leading non-digit


def test_round_numeric_int():
    """Integer treated as float."""
    assert round_numeric(100, 4) == "100"
    assert round_numeric(9999, 4) == "9999"
    assert round_numeric(12345, 4) == "12350"    # rounded to 4 sig figs (ROUND_HALF_UP)


def test_round_numeric_none():
    """None input returns None."""
    assert round_numeric(None, 4) is None


def test_round_numeric_non_parseable():
    """Non-numeric string returns None."""
    assert round_numeric("abc", 4) is None
    assert round_numeric("", 4) is None
    assert round_numeric("   ", 4) is None


def test_round_numeric_negative():
    """Negative numbers produce negative strings."""
    assert round_numeric(-80.40, 4) == "-80.4"


def test_round_numeric_default_ndigits():
    """Use default BID_NUMERIC_NDIGITS (4) when ndigits not given."""
    assert round_numeric(80.40) == "80.4"


def test_round_numeric_edge_zeros():
    """Edge: 0.0, 0.000 etc."""
    assert round_numeric(0.0, 4) == "0"
    assert round_numeric("0.000", 4) == "0"
    assert round_numeric("0.004567", 4) == "0.004567"  # 4 sig figs


# ---------------------------------------------------------------------------
# bid_key
# ---------------------------------------------------------------------------

def test_bid_key_case_whitespace_insensitive():
    """Variants of same claim produce identical hex digest."""
    k1 = bid_key("The cat", "SCORES", "80.40")
    k2 = bid_key("the   cat!", "scores", "80.4")
    assert k1 == k2


def test_bid_key_different_subject_different():
    """Different subject yields different key."""
    k1 = bid_key("cat", "score", "100")
    k2 = bid_key("dog", "score", "100")
    assert k1 != k2


def test_bid_key_different_predicate_different():
    """Different predicate yields different key."""
    k1 = bid_key("cat", "score", "100")
    k2 = bid_key("cat", "jump", "100")
    assert k1 != k2


def test_bid_key_different_numeric_different():
    """Different numeric values yield different keys."""
    k1 = bid_key("cat", "score", 100)
    k2 = bid_key("cat", "score", 200)
    assert k1 != k2


def test_bid_key_missing_numeric_vs_zero():
    """Missing numeric vs numeric zero produce different keys (empty vs '0')."""
    k1 = bid_key("cat", "score")
    k2 = bid_key("cat", "score", 0)
    assert k1 != k2


def test_bid_key_same_numeric_different_form_same():
    """80.40 and 80.4 produce same key after canonicalisation."""
    k1 = bid_key("subject", "predicate", "80.40")
    k2 = bid_key("subject", "predicate", "80.4")
    assert k1 == k2


def test_bid_key_None_numeric():
    """Explicit None numeric same as omitted."""
    k1 = bid_key("sub", "pre")
    k2 = bid_key("sub", "pre", None)
    assert k1 == k2


def test_bid_key_empty_subject():
    """Empty subject allowed (though unusual): deterministic, valid digest, distinct from non-empty."""
    k = bid_key("", "pre")
    assert len(k) == 64 and k == bid_key("", "pre")  # 64-hex, deterministic
    assert k != bid_key("x", "pre")


def test_bid_key_empty_predicate():
    """Empty predicate allowed: deterministic, distinct from non-empty predicate."""
    k = bid_key("sub", "")
    assert len(k) == 64 and k == bid_key("sub", "")
    assert k != bid_key("sub", "x")


def test_bid_key_empty_both():
    """Both empty: deterministic, distinct from either field being non-empty."""
    k = bid_key("", "")
    assert len(k) == 64 and k == bid_key("", "")
    assert k != bid_key("", "x") and k != bid_key("x", "")


def test_bid_key_invariant_normalize_subject():
    """bid_key calls normalize_subject, so case/ws/punctuation variants same."""
    k1 = bid_key("  The Cat!  ", "run", 10)
    k2 = bid_key("the cat", "run", 10)
    assert k1 == k2


def test_bid_key_invariant_predicate_lemma():
    """bid_key calls predicate_lemma, so synonyms and inflection become same."""
    k1 = bid_key("cat", "scores", 10)
    k2 = bid_key("cat", "score", 10)
    assert k1 == k2


def test_bid_key_sha256_hexdigest():
    """Output is a 64-character hex string (SHA-256)."""
    k = bid_key("test", "test", 1)
    assert isinstance(k, str)
    assert len(k) == 64
    int(k, 16)  # raises ValueError if not hex
