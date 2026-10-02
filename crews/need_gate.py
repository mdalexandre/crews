"""Confidence gated routing of a live need answer (SRD typesafe-integration-v1.0 section 2.5,
C-NEED; requirement TSI-NEED-01).

A live need drives routing only when a measured act band admits it. With no measured act band
the decision is uncalibrated, never a silent fall through to an unmeasured default (INV-12: no
PASS, and here no admission, from absence). Below the act threshold, a measured confirm band
gives a second chance the caller must still confirm; below both, or with no measured confirm
band either, the decision is reject.

A band measures precision at a confidence threshold FOR A GIVEN need criteria text (round three
protocol.md section 4: a band is only valid for the criteria version it was measured on). When
the caller passes `criteria_hash` (the current catalog's need criteria hash) and the band itself
carries a provenance criteria hash that differs, the decision is uncalibrated with a drift
reason, before any threshold is even read: a band that no longer describes the criteria in
force must never admit and must never fall back to a silent default. The same no-admission-from-
absence principle covers the band that carries no criteria hash at all: when the caller passes
`criteria_hash` and a real band record exists (an "act" entry) but records no provenance criteria
hash to check it against, the decision is uncalibrated with its own distinguishable reason,
never a silent admit on an unverifiable band.

A band is likewise only valid for the model that answered the observations it was measured on
(JEV-16): a routing decision made by one Jev model must never be admitted on a threshold measured
against a different model's confidence behavior. Unlike the criteria hash, which the caller
already knows before the call, the model that actually answers is known only from the live
response itself (crews.judge.meta), so the caller passes it as `model` at the same point it
passes `criteria_hash`: after the response has been parsed, at the same gate_need call site. The
model check now mirrors the criteria hash shape exactly (JEV-17, matching the precedent JEV-25
already set for criteria_hash): a recorded model that differs from the answering model is a
mismatch and is "uncalibrated" with its own reason, and a real, MEASURED act band that records no
model at all is equally "uncalibrated" with its own distinguishable reason, never a silent admit
on an unverifiable band. Absence is not permission for the model guard any more than it is for
the criteria hash guard; the two now read the same way for the same reason.

The model check, and the criteria hash check beside it, are closed as one class rather than as an
enumeration of the absent shapes found so far (crew task blank-identity-normalization-closer,
JEV-33, correcting the prior repair's `isinstance(model, str) and model != ""`, which enumerated
the empty string and so still read `"   "` and `"\t\n"` as real model identities: two blanks that
name nobody compared equal and admitted; widened again by crew task visible-identity-closer,
JEV-30/JEV-32, correcting JEV-33's own `value.strip() != ""`, which closed every WHITESPACE only
shape but left every ZERO WIDTH, other format, and control shape open, because none of those
characters are whitespace and `.strip()` never touches them: a round five blind hunt over both
guards found U+200B, U+200C, U+200D, U+FEFF, U+2060, U+180E, U+00AD, and U+0000 all admitting,
identical on both sides). Each side of both comparisons is normalized by the same function,
`normalize_identity`: a value is an identity only when it is a string that carries at least one
ASCII alphanumeric character, one of the 62 characters `A` through `Z`, `a` through `z`, or `0`
through `9` (crew task positive-allowlist-closer, JEV-35, replacing the prior denylist of five
Unicode categories with a positive, frozen allowlist that no future Unicode release can widen),
and that carries no ASCII control character, one of the fixed 33 code points `U+0000` through
`U+001F` or `U+007F` (crew task docstring-truth-and-control-char-closer, JEV-37/JEV-39, closing a
residual where a control character can hide or overwrite an alphanumeric one already present);
every other value, string or not,
normalizes to None, meaning "this side names no identity at all". A missing `provenance` key,
`provenance=None`, `provenance={}`, a non dict `provenance`, a missing `provenance.model` key,
`provenance.model=None`, `provenance.model` of any non string type, an empty string, a whitespace
only string, and a string built entirely from zero width or other format or control characters
all normalize to that same None on the band side (`_band_model`); `model=None`, any non string
`model` value, an empty string, a whitespace only string, and a zero width or other invisible only
string all normalize to that same None on the caller side. The criteria hash guard's own operands,
`_band_criteria_hash` and the caller's `criteria_hash` argument, are normalized the same way and
for the same reason: JEV-25 set the criteria hash guard as the shape the model guard should match,
and that guard carried this same hole, latent only because the one production caller
(`crews.cli._need_criteria_hash`) always computes a real 64 character digest.

The rule this module guarantees on both guards: the check proceeds past the gate only when both
sides normalize (via `normalize_identity`) to the same string that carries at least one ASCII
alphanumeric character and no ASCII control character, or the caller omitted the argument entirely
(its own separate compatibility shape, documented below). Every other pairing, including two
strings that would compare equal as raw text while carrying no ASCII alphanumeric character at all
(two matching blank strings, two matching runs of zero width or other format characters, or two
matching runs of a combining mark, a Hangul filler, a private use character, an unassigned code
point, a lone surrogate, or any other character outside the fixed 62 character set), and including
two strings that would compare equal as raw text while each carries an ASCII control character
alongside its alphanumeric one (an SGR escape, a carriage return, or a backspace hiding the same
digit the same way on both sides), is refused. This is stated as a property of `normalize_identity`
and of the equality test built on it, a predicate over "both normalize to a string that contains at
least one of `A` to `Z`, `a` to `z`, or `0` to `9`, contains none of the 33 ASCII control code
points `U+0000` through `U+001F` or `U+007F`, and normalizes to the same string", not as an
enumeration of which Unicode categories happen to render as nothing today: the allowlist
is a fixed 62 character set no future Unicode release can add a 63rd member to, where the prior
denylist of five categories (crew tasks blank-identity-normalization-closer and
visible-identity-closer) was a moving target every new Unicode release could defeat again, and did,
five repairs in a row before this one (JEV-35, crew task positive-allowlist-closer). A category
based positive rule is not the fix either: U+3164 HANGUL FILLER, U+115F HANGUL CHOSEONG FILLER, and
U+1160 HANGUL JUNGSEONG FILLER are all category Lo (letter, other) and render as nothing, so "at
least one letter or digit" still admits them; only an ASCII only allowlist closes the class,
because every code point outside the 128 ASCII slots is excluded regardless of what category a
future Unicode version files it under.

Round six (crew task positive-allowlist-closer, JEV-35) found the sixth generation of the same
failure: a blind hunter probed 38 values on both guards and found 17 that still admitted under the
five category denylist, none of them Cc, Cf, Zs, Zl, or Zp. U+FE0F, U+FE00, U+E0100, U+034F,
U+0301, U+180B, and U+17B4 are all category Mn (a combining mark that renders as an invisible
modifier on the character before it, never as a glyph of its own). U+3164, U+115F, and U+1160 are
category Lo (letter, other), yet the Hangul standard defines all three as fillers that render as
nothing. U+2800 is category So (symbol, other), the Braille pattern with no dots raised, a blank
cell. U+E000 and U+F0000 are category Co (private use, meaningless outside a private agreement).
U+0378 is category Cn (unassigned, reserved for a future Unicode version to define). U+30000 is
category Lo, `CJK UNIFIED IDEOGRAPH-30000`, assigned and rendering as an ordinary CJK glyph on
Unicode 16.0.0 (verified with `unicodedata.category` and `unicodedata.name` on the interpreter
`uv run` uses: `unicodedata.category(chr(0x30000))` returns `"Lo"`); it does not "render as
nothing" the way the rest of this list does, and it admitted under the five category denylist for
the same reason the Hangul fillers above did, because that denylist never excluded Lo at all, not
because it was unassigned. U+D800 is category Cs (a surrogate, a code unit that exists only inside
UTF-16 encoding and names no character of its own). U+1F3FB is category Sk (a modifier symbol, the
lone emoji skin tone modifier with no base emoji to modify). Every one of these was reachable at
the production call site (`crews.cli.core_budget`), not only through a direct call to `gate_need`,
because `install-band` used to accept them as provenance and the routing gate would then accept
them as an answering model or a criteria hash; `install-band` now refuses every one of them by
name (crew task blank-identity-normalization-closer follow up: 215 install probes across all five
required provenance fields accepted zero). No enumerable denylist closes this: the seventeen code
points above span seven different general categories (Mn, Lo, So, Co, Cn, Cs, Sk), because neither
"renders as nothing" nor "is unassigned" nor any other single enumerable Unicode property was ever
the actual boundary, and every future Unicode release can mint another code point outside whatever
property had been checked for. The fix in this module is the only kind of rule a future code point
cannot defeat: instead of listing what is excluded, it lists the fixed 62 characters that are
included, `A` through `Z`, `a` through `z`, and `0` through `9`, a set ASCII froze in 1963 that
Unicode can never add a 63rd member to.

This module distinguishes two different things a caller can do with `model`, because they are
not the same event. A caller that never passes the `model` keyword at all is a legacy caller
(existing call sites, existing tests) asking to skip the check entirely; that is the compatibility
shape below and it is unchanged. A caller that resolves a live response and finds no model there
(crews.judge.meta(response)["model"] is None when the response carried no "model" key, or carried
an explicit null) is a different event: it knows the check applies, and it knows the answering
model that check needs is missing. Reading that missing value as "skip" collapses it into the
first case and lets an unidentified answering model admit against a band that requires one, which
is the same absence-as-permission failure this guard exists to refuse. So `model` defaults to a
private sentinel, not to `None`: omitting the keyword still skips the check (unchanged), but
`model=None` now runs the check and is treated as an unverifiable, non matching model whenever the
band itself records a real one, exactly like a recorded model that differs.

Omitting the keyword remains a legitimate skip, justified in one sentence: it identifies a caller
that has never resolved an answering model at all (a call site that predates JEV-16, or one this
module does not yet know about), not a caller that looked at a live response and found nothing, so
treating the omission as "not applicable" is a compatibility default rather than the absence itself
being read as permission. It would become a hole again only if some future call site started
routing a live judge answer through `gate_need` while never passing `model` at all, the same way
crews.cli.core_budget already does at crews/cli.py:256-257 (`model=info.get("model")`, always
passed, even when that value is None); the day a live call site stops passing the keyword is the
day this compatibility shape needs re-examining, not before.

Interface
    gate_need(need, bands, *, criteria_hash=_UNSET, model=_UNSET) -> {"decision", "reason",
                                                                       "threshold_act",
                                                                       "threshold_confirm"}
        need is the typed need answer, {"confidence": float, ...} (crews/budget.py's shape), or
        a bare confidence float. bands is {"act": <derive_threshold result>, "confirm": <...>},
        the shape crews.bands.derive_bands and crews.bands.bands_status return; it may also carry
        a top level "provenance": {"criteria_hash": ..., "model": ...} (crews.cli
        cmd_calibrate_bands' and crew install-band's shape).
        criteria_hash is the sha256 hex digest of the current catalog's need criteria (see
        crews.cli._need_criteria_hash); OMIT the keyword entirely (leave it at the private _UNSET
        default) to skip the drift and no-criteria-version checks entirely, the legacy
        compatibility shape (existing callers and existing bands files, with no provenance, are
        unaffected). Passing `criteria_hash=None` is NOT the same as omitting it (crew task
        visible-identity-closer, JEV-32, giving criteria_hash the same _UNSET sentinel default
        `model` already has, so the two guards answer "omitted versus resolved to None" the same
        way): it means the caller looked and found no criteria hash to assert, and the check still
        runs on that basis, exactly like `model=None` below. Once the check runs, a criteria_hash
        that is None or blank after normalize_identity ("", "   ", "\t\n", a zero width space, ...)
        is treated as an unverifiable, non matching criteria hash whenever the band itself records
        a real one.
        model is the model that actually answered the live call whose need this gates (crews.judge
        meta(response)["model"]); OMIT the keyword entirely to skip the model checks, the same
        compatibility shape criteria_hash now has. Passing `model=None` is not the same as
        omitting it: it means the caller looked and found no answering model, and the check still
        runs on that basis (see above).
        decision in {"admit", "confirm", "reject", "uncalibrated"}:
          - criteria_hash given, both sides normalize to a real hash, and they differ -> "uncalibrated"
          - criteria_hash given, a real band exists, and the band's own criteria hash
            is missing or blank after normalization                    -> "uncalibrated"
          - criteria_hash given but blank after normalization, and a real band
            exists that records a real criteria hash                   -> "uncalibrated"
          - criteria_hash given but blank, and a real band exists whose own
            criteria hash is also missing or blank                     -> "uncalibrated"
          - model given, both sides normalize to a real string, and they differ -> "uncalibrated"
          - model given as None or blank (answering model missing or unnamed), and the
            band records a real provenance model                       -> "uncalibrated"
          - model given as a real string, a real band exists, and its provenance
            model is missing or blank after normalization               -> "uncalibrated"
          - model given as None or blank, and a real band exists whose provenance
            model is also missing or blank                              -> "uncalibrated"
          - no measured act band                                      -> "uncalibrated"
          - confidence >= threshold_act                                -> "admit"
          - confidence < threshold_act, but a measured confirm band
            exists and confidence >= threshold_confirm                 -> "confirm"
          - otherwise                                                  -> "reject"
        threshold_act and threshold_confirm are the measured threshold when that band is
        MEASURED, else None. On any drift or unverifiable-provenance case above, both are None:
        an unverifiable band's threshold is never surfaced.
"""

from __future__ import annotations

from typing import Any, Final

# crew task positive-allowlist-closer (2026-09-22, JEV-35): the fixed 62 character set an identity
# must carry at least one member of. This replaces the five category denylist
# (`_INVISIBLE_CATEGORIES`, {Cc, Cf, Zs, Zl, Zp}) that JEV-30/JEV-32/JEV-33 built and that a round
# six blind hunt then defeated with 17 further admitting values spanning seven other Unicode general
# categories (Mn, Lo, So, Co, Cn, Cs, Sk; see the module docstring above for the full set, and for
# why one of them, U+30000, is an assigned, rendering CJK letter rather than another invisible
# shape and still belongs on the list). Five consecutive repairs each widened a denylist by one
# category and each was defeated by the next code point nobody had listed, because Unicode grows and a denylist
# can only ever name what has already been found. A category based positive rule fails the same
# way: U+3164, U+115F, and U+1160 (the Hangul fillers) are category Lo, the LETTER category, and
# still render as nothing. This set is different in kind, not degree: it is closed by definition.
# ASCII fixed its 128 code points in 1963 and Unicode can never add a 63rd letter or digit to this
# 62 character set, so no future Unicode release, of any version, in any category, can ever widen
# what this set admits. A value is an identity only when it contains at least one of these 62
# characters; every code point outside them, whatever category a future Unicode version files it
# under, carries no identity on its own.
# crew task docstring-truth-and-control-char-closer (2026-09-22, JEV-37/JEV-39): the fixed 33 code
# point ASCII control set, `U+0000` through `U+001F` plus `U+007F`. This closes a residual a blind
# hunter found narrower than the six holes the allowlist above already closes: `"\x1b[8m\x1b[0m"`,
# `"\r0\r"`, and `"0\x08 \x08"` each carry a real ASCII alphanumeric character (`8`, `0`, `0`) and
# so already survived `normalize_identity` before this closure, yet a terminal renders each of them
# as nothing (an SGR conceal or reset sequence, a carriage return overwrite, a backspace erase). The
# allowlist above answers "does this string carry an identity character at all"; this set answers a
# different question, "does this string also carry a character that can hide or overwrite one", so
# a value with both is refused rather than treated as a legitimate identity that merely happens to
# have decoration attached. Like the 62 character allowlist, this is a second frozen set: ASCII
# fixed these 33 control code points in 1963 exactly as it fixed the 62 alphanumerics, so rejecting
# them is not the unwinnable Unicode denylist (`_INVISIBLE_CATEGORIES`, five widenings, five
# defeats) this module spent five rounds losing to; nothing about it grows as Unicode grows.
_ASCII_CONTROL: Final = frozenset(chr(codepoint) for codepoint in range(0x00, 0x20)) | frozenset("\x7f")

_ASCII_ALPHANUMERIC: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)

_MEASURED = "MEASURED"


class _Unset:
    """Sentinel default shared by the `model` keyword (JEV-17) and the `criteria_hash` keyword
    (crew task visible-identity-closer, JEV-32, matching `model`'s own shape so the two guards
    answer the same "omitted versus resolved to None" question the same way): it distinguishes
    "the caller never passed this keyword at all" (skip the check, the legacy compatibility shape
    both guards keep) from "the caller passed the keyword as `None`" (the check must still run,
    because the caller looked and found nothing there, whether that is a live response carrying no
    answering model or a resolved criteria hash that came back as `None`; either is exactly the
    unverifiable-band case each guard exists to refuse rather than silently admit)."""

    def __repr__(self) -> str:  # pragma: no cover (debug aid only)
        return "<unset>"


_UNSET: Final = _Unset()


def normalize_identity(value: Any) -> str | None:
    """The one rule every identity comparison in this module applies, to both operands of both
    guards. THE COMBINED RULE (crew task docstring-truth-and-control-char-closer, JEV-37/JEV-39):
    an identity is a string that carries at least one ASCII alphanumeric character (one of the
    fixed 62 characters `A` through `Z`, `a` through `z`, or `0` through `9`) and carries no ASCII
    control character (one of the fixed 33 code points `U+0000` through `U+001F` or `U+007F`);
    every other value, string or not, normalizes to None, meaning "this side names no identity at
    all". Both halves are properties of the string's content over a fixed, closed set, not a list
    of the specific blank, invisible, or terminal hiding shapes found so far, and not a list of the
    Unicode general categories a code point happens to render as nothing under (crew task
    positive-allowlist-closer, JEV-35, replacing that category denylist; see the module docstring
    above for why a denylist, and even a category based positive rule, both stay defeatable while a
    fixed ASCII allowlist does not). The control character half is closed the same way: ASCII froze
    those 33 code points in 1963 exactly as it froze the 62 alphanumerics, so ruling them out is
    two frozen sets doing their own separate jobs, never the reopened Unicode denylist this module
    spent five rounds losing to.

    A missing field, an explicit None, a wrong type (int, bool, list, dict, ...), an empty string,
    a whitespace only string, a string built entirely from zero width or other format, control, or
    separator characters, and a string built entirely from any other code point outside the ASCII
    alphanumeric set (a combining mark, a Hangul filler, a Braille blank cell, a private use
    character, an unassigned code point, a lone surrogate, an emoji modifier with no base emoji,
    or any mix of these together, alone or padded with real whitespace) all normalize to this same
    None: none of them name a real identity any more than absence does, because none of them
    contains one of the 62 characters this function requires. This corrects two prior repairs in
    turn: `value.strip() != ""` (closed only WHITESPACE only shapes, JEV-30 defeated it with eight
    zero width and control code points that `.strip()` never touches), and the category denylist
    that replaced it, `unicodedata.category(char) not in {Cc, Cf, Zs, Zl, Zp}` (closed those eight
    plus every other Cc, Cf, Zs, Zl, or Zp code point, but a round six blind hunt then found 17
    further admitting values in seven OTHER categories, Mn, Lo, So, Co, Cn, Cs, and Sk, because
    no single enumerable Unicode property, whether "renders as nothing", "is unassigned", or any
    other, was ever the real boundary).

    A value is an identity only when it contains at least one of the 62 ASCII alphanumeric
    characters and contains none of the 33 ASCII control characters; a value with no alphanumeric
    character, or with any control character present alongside one, survives to None. Three round
    seven values prove this residual: `"\x1b[8m\x1b[0m"` (an SGR conceal/reset escape wrapped
    around no visible payload), `"\r0\r"` (a `0` bracketed by carriage returns that overwrite it at
    column zero), and `"0\x08 \x08"` (a `0` erased by a backspace, a blank, and a second backspace)
    each carry a real ASCII alphanumeric character and so admitted under the allowlist alone before
    this closure, yet each renders as nothing on a terminal. `_band_model` and `_band_criteria_hash` both
    apply this on the band side; `gate_need` applies it to the caller's own `model` and
    `criteria_hash` values, so both guards read both of their operands through the same rule. The
    type check above this rule is exact (`type(value) is str`), not `isinstance`, so a `str`
    subclass that overrides `__eq__`, `__ne__`, or `strip()` to lie about its own content normalizes
    to None rather than being trusted (crew task positive-allowlist-closer, JEV-35, closing the
    subclass equality bypass at its source: `type(value) is str` also means every equality this
    module performs downstream, `band_model != caller_model` and `band_hash != caller_hash`, only
    ever compares two genuine `str` instances, so no subclass's overridden `__ne__` is ever
    consulted either).

    A real identity that passes both halves of the rule is returned exactly as given, not stripped
    or otherwise rewritten: surrounding ordinary whitespace (a space) or an interior zero width
    character stays part of the returned string, because this function's only job, once a value
    clears both halves, is to decide whether it names anybody at all, never to canonicalize what it
    returns once it does. This holds only for whitespace that is not itself an ASCII control
    character: a space (`U+0020`) is not in `_ASCII_CONTROL` and so a space padded identity like
    `"  jev-1  "` returns unchanged, but a tab (`U+0009`) is one of the 33 ASCII control code points,
    so a tab padded identity like `"\\tjev-1\\t"` is refused outright by the control character half
    above and returns None, never the padded string."""
    if type(value) is not str:
        return None
    if any(char in _ASCII_CONTROL for char in value):
        return None
    if any(char in _ASCII_ALPHANUMERIC for char in value):
        return value
    return None


def _band_threshold(bands: dict[str, Any] | None, name: str) -> float | None:
    band = bands.get(name) if isinstance(bands, dict) else None
    if isinstance(band, dict) and band.get("status") == _MEASURED:
        threshold = band.get("threshold")
        return float(threshold) if threshold is not None else None
    return None


def _band_criteria_hash(bands: dict[str, Any] | None) -> str | None:
    """The measured criteria hash a band carries, from its top level "provenance" (crews.cli
    cmd_calibrate_bands' shape), normalized by `normalize_identity`: None when the band carries no
    provenance at all, or when its `criteria_hash` is missing, wrongly typed, empty, or whitespace
    only. A legacy or hand built bands mapping with no provenance never triggers the drift check; a
    band with a blank criteria hash reads the same way as one with none, never as an identity that
    could accidentally match another blank one (crew task blank-identity-normalization-closer,
    JEV-33)."""
    if not isinstance(bands, dict):
        return None
    provenance = bands.get("provenance")
    if isinstance(provenance, dict):
        return normalize_identity(provenance.get("criteria_hash"))
    return None


def _band_model(bands: dict[str, Any] | None) -> str | None:
    """The measured model a band carries, from its top level "provenance" (JEV-16: a band is only
    valid for the model that answered the observations it was measured on), normalized by
    `normalize_identity`: None when the band names no real model identity, whether the cause is a
    missing "provenance" key, `provenance=None`, `provenance={}`, a non dict `provenance`, a
    missing `provenance.model` key, `provenance.model=None`, a non string `provenance.model`, an
    empty string, a whitespace only string, or a string built only from characters outside the
    fixed ASCII alphanumeric set (JEV-35: `normalize_identity` reads any such code point as naming
    no identity, so `provenance.model` set to nothing but a byte order mark, a zero width space, or
    any other non ASCII alphanumeric character is None here too, never a real recorded model). A
    legacy or hand built bands
    mapping with no provenance never triggers the model check, mirroring `_band_criteria_hash`
    above."""
    if not isinstance(bands, dict):
        return None
    provenance = bands.get("provenance")
    if isinstance(provenance, dict):
        return normalize_identity(provenance.get("model"))
    return None


def _uncalibrated_reason(bands: dict[str, Any] | None) -> str:
    """This is only ever called once `_band_threshold(bands, "act")` has already returned None, so
    every branch below names a distinct way that can happen, and the reason text stays
    distinguishable across all four even though each one is the same "uncalibrated" decision:
    "no band installed" (no `bands` dict, no "act" entry, or the "act" entry is not itself a
    dict), "act band is INSUFFICIENT", "act band carries some other non MEASURED status" (neither
    "MEASURED" nor "INSUFFICIENT"), and "act band status is MEASURED but its threshold is not
    recorded" (a MEASURED band whose `threshold` field is None or absent, the one case a plain
    `status != "MEASURED"` comparison cannot name without contradicting itself)."""
    if not isinstance(bands, dict) or "act" not in bands:
        return "no measured act band: no band installed"
    band = bands.get("act")
    if not isinstance(band, dict):
        return "no measured act band: no band installed"
    status = band.get("status")
    if status == "INSUFFICIENT":
        return f"no measured act band: act band is INSUFFICIENT (n_total={band.get('n_total')})"
    if status == _MEASURED:
        return "no measured act band: act band status is MEASURED but its threshold is not recorded"
    return f"no measured act band: act band status is {status!r}, not MEASURED"


def _need_confidence(need: Any) -> float:
    if isinstance(need, dict):
        confidence = need.get("confidence")
        if confidence is None:
            raise ValueError("gate_need: need carries no 'confidence'")
        return float(confidence)
    if isinstance(need, (int, float)):
        return float(need)
    raise ValueError(f"gate_need: need must be a dict with 'confidence' or a number, got {type(need).__name__}")


def gate_need(need: Any, bands: dict[str, Any], *, criteria_hash: str | None | _Unset = _UNSET,
              model: str | None | _Unset = _UNSET) -> dict[str, Any]:
    if not isinstance(criteria_hash, _Unset):
        # Reaching this line means the caller passed the `criteria_hash` keyword at all, whether as
        # a real string, as None, or as a blank one: only the true default, the _UNSET sentinel,
        # skips this block, so a legacy caller that never mentions `criteria_hash` is unaffected
        # (crew task visible-identity-closer, JEV-32: `criteria_hash` used to test `is not None`
        # here, which could not tell "the caller never passed this keyword" apart from "the caller
        # passed criteria_hash=None"; both read as "not passed" and skipped the check, so a caller
        # that resolved a criteria hash and got back None admitted against a band recording a real
        # one, the exact shape the model guard below already refuses via the same _Unset sentinel.
        # criteria_hash=None now runs this block and is normalized like every other value: a blank
        # string ("", "   ", "\t\n", a zero width space, ...) still enters this block exactly like
        # a real one, because the caller is still asserting "the criteria in force is known" or, in
        # the None case, "I looked and found no criteria hash to assert". normalize_identity then
        # decides, on both sides, whether either value actually names an identity, mirroring the
        # model check below and closing the same hole JEV-25's precedent left latent: _band_
        # criteria_hash previously accepted any `str`, with no non empty test at all, so two blank
        # criteria hashes compared equal).
        band_hash = _band_criteria_hash(bands)
        caller_hash = normalize_identity(criteria_hash)
        both_named = band_hash is not None and caller_hash is not None
        if both_named and band_hash != caller_hash:
            return {
                "decision": "uncalibrated",
                "reason": (
                    f"band criteria drift: band measured against criteria hash {band_hash}, "
                    f"catalog need criteria is now {caller_hash}"
                ),
                "threshold_act": None,
                "threshold_confirm": None,
            }
        if not both_named and _band_threshold(bands, "act") is not None:
            # There is no admission from absence (this module's own principle above): the act
            # band is MEASURED (so it would otherwise admit or reject on its threshold), but at
            # least one side names no criteria identity to verify the other against, including the
            # case where NEITHER side names one (a band with a blank or absent criteria hash,
            # checked against a caller supplied criteria_hash that is itself blank: the pairing
            # JEV-33 found admitting). A band with a recorded, matching or drifted hash is handled
            # above; a bands mapping with no "act" entry at all, or an act band that is not itself
            # MEASURED (INSUFFICIENT or otherwise), falls through to the checks below and keeps
            # its own distinguishable _uncalibrated_reason text rather than being masked by this
            # one.
            if band_hash is not None:
                reason = (
                    f"criteria hash missing: band measured against criteria hash {band_hash}, "
                    "but the given criteria hash names no real identity to check against it"
                )
            elif caller_hash is not None:
                reason = (
                    f"band records no criteria version: catalog need criteria is now {caller_hash}, "
                    "but this band carries no provenance criteria hash to check against it"
                )
            else:
                reason = (
                    "no criteria version recorded on either side: this band carries no provenance "
                    "criteria hash and the given criteria hash names no real identity, so neither "
                    "can be checked against the other"
                )
            return {
                "decision": "uncalibrated",
                "reason": reason,
                "threshold_act": None,
                "threshold_confirm": None,
            }

    if not isinstance(model, _Unset):
        # `model` reaching this line means the caller passed the keyword at all, whether as a real
        # string, as None, or as a blank one (see the module docstring): only the true default,
        # the _UNSET sentinel, skips this block, so a legacy caller that never mentions `model` is
        # unaffected.
        #
        # THE SINGLE RULE (crew task blank-identity-normalization-closer, JEV-33, closing the class
        # rather than a fourth named instance): normalize both sides with `normalize_identity`, the
        # same function `_band_model` already applies on the band side and the criteria hash check
        # above applies to both of its own operands. The check proceeds past this gate ONLY when
        # both sides normalize to a real string and those strings are equal; every other cell of
        # that 2x2 truth table, including the one where NEITHER side names anything and the one
        # where BOTH sides are the same blank string (the pairing JEV-33 found admitting), is
        # refused. This is a predicate over `normalize_identity(band) == normalize_identity(caller)
        # is not None`, not an enumeration of specific absent or blank shapes, so it needs no list
        # of which strings count as blank to stay correct.
        band_model = _band_model(bands)
        caller_model = normalize_identity(model)
        both_named = band_model is not None and caller_model is not None
        if both_named and band_model != caller_model:
            # Both sides name a real identity and they differ: an unconditional refusal, mirroring
            # the criteria hash drift case above exactly (JEV-17, matching the JEV-25 precedent).
            return {
                "decision": "uncalibrated",
                "reason": (
                    f"band model mismatch: band measured against model {band_model}, "
                    f"answering model is {caller_model}"
                ),
                "threshold_act": None,
                "threshold_confirm": None,
            }
        if not both_named and _band_threshold(bands, "act") is not None:
            # There is no admission from absence (this module's own principle above): the act band
            # is MEASURED (so it would otherwise admit or reject on its threshold), but at least one
            # side names no model identity to verify the other against, including the case where
            # NEITHER side names one (JEV-17: this is the hole this crew task closes, a legacy band
            # with no provenance model checked by a response that also carried no answering model).
            # A bands mapping with no "act" entry at all, or an act band that is not itself
            # MEASURED, falls through to the checks below and keeps its own distinguishable
            # _uncalibrated_reason text rather than being masked by this one.
            if band_model is not None:
                reason = (
                    f"answering model missing: band measured against model {band_model}, "
                    "but this response carries no answering model to check against it"
                )
            elif caller_model is not None:
                reason = (
                    f"band records no model: answering model is {caller_model}, "
                    "but this band carries no provenance model to check against it"
                )
            else:
                reason = (
                    "no answering model recorded on either side: this response carries no "
                    "answering model and this band carries no provenance model, so neither can be "
                    "checked against the other"
                )
            return {
                "decision": "uncalibrated",
                "reason": reason,
                "threshold_act": None,
                "threshold_confirm": None,
            }
        # Either both_named and band_model == caller_model (the one admitting cell), or neither side
        # names a model and there is no real MEASURED act band to gate on yet: proceed, and let the
        # threshold logic below (or its own _uncalibrated_reason) decide.

    threshold_act = _band_threshold(bands, "act")
    threshold_confirm = _band_threshold(bands, "confirm")

    if threshold_act is None:
        return {
            "decision": "uncalibrated",
            "reason": _uncalibrated_reason(bands),
            "threshold_act": threshold_act,
            "threshold_confirm": threshold_confirm,
        }

    confidence = _need_confidence(need)

    if confidence >= threshold_act:
        return {
            "decision": "admit",
            "reason": f"confidence {confidence} >= act threshold {threshold_act}",
            "threshold_act": threshold_act,
            "threshold_confirm": threshold_confirm,
        }

    if threshold_confirm is not None and confidence >= threshold_confirm:
        return {
            "decision": "confirm",
            "reason": (
                f"confidence {confidence} below act threshold {threshold_act}, "
                f"but >= confirm threshold {threshold_confirm}"
            ),
            "threshold_act": threshold_act,
            "threshold_confirm": threshold_confirm,
        }

    if threshold_confirm is None:
        reason = f"confidence {confidence} below act threshold {threshold_act}; no measured confirm band"
    else:
        reason = (
            f"confidence {confidence} below act threshold {threshold_act} "
            f"and confirm threshold {threshold_confirm}"
        )
    return {
        "decision": "reject",
        "reason": reason,
        "threshold_act": threshold_act,
        "threshold_confirm": threshold_confirm,
    }
