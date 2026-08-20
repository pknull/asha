"""Shared terminal-safe Unicode admission policy for Control text."""

from __future__ import annotations

import unicodedata


ZWJ = "\u200d"
KEYCAP = "\u20e3"


def is_variation_selector(character: str) -> bool:
    codepoint = ord(character)
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def is_emoji_modifier(character: str) -> bool:
    return 0x1F3FB <= ord(character) <= 0x1F3FF


def is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def is_emoji_base(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF or
        0x2600 <= codepoint <= 0x27FF or
        codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x3030, 0x303D}
    )


def is_emoji_modifier_base(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint in {0x261D, 0x26F9, 0x1F385, 0x1F3C7, 0x1F47C, 0x1F48F,
                      0x1F491, 0x1F4AA, 0x1F57A, 0x1F590, 0x1F6A3,
                      0x1F6C0, 0x1F6CC, 0x1F926, 0x1F90C, 0x1F90F,
                      0x1F977, 0x1F9BB} or
        0x270A <= codepoint <= 0x270D or
        0x1F3C2 <= codepoint <= 0x1F3C4 or
        0x1F3CA <= codepoint <= 0x1F3CC or
        0x1F442 <= codepoint <= 0x1F443 or
        0x1F446 <= codepoint <= 0x1F450 or
        0x1F466 <= codepoint <= 0x1F478 or
        0x1F481 <= codepoint <= 0x1F483 or
        0x1F485 <= codepoint <= 0x1F487 or
        0x1F574 <= codepoint <= 0x1F575 or
        0x1F595 <= codepoint <= 0x1F596 or
        0x1F645 <= codepoint <= 0x1F647 or
        0x1F64B <= codepoint <= 0x1F64F or
        0x1F6B4 <= codepoint <= 0x1F6B6 or
        0x1F918 <= codepoint <= 0x1F91F or
        0x1F930 <= codepoint <= 0x1F939 or
        0x1F93C <= codepoint <= 0x1F93E or
        0x1F9B5 <= codepoint <= 0x1F9B6 or
        0x1F9B8 <= codepoint <= 0x1F9B9 or
        0x1F9CD <= codepoint <= 0x1F9CF or
        0x1F9D1 <= codepoint <= 0x1F9DD or
        0x1FAC3 <= codepoint <= 0x1FAC5 or
        0x1FAF0 <= codepoint <= 0x1FAF8
    )


def is_cluster_extension(character: str) -> bool:
    return (
        bool(unicodedata.combining(character)) or
        unicodedata.category(character) in {"Mn", "Me"} or
        is_variation_selector(character)
    )


_PROFESSION_BASES = frozenset({"👨", "👩", "🧑"})
_PROFESSION_TARGET = "💻"


def is_supported_zwj_prefix(value: str) -> bool:
    if value.count(ZWJ) != 1 or not value.endswith(ZWJ):
        return False
    left = value[:-1]
    bases = [
        character for character in left
        if not is_variation_selector(character) and
        not is_emoji_modifier(character)
    ]
    modifiers = [character for character in left if is_emoji_modifier(character)]
    return (
        len(bases) == 1 and bases[0] in _PROFESSION_BASES and
        len(modifiers) <= 1
    )


def is_supported_zwj_sequence(value: str) -> bool:
    if value.count(ZWJ) != 1:
        return False
    left, right = value.split(ZWJ)
    if not is_supported_zwj_prefix(left + ZWJ):
        return False
    return "".join(
        character for character in right
        if not is_variation_selector(character)
    ) == _PROFESSION_TARGET


def display_clusters(value: str) -> list[str]:
    clusters: list[str] = []
    for character in value:
        if not clusters:
            clusters.append(character)
            continue
        current = clusters[-1]
        if is_emoji_modifier(character):
            visible = [
                item for item in current
                if not is_cluster_extension(item) and item != ZWJ and
                not is_emoji_modifier(item) and item != KEYCAP
            ]
            if (ZWJ not in current and visible and
                    is_emoji_modifier_base(visible[-1]) and
                    not any(is_emoji_modifier(item) for item in current)):
                clusters[-1] += character
            else:
                clusters.append(character)
            continue
        if character == KEYCAP:
            plain = "".join(
                item for item in current if not is_variation_selector(item)
            )
            if (not current.endswith(ZWJ) and KEYCAP not in current and
                    len(plain) == 1 and plain in "#*0123456789"):
                clusters[-1] += character
            else:
                clusters.append(character)
            continue
        if current.endswith(ZWJ) and is_supported_zwj_sequence(current + character):
            clusters[-1] += character
            continue
        if character == ZWJ:
            if is_supported_zwj_prefix(current + character):
                clusters[-1] += character
            else:
                clusters.append(character)
            continue
        if is_cluster_extension(character):
            clusters[-1] += character
            continue
        if (is_regional_indicator(character) and
                sum(is_regional_indicator(item) for item in current) % 2 == 1 and
                all(is_regional_indicator(item) for item in current)):
            clusters[-1] += character
            continue
        clusters.append(character)
    return clusters


def prompt_character_allowed(value: str, character: str) -> bool:
    category = unicodedata.category(character)
    if character == ZWJ:
        return bool(value) and is_supported_zwj_prefix(
            display_clusters(value)[-1] + ZWJ,
        )
    if is_variation_selector(character):
        if not value:
            return False
        cluster = display_clusters(value)[-1]
        return (
            not cluster.endswith(ZWJ) and
            not any(is_variation_selector(item) for item in cluster) and
            any(is_emoji_base(item) or item in "#*0123456789" for item in cluster)
        )
    if is_emoji_modifier(character):
        if not value:
            return False
        cluster = display_clusters(value)[-1]
        visible = [
            item for item in cluster
            if not is_cluster_extension(item) and item != ZWJ and
            not is_emoji_modifier(item) and item != KEYCAP
        ]
        return (
            ZWJ not in cluster and bool(visible) and
            is_emoji_modifier_base(visible[-1]) and
            not any(is_emoji_modifier(item) for item in cluster)
        )
    if character == KEYCAP:
        if not value:
            return False
        cluster = display_clusters(value)[-1]
        plain = "".join(
            item for item in cluster if not is_variation_selector(item)
        )
        return (
            not cluster.endswith(ZWJ) and KEYCAP not in cluster and
            len(plain) == 1 and plain in "#*0123456789"
        )
    if is_cluster_extension(character) and not value:
        return False
    if value.endswith(ZWJ):
        return is_supported_zwj_sequence(display_clusters(value)[-1] + character)
    return character.isprintable() and category not in {"Cc", "Cf", "Cs"}


def terminal_text_is_complete(value: str) -> bool:
    """Return whether every cluster follows Control's admitted text grammar."""
    if not value:
        return True
    for cluster in display_clusters(value):
        if any(
            unicodedata.category(character) in {"Cc", "Cs"} or
            (unicodedata.category(character) == "Cf" and character != ZWJ)
            for character in cluster
        ):
            return False
        if ZWJ in cluster and not is_supported_zwj_sequence(cluster):
            return False
        visible = [
            character for character in cluster
            if not is_cluster_extension(character) and character != ZWJ and
            not is_emoji_modifier(character) and character != KEYCAP
        ]
        if not visible or any(not character.isprintable() for character in visible):
            return False
        modifiers = [character for character in cluster if is_emoji_modifier(character)]
        if modifiers and (
            len(modifiers) != 1 or not is_emoji_modifier_base(visible[0])
        ):
            return False
        selectors = [character for character in cluster if is_variation_selector(character)]
        if selectors and (
            len(selectors) != 1 or
            not any(is_emoji_base(item) or item in "#*0123456789" for item in visible)
        ):
            return False
        if KEYCAP in cluster and not (
            cluster.count(KEYCAP) == 1 and len(visible) == 1 and
            visible[0] in "#*0123456789"
        ):
            return False
        regional = [item for item in visible if is_regional_indicator(item)]
        if regional and (len(regional) not in {1, 2} or len(regional) != len(visible)):
            return False
    return True
