"""crews/need_gate.py: confidence gated routing of a live need answer (SRD
typesafe-integration-v1.0 section 2.5, C-NEED; TSI-NEED-01).

`uv run pytest tests/test_need_gate.py`
"""

from __future__ import annotations

import pytest

from crews.need_gate import gate_need, normalize_identity

MEASURED_BANDS = {
    "act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9, "wilson_lower": 0.86},
    "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36, "precision": 0.857, "wilson_lower": 0.72},
}

CONFIRM_INSUFFICIENT_BANDS = {
    "act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9, "wilson_lower": 0.86},
    "confirm": {"status": "INSUFFICIENT", "n_total": 5},
}

UNCALIBRATED_BANDS = {"status": "UNCALIBRATED"}

ACT_INSUFFICIENT_BANDS = {
    "act": {"status": "INSUFFICIENT", "n_total": 3},
    "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36, "precision": 0.857, "wilson_lower": 0.72},
}


def test_gate_need_uncalibrated_when_bands_carry_no_act_status() -> None:
    result = gate_need({"confidence": 0.95}, UNCALIBRATED_BANDS)
    assert result["decision"] == "uncalibrated"
    assert result["threshold_act"] is None
    assert result["threshold_confirm"] is None
    assert "reason" in result


def test_gate_need_uncalibrated_when_act_band_is_insufficient() -> None:
    result = gate_need({"confidence": 0.95}, ACT_INSUFFICIENT_BANDS)
    assert result["decision"] == "uncalibrated"
    assert result["threshold_act"] is None
    assert result["threshold_confirm"] == 0.70


def test_gate_need_admits_at_or_above_act_threshold() -> None:
    at_threshold = gate_need({"confidence": 0.85}, MEASURED_BANDS)
    above_threshold = gate_need({"confidence": 0.95}, MEASURED_BANDS)
    assert at_threshold["decision"] == "admit"
    assert above_threshold["decision"] == "admit"
    assert at_threshold["threshold_act"] == 0.85
    assert at_threshold["threshold_confirm"] == 0.70


def test_gate_need_confirms_below_act_but_at_or_above_confirm() -> None:
    result = gate_need({"confidence": 0.75}, MEASURED_BANDS)
    assert result["decision"] == "confirm"
    assert result["threshold_act"] == 0.85
    assert result["threshold_confirm"] == 0.70

    at_confirm_threshold = gate_need({"confidence": 0.70}, MEASURED_BANDS)
    assert at_confirm_threshold["decision"] == "confirm"


def test_gate_need_rejects_below_both_thresholds() -> None:
    result = gate_need({"confidence": 0.50}, MEASURED_BANDS)
    assert result["decision"] == "reject"
    assert result["threshold_act"] == 0.85
    assert result["threshold_confirm"] == 0.70


def test_gate_need_rejects_below_act_with_no_measured_confirm_band() -> None:
    result = gate_need({"confidence": 0.50}, CONFIRM_INSUFFICIENT_BANDS)
    assert result["decision"] == "reject"
    assert result["threshold_act"] == 0.85
    assert result["threshold_confirm"] is None


def test_gate_need_accepts_bare_confidence_number() -> None:
    result = gate_need(0.90, MEASURED_BANDS)
    assert result["decision"] == "admit"


def test_gate_need_raises_on_missing_confidence() -> None:
    with pytest.raises(ValueError):
        gate_need({"choice": "everyday_implementation"}, MEASURED_BANDS)


# --- crew task blank-identity-normalization-closer (2026-09-22, JEV-33): normalize_identity is
# the one rule closing the blank-identity class on both operands of both guards. Unit tested here
# directly, on values nobody has tested yet, per this crew task's own acceptance criterion A1. ----

def test_normalize_identity_treats_any_whitespace_only_string_as_no_identity() -> None:
    """A value is an identity only if it still carries content after `.strip()`. Three named
    values nobody has tested yet, each proved by probe: a lone carriage return, a vertical tab
    mixed with spaces, and a full width (non ASCII) space, which Python's `str.strip()` also
    treats as whitespace so it must normalize to None exactly like an ASCII blank does."""
    assert normalize_identity("\r") is None
    assert normalize_identity("  \x0b  ") is None
    assert normalize_identity("　") is None  # ideographic (full width) space


def test_normalize_identity_returns_the_real_string_unchanged_when_non_blank() -> None:
    """A string that carries real content, even with surrounding whitespace, is still an identity,
    and normalize_identity returns it exactly as given rather than stripping it: this rule closes
    the blank-identity class, it does not silently rewrite a real identity's value."""
    assert normalize_identity("jev-1.13.0") == "jev-1.13.0"
    assert normalize_identity("  jev-1.13.0  ") == "  jev-1.13.0  "


def test_normalize_identity_treats_any_non_string_or_missing_value_as_no_identity() -> None:
    """Every non string value normalizes to None too, the same as a blank string: this is a rule
    about what an identity IS (a string with real content), not a list of the blank shapes a prior
    repair happened to enumerate."""
    assert normalize_identity(None) is None
    assert normalize_identity(0) is None
    assert normalize_identity(False) is None
    assert normalize_identity([]) is None
    assert normalize_identity({}) is None
    assert normalize_identity(["jev-1.13.0"]) is None


def test_gate_need_criteria_hash_guard_applies_the_same_normalization_as_the_model_guard() -> None:
    """The same rule, both guards, both operands (crew task blank-identity-normalization-closer):
    a whitespace only band criteria hash checked by a whitespace only caller criteria hash must
    not admit, mirroring the model guard's own whitespace closure in tests/test_band_install.py."""
    band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                    "wilson_lower": 0.86},
            "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36, "precision": 0.857,
                       "wilson_lower": 0.72},
            "provenance": {"criteria_hash": "\t\n"}}
    result = gate_need({"confidence": 0.95}, band, criteria_hash="\t\n")
    assert result["decision"] == "uncalibrated"
    assert result["threshold_act"] is None and result["threshold_confirm"] is None


# --- crew task visible-identity-closer (2026-09-22, JEV-30/JEV-32/JEV-33): a whitespace only
# string is not the whole class. THE DEFEATING SET a round five blind hunt found admitting on both
# guards, identical on the band side and the caller side, none of them whitespace and so none of
# them touched by `.strip()`: U+200B ZERO WIDTH SPACE, U+200C ZERO WIDTH NON JOINER, U+200D ZERO
# WIDTH JOINER, U+FEFF BYTE ORDER MARK / ZERO WIDTH NO BREAK SPACE, U+2060 WORD JOINER, U+180E
# MONGOLIAN VOWEL SEPARATOR, U+00AD SOFT HYPHEN, and U+0000 NULL. -----------------------------------

_DEFEATING_SET = ("​", "‌", "‍", "﻿", "⁠", "᠎", "­", "\u0000")


def test_normalize_identity_refuses_every_character_in_the_defeating_set() -> None:
    """A1: every one of the eight round five defeating characters normalizes to None on its own,
    both alone and padded with real whitespace, proving the fix is a property of the character
    (its Unicode general category), not a string-shape special case."""
    for char in _DEFEATING_SET:
        assert normalize_identity(char) is None, repr(char)
        assert normalize_identity(char * 3) is None, repr(char)
        assert normalize_identity(f"  {char}  ") is None, repr(char)


def test_gate_need_model_guard_refuses_every_defeating_character_paired_identically() -> None:
    """A1: reproduced at the model guard itself, each defeating character identical on the band
    side and the caller side (the exact admitting pairing a round five blind hunt found: raw text
    equality of two matching invisible strings used to read as a real, matching model)."""
    for char in _DEFEATING_SET:
        band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                        "wilson_lower": 0.86},
                "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36,
                           "precision": 0.857, "wilson_lower": 0.72},
                "provenance": {"model": char}}
        result = gate_need({"confidence": 0.995}, band, model=char)
        assert result["decision"] == "uncalibrated", (repr(char), result)
        assert result["threshold_act"] is None and result["threshold_confirm"] is None


def test_gate_need_criteria_hash_guard_refuses_every_defeating_character_paired_identically() -> None:
    """A1: the same reproduction against the criteria hash guard, since it applies the identical
    `normalize_identity` rule to its own operands."""
    for char in _DEFEATING_SET:
        band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                        "wilson_lower": 0.86},
                "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36,
                           "precision": 0.857, "wilson_lower": 0.72},
                "provenance": {"criteria_hash": char}}
        result = gate_need({"confidence": 0.995}, band, criteria_hash=char)
        assert result["decision"] == "uncalibrated", (repr(char), result)
        assert result["threshold_act"] is None and result["threshold_confirm"] is None


def test_normalize_identity_general_property_five_untested_code_points() -> None:
    """A2: normalize_identity is stated as a property of the fixed 62 character ASCII alphanumeric
    allowlist (crew task positive-allowlist-closer, JEV-35), not as a list of the code points found
    admitting so far. Five code points nobody has probed here yet, each checked directly, prove the
    allowlist generalizes correctly to values it was never told about:
      - U+2001 EM SPACE (category Zs, no ASCII alphanumeric character, refused)
      - U+000B VERTICAL TAB (category Cc, no ASCII alphanumeric character, refused)
      - U+2028 LINE SEPARATOR (category Zl, no ASCII alphanumeric character, refused)
      - U+061C ARABIC LETTER MARK (category Cf, no ASCII alphanumeric character, refused)
      - U+00E9 LATIN SMALL LETTER E WITH ACUTE, alone (category Ll, an ordinary printable letter,
        but not one of the 62 ASCII characters, so it is refused too. This is the exact case a
        category based positive rule gets wrong the other way: a category test would call U+00E9 a
        visible letter and admit it, the same category reasoning that lets U+3164 HANGUL FILLER
        (also a Letter category, Lo) through as an "identity" while it renders as nothing. The
        ASCII allowlist refuses both consistently, because neither one carries one of the 62
        characters this rule actually checks for, whatever category either is filed under.
    """
    assert normalize_identity(" ") is None
    assert normalize_identity("\u000b") is None
    assert normalize_identity(" ") is None
    assert normalize_identity("؜") is None
    assert normalize_identity("é") is None


def test_normalize_identity_admits_non_ascii_letters_only_alongside_a_real_ascii_character() -> None:
    """A3: a realistic identity may legitimately carry a non ASCII letter, as long as it also
    carries at least one ASCII alphanumeric character somewhere in the same string (crew task
    positive-allowlist-closer, JEV-35): normalize_identity tests the whole string for membership,
    not the first character alone, so a Cyrillic word followed by "123" is a real identity (it
    contains "1", "2", and "3"), while the same Cyrillic word alone, with no ASCII alphanumeric
    character anywhere in it, is not. Both crews.judge.meta's `model` field and
    crews.cli._need_criteria_hash's sha256 hex digest are ASCII only in production, so this never
    blocks a real production identity; it only refuses the round six admitting set and any other
    string built entirely from non ASCII code points."""
    assert normalize_identity("тест123") == "тест123"
    assert normalize_identity("тест") is None


# --- crew task positive-allowlist-closer (2026-09-22, JEV-35): round six's admitting set. A blind
# hunter probed 38 values on both guards and found these 18 admitting, none of them Cc, Cf, Zs, Zl,
# or Zp, spanning seven other general categories (Mn, Lo, So, Co, Cn, Cs, Sk). All 18 were reachable
# at the production call site (crews.cli.core_budget) through crew install-band's provenance
# fields, not only through a direct call to gate_need, until install-band was closed to refuse every
# one of them by name. -----------------------------------------------------------------------

_ROUND_SIX_ADMITTING_SET = (
    "️",       # VARIATION SELECTOR-16 (Mn)
    "︀",       # VARIATION SELECTOR-1 (Mn)
    "\U000e0100",   # VARIATION SELECTOR-17 (Mn)
    "͏",       # COMBINING GRAPHEME JOINER (Mn)
    "́",       # COMBINING ACUTE ACCENT (Mn)
    "᠋",       # MONGOLIAN FREE VARIATION SELECTOR ONE (Mn)
    "឴",       # KHMER VOWEL INHERENT AQ (Mn)
    "ㅤ",       # HANGUL FILLER (Lo)
    "ᅟ",       # HANGUL CHOSEONG FILLER (Lo)
    "ᅠ",       # HANGUL JUNGSEONG FILLER (Lo)
    "⠀",       # BRAILLE PATTERN BLANK (So)
    "",       # PRIVATE USE (Co)
    "\U000f0000",   # PRIVATE USE, PLANE 15 (Co)
    "͸",       # unassigned (Cn)
    "\U00030000",   # CJK UNIFIED IDEOGRAPH-30000, assigned and rendering (Lo, not Cn; admits here
                    # only because the round six denylist never excluded Lo, not because it lacks a
                    # glyph)
    "\ud800",       # lone surrogate (Cs)
    "\U0001f3fb",   # EMOJI MODIFIER FITZPATRICK TYPE-1-2 (Sk)
    "️​",  # mixed: VS-16 followed by ZERO WIDTH SPACE
)


def test_normalize_identity_refuses_the_full_round_six_admitting_set() -> None:
    """A1: every one of the 18 round six admitting values normalizes to None on its own, proving
    the positive ASCII allowlist closes all of them at once, across seven different Unicode general
    categories, with no category specific handling anywhere in the rule."""
    for value in _ROUND_SIX_ADMITTING_SET:
        assert normalize_identity(value) is None, repr(value)
        assert normalize_identity(value * 3) is None, repr(value)


def test_gate_need_model_guard_refuses_the_full_round_six_admitting_set_paired_identically() -> None:
    """A1: reproduced at the model guard itself, each round six value identical on the band side
    and the caller side, the exact admitting pairing the blind hunt found before this repair."""
    for value in _ROUND_SIX_ADMITTING_SET:
        band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                        "wilson_lower": 0.86},
                "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36,
                           "precision": 0.857, "wilson_lower": 0.72},
                "provenance": {"model": value}}
        result = gate_need({"confidence": 0.995}, band, model=value)
        assert result["decision"] == "uncalibrated", (repr(value), result)
        assert result["threshold_act"] is None and result["threshold_confirm"] is None


def test_gate_need_criteria_hash_guard_refuses_the_full_round_six_admitting_set_paired_identically() -> None:
    """A1: the same reproduction against the criteria hash guard, since it applies the identical
    normalize_identity rule to its own operands."""
    for value in _ROUND_SIX_ADMITTING_SET:
        band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                        "wilson_lower": 0.86},
                "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36,
                           "precision": 0.857, "wilson_lower": 0.72},
                "provenance": {"criteria_hash": value}}
        result = gate_need({"confidence": 0.995}, band, criteria_hash=value)
        assert result["decision"] == "uncalibrated", (repr(value), result)
        assert result["threshold_act"] is None and result["threshold_confirm"] is None


def test_normalize_identity_realistic_identities_still_admit() -> None:
    """A3: every realistic identity this repository actually produces still carries a real ASCII
    alphanumeric character and survives normalize_identity unchanged: a model name, a 64 character
    sha256 hex digest, and a semantic version."""
    import hashlib
    assert normalize_identity("jev-1.13.0") == "jev-1.13.0"
    digest = hashlib.sha256(b"catalog-need-criteria").hexdigest()
    assert normalize_identity(digest) == digest
    assert normalize_identity("2.6.0") == "2.6.0"


# --- crew task docstring-truth-and-control-char-closer (2026-09-22, JEV-37/JEV-39): the residual a
# blind hunter found under what would defeat the allowlist rule. Every character in each of these
# three values is ASCII, and each value carries a real ASCII alphanumeric character (an "8" or a
# "0"), so each satisfied normalize_identity's allowlist half exactly as written and admitted before
# this closure; a terminal renders each of them as nothing (an SGR conceal/reset escape, a carriage
# return overwrite, a backspace erase). ------------------------------------------------------------

_CONTROL_CHARACTER_ADMITTING_SET = (
    "\x1b[8m\x1b[0m",   # SGR conceal (8) then reset (0): renders as nothing
    "\r0\r",            # "0" bracketed by carriage returns that overwrite it at column zero
    "0\x08 \x08",       # "0" erased by a backspace, a blank, and a second backspace
)


def test_normalize_identity_admitted_the_control_character_set_before_this_closure() -> None:
    """A3: each of the three values carries a real ASCII alphanumeric character, so it admitted
    under the allowlist alone (proving the residual existed) before the control character closure
    below is exercised. This reproduces the pre closure behavior directly against the character
    membership rule, independent of normalize_identity, so it stays true regardless of future edits
    to this module."""
    for value in _CONTROL_CHARACTER_ADMITTING_SET:
        assert any(char.isalnum() and ord(char) < 128 for char in value), repr(value)


def test_normalize_identity_refuses_the_control_character_admitting_set() -> None:
    """A3: every one of the three round seven admitting values now normalizes to None, both alone
    and padded with a real identity character, proving the ASCII control closure is a property of
    the string's content (does it carry one of the 33 ASCII control code points), not a list of
    these three specific escape shapes."""
    for value in _CONTROL_CHARACTER_ADMITTING_SET:
        assert normalize_identity(value) is None, repr(value)
        assert normalize_identity(f"real-id-{value}") is None, repr(value)


def test_gate_need_model_guard_refuses_the_control_character_admitting_set_paired_identically() -> None:
    """A3: reproduced at the model guard itself, each control character value identical on the band
    side and the caller side, proving the closure holds at the production call site, not only
    inside normalize_identity."""
    for value in _CONTROL_CHARACTER_ADMITTING_SET:
        band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                        "wilson_lower": 0.86},
                "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36,
                           "precision": 0.857, "wilson_lower": 0.72},
                "provenance": {"model": value}}
        result = gate_need({"confidence": 0.995}, band, model=value)
        assert result["decision"] == "uncalibrated", (repr(value), result)
        assert result["threshold_act"] is None and result["threshold_confirm"] is None


def test_gate_need_criteria_hash_guard_refuses_the_control_character_admitting_set_paired_identically() -> None:
    """A3: the same reproduction against the criteria hash guard, since it applies the identical
    normalize_identity rule to its own operands."""
    for value in _CONTROL_CHARACTER_ADMITTING_SET:
        band = {"act": {"status": "MEASURED", "threshold": 0.85, "n": 40, "k": 36, "precision": 0.9,
                        "wilson_lower": 0.86},
                "confirm": {"status": "MEASURED", "threshold": 0.70, "n": 42, "k": 36,
                           "precision": 0.857, "wilson_lower": 0.72},
                "provenance": {"criteria_hash": value}}
        result = gate_need({"confidence": 0.995}, band, criteria_hash=value)
        assert result["decision"] == "uncalibrated", (repr(value), result)
        assert result["threshold_act"] is None and result["threshold_confirm"] is None


def test_normalize_identity_space_padding_survives_but_tab_padding_is_refused() -> None:
    """A8 (skill-routing-legibility-and-default-repairer, 2026-09-22): the module docstring's
    whitespace claim now distinguishes ordinary whitespace (a space, `U+0020`, not an ASCII control
    character) from a tab (`U+0009`, one of the 33 ASCII control code points). A space padded identity
    survives unchanged, exactly as the docstring claims; a tab padded identity is refused outright by
    the control character half and returns None, never the padded string."""
    assert normalize_identity("  jev-1  ") == "  jev-1  "
    assert normalize_identity("\tjev-1\t") is None


def test_normalize_identity_legitimate_identity_with_no_control_characters_is_unaffected() -> None:
    """A5: every realistic identity named in the crew task input, none of which contains an ASCII
    control character, survives normalize_identity unchanged after the control character closure:
    an Amazon Bedrock style model ARN, a HuggingFace repo id, a fine tune id with colons, an accented
    cafe name, and a 64 character sha256 digest."""
    import hashlib
    assert normalize_identity("anthropic.claude-3-5-sonnet-20240620-v1:0") == (
        "anthropic.claude-3-5-sonnet-20240620-v1:0"
    )
    assert normalize_identity("meta-llama/Llama-3.1-70B-Instruct") == "meta-llama/Llama-3.1-70B-Instruct"
    assert normalize_identity("ft:gpt-4o:acme::9xYz1") == "ft:gpt-4o:acme::9xYz1"
    assert normalize_identity("café-v2") == "café-v2"
    digest = hashlib.sha256(b"control-character-closure").hexdigest()
    assert normalize_identity(digest) == digest
