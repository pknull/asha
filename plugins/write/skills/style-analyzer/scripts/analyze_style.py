#!/usr/bin/env python3
"""
Style Analyzer - Quantified prose analysis for voice.md generation.

Extracts measurable style patterns from exemplar texts:
- Sentence metrics (length, variance, rhythm)
- Dialogue analysis (ratio, tags, attribution, quote style)
- Vocabulary profile (diversity, adverbs, adjective stacking)
- Paragraph structure
- Forbidden pattern detection (filter words, hedging, AI signals)
- Repetition analysis

Usage:
    python analyze_style.py <file.txt> [--json]
    python analyze_style.py <directory/> [--json]
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median, stdev
from typing import NamedTuple


# === AI Signal Words (known flat prose indicators) ===
AI_SIGNAL_WORDS = {
    # Hedging
    "seemingly", "apparently", "somewhat", "perhaps", "arguably",
    "presumably", "ostensibly", "supposedly", "conceivably",
    # Overused transitions
    "furthermore", "moreover", "additionally", "consequently",
    "nevertheless", "nonetheless", "subsequently", "ultimately",
    # Generic intensifiers
    "incredibly", "absolutely", "literally", "fundamentally",
    "essentially", "basically", "actually", "definitely",
    # Flat descriptors
    "various", "numerous", "significant", "substantial",
    "considerable", "notable", "remarkable", "profound",
    # AI-favored constructions
    "delve", "utilize", "leverage", "facilitate", "implement",
    "foster", "enhance", "optimize", "streamline", "navigate",
    # Emotional tells
    "palpable", "visceral", "tangible", "resonated", "struck",
}

# Filter word patterns (should show, not tell)
FILTER_PATTERNS = [
    r"\b(he|she|they|I)\s+(saw|heard|felt|noticed|realized|wondered|thought|knew)\b",
    r"\b(could see|could hear|could feel|could tell)\b",
    r"\b(watched as|listened to|observed)\b",
]

# Hedging patterns
HEDGE_PATTERNS = [
    r"\bseemed to\b",
    r"\bappeared to\b",
    r"\bas if\b",
    r"\bsort of\b",
    r"\bkind of\b",
    r"\ba bit\b",
    r"\bsomewhat\b",
    r"\brather\b",
    r"\bquite\b",
    r"\bsomehow\b",
]

# === Categorized Cliche Detection ===
# Each category: id (stable slug), name (human label), why (craft rationale),
# patterns (regex list). Optional: detector (custom function name), threshold.
# Source: r/WritingWithAI community + craft analysis.

CLICHE_CATEGORIES: list[dict] = [
    # --- Fiction Cliche Families (28 categories) ---
    {
        "id": "body_as_metaphor",
        "name": "Body-as-Metaphor Cliches",
        "why": "Stock physical reactions replace character-specific emotional rendering",
        "patterns": [
            r"\bchest tightened\b",
            r"\bbreath caught\b",
            r"\bstomach dropped\b",
            r"\bheart hammered\b",
            r"\bthroat tightened\b",
            r"\bpulse quickened\b",
            r"\bmouth went dry\b",
            r"\bknees went weak\b",
            r"\bgut twisted\b",
            r"\bvision blurred\b",
            r"\bbreath hitched\b",
            r"\bsent (a )?shivers?\b",
        ],
    },
    {
        "id": "cardiac_sequence",
        "name": "Cardiac Sequence",
        "why": "The heart has been overworked into cliche as an emotion organ",
        "patterns": [
            r"\bheart lurched\b",
            r"\bheart stuttered\b",
            r"\bheart climbed\b",
            r"\bheart cracked\b",
            r"\bheart pounded\b",
            r"\bheart hammered\b",
            r"\bheart beat like\b",
            r"\bchest felt hollowed\b",
            r"\bache behind (her|his|their) sternum\b",
        ],
    },
    {
        "id": "breath_as_device",
        "name": "Breath as Narrative Device",
        "why": "Overworked respiratory drama substitutes for earned tension",
        "patterns": [
            r"\bforgot how to breathe\b",
            r"\bcouldn't find enough air\b",
            r"\bbreath (she|he|they) hadn't known\b",
            r"\bdidn't know (he|she|they) was holding\b",
            r"\bexhaled like a prayer\b",
            r"\bbreathed like a confession\b",
            r"\blet out a breath\b",
        ],
    },
    {
        "id": "hands_as_surrogate",
        "name": "Hands as Emotional Surrogate",
        "why": "Hands become the sole vehicle for emotion instead of deeper characterization",
        "patterns": [
            r"\bhands weren't quite steady\b",
            r"\bhands found (her|his|their) face\b",
            r"\bhands curled into fists\b",
            r"\bran a hand through\b",
            r"\bpressed a hand to (her|his|their) mouth\b",
            r"\bhands shook\b",
            r"\bhands trembled\b",
        ],
    },
    {
        "id": "impossible_faces",
        "name": "Faces/Eyes Doing Impossible Work",
        "why": "Loading all emotion onto faces and eyes instead of whole-body behavior",
        "patterns": [
            r"\beyes? (flickered|softened|hardened|clouded|darkened|blazed)\b",
            r"\beyes? (went glassy|went blank|went cold|went flat)\b",
            r"\beyes? (betrayed|shuttered|bored into)\b",
            r"\beyes widened\b",
            r"\bjaw dropped\b",
            r"\bface (fell|crumbled|softened|hardened)\b",
        ],
    },
    {
        "id": "smile_catalogue",
        "name": "Smile Catalogue",
        "why": "Overworked smile variants replace genuine action and behavior",
        "patterns": [
            r"\bghost of a smile\b",
            r"\bshadow of a smile\b",
            r"\bhint of a smile\b",
            r"\bhalf-smile\b",
            r"\bsad smile\b",
            r"\bwry smile\b",
            r"\brueful smile\b",
            r"\bsmile that didn't reach\b",
            r"\bsmile that wasn't really\b",
        ],
    },
    {
        "id": "shimmer_family",
        "name": "Shimmer/Glimmer Family",
        "why": "Creates generic luminous-magical tone that signals AI prose immediately",
        "patterns": [
            r"\bshimmer(ed|ing|s)?\b",
            r"\bglimmer(ed|ing|s)?\b",
            r"\bglisten(ed|ing|s)?\b",
            r"\bgleam(ed|ing|s)?\b",
            r"\bsparkl(ed|ing|es)?\b",
            r"\blight danced\b",
            r"\bsmolder(ed|ing|s)?\b",
            r"\bglow(ed|ing|s)?\b",
            r"\bradiat(ed|ing|es)?\b",
            r"\bbloom(ed|ing|s)?\b",
        ],
    },
    {
        "id": "shadow_worship",
        "name": "Shadow/Darkness Worship",
        "why": "Darkness is not a character; shadow used as lazy atmosphere",
        "patterns": [
            r"\bshadows? pooled\b",
            r"\bdarkness gathered\b",
            r"\bshadows? crept\b",
            r"\bdarkness swallowed\b",
            r"\bshadow of a smile\b",
            r"\bcast a (long )?shadow\b",
            r"\b(loom|lurk)(ed|ing|s)?\b",
            r"\b(shroud|veil|cloak)(ed|ing|s)?\b",
        ],
    },
    {
        "id": "silence_as_drama",
        "name": "Silence/Stillness as Drama",
        "why": "Tension comes from what characters do and don't say, not personified silence",
        "patterns": [
            r"\bsilence stretched\b",
            r"\bsilence fell\b",
            r"\bsilence hung\b",
            r"\bsilence pressed\b",
            r"\bsilence was deafening\b",
            r"\bthe air went still\b",
            r"\btime slowed\b",
            r"\bloaded pause\b",
            r"\bpregnant pause\b",
            r"\bthe space between\b",
        ],
    },
    {
        "id": "something_vagueness",
        "name": "\"Something\" Vagueness",
        "why": "Placeholders for emotions the writer hasn't earned or specified",
        "patterns": [
            r"\bsomething shifted\b",
            r"\bsomething stirred\b",
            r"\bsomething broke\b",
            r"\bsomething passed between\b",
            r"\bsomething unspoken\b",
            r"\bsomething (dark|raw|cold|hot)\b",
            r"\bsomething like (grief|hope|fear|love|pain)\b",
            r"\bsomething (she|he|they) couldn't name\b",
        ],
    },
    {
        "id": "agency_removers",
        "name": "Agency Removers",
        "why": "Removes character volition and signals AI autopilot",
        "patterns": [
            r"\bcouldn't help but\b",
            r"\bfound (himself|herself|themselves|themself)\b",
            r"\bcouldn't stop (himself|herself|themselves)\b",
            r"\bbefore (she|he|they) knew it\b",
            r"\bwithout thinking\b",
            r"\bwithout realizing\b",
        ],
    },
    {
        "id": "cool_observer",
        "name": "Cool Observer Mode",
        "why": "Characters should experience their world, not audit it",
        "patterns": [
            r"\b(regarded|assessed|catalogued|appraised)\b",
            r"\btook in (the |his |her |their )?\b",
            r"\bsurveyed (the |his |her |their )?\b",
            r"\bfiled away\b",
            r"\bcommitted to memory\b",
            r"\bdrank in\b",
            r"\bscanned the room\b",
        ],
    },
    {
        "id": "overworked_dialogue_tags",
        "name": "Overworked Dialogue Tags",
        "why": "Elaborate tags draw attention to themselves; 'said' is invisible",
        "patterns": [
            r"\b(breathed|murmured|mused|ventured|offered)\b",
            r"\b(probed|deadpanned|hazarded|hedged|posited)\b",
            r"\b(opined|interjected)\b",
            r"\bsaid (softly|quietly|finally|simply|carefully|gently|flatly)\b",
        ],
    },
    {
        "id": "vague_depth_adjectives",
        "name": "Vague Depth Adjectives",
        "why": "Gesture at profundity without earning it; tell reader how to feel",
        "patterns": [
            r"\b(profound|ethereal|haunting|poignant|ineffable)\b",
            r"\b(ephemeral|transcendent|liminal|luminous|sublime)\b",
            r"\b(otherworldly|wistful|melancholy|bittersweet|mercurial)\b",
        ],
    },
    {
        "id": "melodramatic_emotion",
        "name": "Melodramatic Emotion Dumps",
        "why": "Naming large emotions distances the reader; dramatize through action instead",
        "patterns": [
            r"\b(anguish|torment|despair|yearning|dread)\b",
            r"\b(hollowness|devastation|desolation|agony|wretchedness)\b",
            r"\b(shattered|obliterated|consumed|overwhelmed|undone)\b",
            r"\b(bereft|inconsolable)\b",
        ],
    },
    {
        "id": "weight_gravity",
        "name": "Weight/Gravity Obsession",
        "why": "Abstract weight metaphors substitute for specific character impact",
        "patterns": [
            r"\bweight of (it|the|this)\b",
            r"\bgravity of the moment\b",
            r"\bsettled over (her|him|them) like a weight\b",
            r"\bpressed down on (her|him|them)\b",
            r"\bheavy with meaning\b",
            r"\b(laden|freighted) with\b",
            r"\bcrushing weight\b",
            r"\bburden of\b",
        ],
    },
    {
        "id": "transition_crutches",
        "name": "Transition/Pacing Crutches",
        "why": "Cut these or replace with concrete action",
        "patterns": [
            r"\bfor a moment\b",
            r"\bin that moment\b",
            r"\bfor a heartbeat\b",
            r"\bfor a beat\b",
            r"\bwithout a word\b",
            r"\bas if on cue\b",
            r"\bdespite everything\b",
            r"\ball at once\b",
            r"\bwhat felt like an eternity\b",
            r"\btime seemed to stop\b",
        ],
    },
    {
        "id": "introspection_filler",
        "name": "Introspection Filler",
        "why": "Write the thought itself, not the act of thinking",
        "patterns": [
            r"\bsuddenly realized\b",
            r"\bturned the thought over\b",
            r"\btried to make sense of\b",
            r"\bwrestled with\b",
            r"\bgrappled with\b",
            r"\bcouldn't shake the feeling\b",
            r"\btried to convince (herself|himself|themselves)\b",
        ],
    },
    {
        "id": "pseudo_profound_nouns",
        "name": "Pseudo-Profound Nouns",
        "why": "Abstract atmospheric nouns need physical grounding to earn their place",
        "patterns": [
            r"\b(void|echo|abyss)\b",
            r"\b(absence|presence)\b",
            r"\b(threshold|precipice)\b",
            r"\b(eternity|infinity)\b",
            r"\b(ember|ash)\b",
            r"\b(fracture|wound|scar)\b",
        ],
    },
    {
        "id": "redundant_intensifiers",
        "name": "Redundant Intensifiers",
        "why": "Strong writing does not need to tell the reader how strongly to feel",
        "patterns": [
            r"\b(truly|utterly|completely|absolutely|simply)\b",
            r"\b(undeniably|inevitably|overwhelmingly)\b",
            r"\b(breathtakingly|impossibly|unbearably)\b",
            r"\b(achingly|devastatingly|indescribably|unfathomably)\b",
        ],
    },
    {
        "id": "overworked_metaphors",
        "name": "Four Overworked Metaphor Families",
        "why": "These are defaults, not metaphors; find what belongs to THIS character",
        "patterns": [
            # FIRE
            r"\bburned in (her|his|their) chest\b",
            r"\bsmoldering\b",
            r"\ba spark in (her|his|their) chest\b",
            r"\bashes where\b",
            # WATER
            r"\bdrowning in (it|grief|sorrow|emotion)\b",
            r"\bpulled under\b",
            r"\btide of (grief|emotion|feeling)\b",
            r"\bflood of (memory|memories|emotion)\b",
            # SHARP
            r"\bcut like glass\b",
            r"\bknife-edge\b",
            r"\bjagged under the surface\b",
            # COLD
            r"\bice in (her|his|their) veins\b",
            r"\bsomething glacial\b",
            r"\bblood ran cold\b",
        ],
    },
    {
        "id": "weather_projection",
        "name": "Weather as Emotional Projection",
        "why": "Pathetic fallacy should be deliberate, not reflexive",
        "patterns": [
            r"\bthe wind whispered\b",
            r"\bthe sky wept\b",
            r"\brain fell like tears\b",
            r"\bas if nature (itself )?understood\b",
            r"\bmirrored the turmoil\b",
        ],
    },
    {
        "id": "metallic_taste_trinity",
        "name": "Copper/Iron/Blood Trinity",
        "why": "Overuse makes these signal AI prose rather than visceral realism",
        "patterns": [
            r"\btasted like copper\b",
            r"\btang of iron\b",
            r"\bmetallic taste of (fear|blood|adrenaline)\b",
            r"\bblood in (her|his|their) mouth\b",
        ],
    },
    {
        "id": "kind_of_construction",
        "name": "\"Kind Of\" Construction",
        "why": "Write the actual thing, not the category of the thing",
        "patterns": [
            r"\bthe kind of (tired|silence|love|fear|pain|anger|cold|dark|quiet)\b",
            r"\bthe kind of .{5,30} that\b",
        ],
    },
    {
        "id": "threshold_metaphor",
        "name": "Doors/Thresholds as Heavy Metaphor",
        "why": "Let rooms be rooms unless the metaphor is genuinely earned",
        "patterns": [
            r"\bthe door between them\b",
            r"\bstood at the threshold\b",
            r"\bthe room felt smaller\b",
            r"\bthe empty chair\b",
            r"\bthe space between them\b",
        ],
    },
    {
        "id": "unsaid_apologies",
        "name": "Unsaid Apologies",
        "why": "The unsaid is powerful once; as a pattern it becomes avoidance",
        "patterns": [
            r"\bthe apology (she|he|they)'?d? never\b",
            r"\bthe words that didn't come\b",
            r"\ball the things neither of them\b",
            r"\bthe thing (she|he|they) didn't say\b",
        ],
    },
    {
        "id": "time_freezing",
        "name": "Time Freezing/World Shrinking",
        "why": "Earn this effect; as a default it signals AI",
        "patterns": [
            r"\bthe world narrowed\b",
            r"\beverything (else )?fell away\b",
            r"\bcolor bled out\b",
            r"\bthe moment stretched\b",
            r"\bheld in amber\b",
            r"\boutside of time\b",
            r"\btime stood still\b",
        ],
    },
    {
        "id": "things_in_body",
        "name": "Things Living in the Body",
        "why": "Embodied metaphor is powerful when earned, not as warehouse for unnamed feelings",
        "patterns": [
            r"\b(grief|rage|anger|fear|love) (she|he|they) kept in\b",
            r"\b(rage|anger|fear) that curled in\b",
            r"\bache that (had )?settled in (her|his|their) bones\b",
            r"\bkept locked behind (her|his|their) teeth\b",
            r"\bcarried it in (her|his|their) body\b",
        ],
    },
    # --- Structural AI Tells (8 categories) ---
    {
        "id": "triplet_framing",
        "name": "Triplet Framing",
        "why": "Rule-of-three becomes formulaic when overused",
        "patterns": [
            r"\b\w+,\s+\w+,\s+and\s+\w+\b",
        ],
    },
    {
        "id": "inspirational_pivot",
        "name": "Inspirational Pivot",
        "why": "'It's not about X, it's about Y' is essay-brain, not story-brain",
        "patterns": [
            r"[Ii]t'?s not about .{3,30},? it'?s about\b",
            r"[Ii]t'?s not .{3,30} ?\u2014 ?it'?s\b",
        ],
    },
    {
        "id": "countdown_pattern",
        "name": "Countdown Pattern",
        "why": "Rhetorical countdown is a non-fiction habit leaking into prose",
        "patterns": [
            r"\bNot \w+\.\s*Not \w+\.\s*(Just|Only|Simply|But)\b",
        ],
    },
    {
        "id": "self_answered_rhetorical",
        "name": "Self-Answered Rhetorical Question",
        "why": "Rhetorical question immediately answered removes reader engagement",
        "patterns": [
            r"\?\s+(The answer|Because|Simple|Yes|No|It was|The truth)\b",
        ],
    },
    {
        "id": "heres_the_thing",
        "name": "\"Here's the Thing\" Phrases",
        "why": "False-casual authority phrases signal AI essay mode",
        "patterns": [
            r"\b[Hh]ere'?s the (kicker|thing|catch|deal|twist|reality)\b",
            r"\b[Ll]et'?s (break this down|unpack this)\b",
            r"\b[Ii]n conclusion\b",
            r"\b[Tt]o sum up\b",
        ],
    },
    {
        "id": "think_of_it_as",
        "name": "\"Think of It As\" Analogies",
        "why": "Pedagogical analogies belong in essays, not fiction",
        "patterns": [
            r"\b[Tt]hink of it as\b",
            r"\b[Ii]magine a world where\b",
            r"\b[Pp]icture this\b",
        ],
    },
    {
        "id": "emdash_overuse",
        "name": "Em-Dash Overuse",
        "why": "High em-dash density signals AI reliance on parenthetical insertion",
        "patterns": [],
        "detector": "emdash_density",
        "threshold": 5.0,
    },
    {
        "id": "anaphora",
        "name": "Anaphora (Repeated Openings)",
        "why": "3+ consecutive sentences with same opening word signals mechanical prose",
        "patterns": [],
        "detector": "anaphora",
        "threshold": 3,
    },
]


# === Custom Detector Functions (for non-regex categories) ===

def _detect_emdash_density(text: str, threshold: float = 5.0) -> list[str]:
    """Flag em-dash overuse by density per 1000 words."""
    emdash_count = text.count('\u2014') + text.count('---')
    word_count = len(text.split())
    if word_count == 0:
        return []
    density = emdash_count / (word_count / 1000)
    if density > threshold:
        return [f"em-dash density: {density:.1f}/1000 words (threshold: {threshold})"]
    return []


def _detect_anaphora(text: str, threshold: float = 3) -> list[str]:
    """Flag 3+ consecutive sentences starting with the same word."""
    sentences = split_sentences(text)
    results = []
    streak_word = None
    streak_count = 0
    for s in sentences:
        words = s.split()
        first_word = words[0].lower() if words else ""
        if first_word == streak_word:
            streak_count += 1
            if streak_count >= int(threshold):
                results.append(
                    f"'{first_word}' opens {streak_count} consecutive sentences"
                )
        else:
            streak_word = first_word
            streak_count = 1
    return results


CUSTOM_DETECTORS = {
    "emdash_density": _detect_emdash_density,
    "anaphora": _detect_anaphora,
}


class StyleMetrics(NamedTuple):
    """Complete style analysis results."""
    sentence_metrics: dict
    dialogue_metrics: dict
    vocabulary_metrics: dict
    paragraph_metrics: dict
    forbidden_patterns: dict
    repetition_metrics: dict
    word_count: int
    sentence_count: int


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, handling common edge cases."""
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Protect common abbreviations from splitting
    abbrevs = ["Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "Sr.", "Jr.", "vs.", "etc.", "e.g.", "i.e."]
    for abbr in abbrevs:
        text = text.replace(abbr, abbr.replace(".", "\x00"))

    # Split on sentence-ending punctuation followed by space+capital or end
    pattern = r'[.!?]+(?=\s+[A-Z]|\s*$)'
    sentences = re.split(pattern, text)

    # Restore abbreviation periods
    sentences = [s.replace("\x00", ".").strip() for s in sentences if s.strip()]

    return sentences


def split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs."""
    paragraphs = re.split(r'\n\s*\n', text)
    return [p.strip() for p in paragraphs if p.strip()]


def extract_dialogue(text: str) -> list[str]:
    """Extract all dialogue from text (quoted speech)."""
    # Match double-quoted dialogue
    double_quotes = re.findall(r'"([^"]*)"', text)
    # Match single-quoted dialogue (less common)
    single_quotes = re.findall(r"'([^']*)'", text)
    # Match em-dash interrupted dialogue
    em_dash = re.findall(r'"([^"]*—[^"]*)"', text)

    return double_quotes + single_quotes + em_dash


def analyze_sentence_metrics(sentences: list[str]) -> dict:
    """Compute sentence-level statistics."""
    if not sentences:
        return {"error": "no sentences found"}

    lengths = [len(s.split()) for s in sentences]

    return {
        "count": len(sentences),
        "mean_length": round(mean(lengths), 1),
        "median_length": median(lengths),
        "std_dev": round(stdev(lengths), 2) if len(lengths) > 1 else 0,
        "min_length": min(lengths),
        "max_length": max(lengths),
        "short_ratio": round(len([l for l in lengths if l < 8]) / len(lengths), 3),
        "long_ratio": round(len([l for l in lengths if l > 25]) / len(lengths), 3),
        "length_distribution": {
            "very_short_1_5": len([l for l in lengths if 1 <= l <= 5]),
            "short_6_10": len([l for l in lengths if 6 <= l <= 10]),
            "medium_11_20": len([l for l in lengths if 11 <= l <= 20]),
            "long_21_30": len([l for l in lengths if 21 <= l <= 30]),
            "very_long_31_plus": len([l for l in lengths if l > 30]),
        }
    }


def analyze_dialogue(text: str, word_count: int) -> dict:
    """Analyze dialogue patterns."""
    dialogue_segments = extract_dialogue(text)
    dialogue_words = sum(len(d.split()) for d in dialogue_segments)

    # Quote style detection
    double_count = len(re.findall(r'"[^"]*"', text))
    single_count = len(re.findall(r"'[^']*'", text))
    em_dash_count = len(re.findall(r'—', text))

    # Tag analysis
    said_count = len(re.findall(r'\bsaid\b', text, re.I))
    asked_count = len(re.findall(r'\basked\b', text, re.I))
    replied_count = len(re.findall(r'\breplied\b', text, re.I))
    whispered_count = len(re.findall(r'\bwhispered\b', text, re.I))
    shouted_count = len(re.findall(r'\bshouted\b', text, re.I))

    total_tags = said_count + asked_count + replied_count + whispered_count + shouted_count

    # Attribution style: "said Name" vs "Name said"
    said_name = len(re.findall(r'\bsaid\s+[A-Z][a-z]+', text))
    name_said = len(re.findall(r'[A-Z][a-z]+\s+said\b', text))

    return {
        "dialogue_ratio": round(dialogue_words / word_count, 3) if word_count > 0 else 0,
        "dialogue_segments": len(dialogue_segments),
        "quote_style": {
            "double_quotes": double_count,
            "single_quotes": single_count,
            "em_dash_interruptions": em_dash_count,
            "dominant": "double" if double_count > single_count else "single" if single_count > 0 else "double"
        },
        "tags": {
            "said": said_count,
            "asked": asked_count,
            "replied": replied_count,
            "whispered": whispered_count,
            "shouted": shouted_count,
            "total": total_tags,
            "said_percentage": round(said_count / total_tags * 100, 1) if total_tags > 0 else 0,
        },
        "attribution_style": {
            "said_name": said_name,
            "name_said": name_said,
            "dominant": "said Name" if said_name > name_said else "Name said" if name_said > 0 else "mixed"
        }
    }


def analyze_vocabulary(text: str, word_count: int) -> dict:
    """Analyze vocabulary patterns."""
    # Tokenize to words
    words = re.findall(r'\b[a-z]+\b', text.lower())
    word_counts = Counter(words)

    unique_words = len(word_counts)
    rare_words = sum(1 for w, c in word_counts.items() if c == 1)

    # Adverb detection (-ly words, excluding common exceptions)
    ly_exceptions = {"only", "early", "daily", "weekly", "monthly", "yearly", "family", "lonely", "lovely", "friendly"}
    adverbs = [w for w in words if w.endswith('ly') and w not in ly_exceptions]
    adverb_density = len(adverbs) / (word_count / 1000) if word_count > 0 else 0

    # Adjective stacking (consecutive adjectives before nouns)
    # Simplified: count comma-separated adjectives
    adj_stacks = re.findall(r'\b([a-z]+,\s*[a-z]+)\s+[a-z]+\b', text.lower())

    # AI signal word detection
    ai_signals_found = [w for w in AI_SIGNAL_WORDS if w in word_counts]
    ai_signal_count = sum(word_counts[w] for w in ai_signals_found)

    return {
        "unique_word_ratio": round(unique_words / len(words), 3) if words else 0,
        "unique_words": unique_words,
        "rare_word_ratio": round(rare_words / len(words), 3) if words else 0,
        "adverb_density_per_1000": round(adverb_density, 2),
        "adverb_count": len(adverbs),
        "top_adverbs": Counter(adverbs).most_common(10),
        "adjective_stacking_count": len(adj_stacks),
        "ai_signals": {
            "count": ai_signal_count,
            "density_per_1000": round(ai_signal_count / (word_count / 1000), 2) if word_count > 0 else 0,
            "words_found": ai_signals_found[:20],  # Limit output
        }
    }


def analyze_paragraphs(paragraphs: list[str]) -> dict:
    """Analyze paragraph structure."""
    if not paragraphs:
        return {"error": "no paragraphs found"}

    sentences_per_para = [len(split_sentences(p)) for p in paragraphs]
    dialogue_paras = sum(1 for p in paragraphs if '"' in p or "'" in p)
    single_sentence = sum(1 for c in sentences_per_para if c == 1)

    return {
        "count": len(paragraphs),
        "mean_sentences": round(mean(sentences_per_para), 1),
        "single_sentence_ratio": round(single_sentence / len(paragraphs), 3),
        "dialogue_paragraph_ratio": round(dialogue_paras / len(paragraphs), 3),
        "length_distribution": {
            "single": single_sentence,
            "short_2_3": sum(1 for c in sentences_per_para if 2 <= c <= 3),
            "medium_4_6": sum(1 for c in sentences_per_para if 4 <= c <= 6),
            "long_7_plus": sum(1 for c in sentences_per_para if c >= 7),
        }
    }


def detect_forbidden_patterns(
    text: str, suppress_categories: set[str] | None = None
) -> dict:
    """Detect filter words, hedging, and categorized clichés."""
    results = {
        "filter_words": [],
        "hedging": [],
        "cliches": [],  # backward-compatible flat list
        "cliche_categories": {},  # new: per-category breakdown
        "suppressed": [],  # categories that were skipped
        "totals": {
            "filter_words": 0,
            "hedging": 0,
            "cliches": 0,
            "cliche_categories": 0,
        },
    }

    # Filter words (unchanged)
    for pattern in FILTER_PATTERNS:
        matches = re.findall(pattern, text, re.I)
        if matches:
            results["filter_words"].extend(
                matches if isinstance(matches[0], str) else [m[0] for m in matches]
            )
    results["totals"]["filter_words"] = len(results["filter_words"])

    # Hedging (unchanged)
    for pattern in HEDGE_PATTERNS:
        matches = re.findall(pattern, text, re.I)
        results["hedging"].extend(matches)
    results["totals"]["hedging"] = len(results["hedging"])

    # Categorized cliche detection
    all_cliche_matches = []
    for category in CLICHE_CATEGORIES:
        cat_id = category["id"]
        if suppress_categories and cat_id in suppress_categories:
            results["suppressed"].append(cat_id)
            continue

        matches = []
        detector_name = category.get("detector")
        if detector_name:
            detector_fn = CUSTOM_DETECTORS.get(detector_name)
            if detector_fn:
                threshold = category.get("threshold")
                matches = detector_fn(text, threshold) if threshold else detector_fn(text)
        else:
            for pattern in category["patterns"]:
                for m in re.finditer(pattern, text, re.I):
                    matches.append(m.group(0))

        if matches:
            results["cliche_categories"][cat_id] = {
                "name": category["name"],
                "why": category["why"],
                "count": len(matches),
                "examples": matches[:10],
            }
            all_cliche_matches.extend(matches)

    # Backward compatibility: flat cliches list
    results["cliches"] = all_cliche_matches[:20]
    results["totals"]["cliches"] = len(all_cliche_matches)
    results["totals"]["cliche_categories"] = sum(
        v["count"] for v in results["cliche_categories"].values()
    )

    # Limit examples
    results["filter_words"] = results["filter_words"][:20]
    results["hedging"] = results["hedging"][:20]

    return results


def analyze_repetition(text: str) -> dict:
    """Detect word and phrase repetition."""
    words = re.findall(r'\b[a-z]+\b', text.lower())
    word_counts = Counter(words)

    # Filter common words
    stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
                  "of", "with", "by", "from", "as", "is", "was", "were", "been", "be",
                  "have", "has", "had", "do", "does", "did", "will", "would", "could",
                  "should", "may", "might", "must", "shall", "can", "it", "its", "this",
                  "that", "these", "those", "i", "you", "he", "she", "we", "they", "him",
                  "her", "his", "my", "your", "our", "their", "me", "us", "them"}

    # Find overused words (appear more than expected)
    content_words = {w: c for w, c in word_counts.items()
                     if w not in stop_words and len(w) > 3 and c > 2}

    total_content = sum(content_words.values())
    overused = [(w, c, round(c / total_content * 100, 2))
                for w, c in content_words.items()
                if c / total_content > 0.01]  # More than 1% of content words
    overused.sort(key=lambda x: x[1], reverse=True)

    # Detect repeated phrases (2-4 word ngrams)
    def get_ngrams(words: list[str], n: int) -> list[str]:
        return [' '.join(words[i:i+n]) for i in range(len(words) - n + 1)]

    bigrams = Counter(get_ngrams(words, 2))
    trigrams = Counter(get_ngrams(words, 3))

    # Filter to repeated phrases (more than 2 occurrences)
    repeated_bigrams = [(p, c) for p, c in bigrams.most_common(20)
                        if c > 2 and not all(w in stop_words for w in p.split())]
    repeated_trigrams = [(p, c) for p, c in trigrams.most_common(20)
                         if c > 2 and not all(w in stop_words for w in p.split())]

    return {
        "overused_words": overused[:15],
        "repeated_bigrams": repeated_bigrams[:10],
        "repeated_trigrams": repeated_trigrams[:10],
    }


def analyze_text(
    text: str, suppress_categories: set[str] | None = None
) -> StyleMetrics:
    """Run complete style analysis on text."""
    sentences = split_sentences(text)
    paragraphs = split_paragraphs(text)
    words = text.split()
    word_count = len(words)

    return StyleMetrics(
        sentence_metrics=analyze_sentence_metrics(sentences),
        dialogue_metrics=analyze_dialogue(text, word_count),
        vocabulary_metrics=analyze_vocabulary(text, word_count),
        paragraph_metrics=analyze_paragraphs(paragraphs),
        forbidden_patterns=detect_forbidden_patterns(text, suppress_categories),
        repetition_metrics=analyze_repetition(text),
        word_count=word_count,
        sentence_count=len(sentences),
    )


def format_markdown_report(metrics: StyleMetrics, source: str) -> str:
    """Format analysis as markdown report."""
    lines = [
        f"# Style Analysis: {source}",
        "",
        "## Source Info",
        f"- **Word count**: {metrics.word_count:,}",
        f"- **Sentence count**: {metrics.sentence_count:,}",
        "",
        "## Sentence Metrics",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Mean length | {metrics.sentence_metrics['mean_length']} words |",
        f"| Median length | {metrics.sentence_metrics['median_length']} words |",
        f"| Std deviation | {metrics.sentence_metrics['std_dev']} |",
        f"| Short sentences (<8 words) | {metrics.sentence_metrics['short_ratio']*100:.1f}% |",
        f"| Long sentences (>25 words) | {metrics.sentence_metrics['long_ratio']*100:.1f}% |",
        "",
        "## Dialogue Profile",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Dialogue ratio | {metrics.dialogue_metrics['dialogue_ratio']*100:.1f}% |",
        f"| Quote style | {metrics.dialogue_metrics['quote_style']['dominant']} quotes |",
        f"| Most common tag | \"said\" ({metrics.dialogue_metrics['tags']['said_percentage']}%) |",
        f"| Attribution style | {metrics.dialogue_metrics['attribution_style']['dominant']} |",
        "",
        "## Vocabulary Profile",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Unique word ratio | {metrics.vocabulary_metrics['unique_word_ratio']:.2f} |",
        f"| Rare word ratio | {metrics.vocabulary_metrics['rare_word_ratio']:.2f} |",
        f"| Adverb density | {metrics.vocabulary_metrics['adverb_density_per_1000']:.1f} per 1000 |",
        f"| AI signal density | {metrics.vocabulary_metrics['ai_signals']['density_per_1000']:.1f} per 1000 |",
        "",
        "## Paragraph Structure",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Mean paragraph length | {metrics.paragraph_metrics['mean_sentences']:.1f} sentences |",
        f"| Single-sentence paragraphs | {metrics.paragraph_metrics['single_sentence_ratio']*100:.1f}% |",
        f"| Dialogue paragraphs | {metrics.paragraph_metrics['dialogue_paragraph_ratio']*100:.1f}% |",
        "",
        "## Forbidden Patterns Found",
        f"- Filter words: {metrics.forbidden_patterns['totals']['filter_words']} occurrences",
        f"- Hedging: {metrics.forbidden_patterns['totals']['hedging']} occurrences",
        f"- Cliché categories: {metrics.forbidden_patterns['totals']['cliche_categories']} occurrences across {len(metrics.forbidden_patterns['cliche_categories'])} categories",
    ]

    # Per-category cliche breakdown
    if metrics.forbidden_patterns["cliche_categories"]:
        lines.extend([
            "",
            "## Cliche Categories Detected",
            "| Category | Count | Why It Matters |",
            "|----------|-------|----------------|",
        ])
        for cat_id, cat_data in sorted(
            metrics.forbidden_patterns["cliche_categories"].items(),
            key=lambda x: x[1]["count"],
            reverse=True,
        ):
            lines.append(
                f"| {cat_data['name']} | {cat_data['count']} | {cat_data['why']} |"
            )

        lines.extend(["", "### Top Examples"])
        for cat_id, cat_data in metrics.forbidden_patterns["cliche_categories"].items():
            examples = ", ".join(f'"{e}"' for e in cat_data["examples"][:5])
            lines.append(f"- **{cat_data['name']}**: {examples}")

    if metrics.forbidden_patterns.get("suppressed"):
        lines.extend([
            "",
            "## Suppressed Categories",
            "- " + ", ".join(metrics.forbidden_patterns["suppressed"]),
        ])

    if metrics.vocabulary_metrics['ai_signals']['words_found']:
        lines.extend([
            "",
            "## AI Signal Words Detected",
            "- " + ", ".join(metrics.vocabulary_metrics['ai_signals']['words_found'][:15]),
        ])

    if metrics.repetition_metrics['overused_words']:
        lines.extend([
            "",
            "## Overused Words",
            "| Word | Count | % of Content |",
            "|------|-------|--------------|",
        ])
        for word, count, pct in metrics.repetition_metrics['overused_words'][:10]:
            lines.append(f"| {word} | {count} | {pct}% |")

    lines.extend([
        "",
        "## Grep Patterns for Validation",
        "```bash",
        "# Filter words",
        'grep -E "(he saw|she heard|they felt)" *.md',
        "",
        "# Hedging",
        'grep -E "(seemed to|appeared to|felt like)" *.md',
        "",
        "# AI signals",
        'grep -iE "(delve|utilize|leverage|facilitate|palpable)" *.md',
        "```",
    ])

    return "\n".join(lines)


def parse_voice_suppressions(voice_path: str) -> set[str]:
    """Extract suppress_categories from a voice.md YAML block."""
    try:
        text = Path(voice_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    match = re.search(r"suppress_categories:\s*\[([^\]]*)\]", text)
    if match:
        items = match.group(1).strip()
        if items:
            return {item.strip() for item in items.split(",")}
    return set()


def main():
    parser = argparse.ArgumentParser(
        description="Analyze prose style for voice.md generation"
    )
    parser.add_argument("path", nargs="?", help="File or directory to analyze")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--suppress",
        nargs="*",
        default=None,
        help="Category IDs to suppress (e.g., shimmer_family shadow_worship)",
    )
    parser.add_argument(
        "--voice",
        type=str,
        default=None,
        help="Path to voice.md to read suppress_categories from",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List all available cliche category IDs and exit",
    )
    args = parser.parse_args()

    # List categories mode
    if args.list_categories:
        for cat in CLICHE_CATEGORIES:
            print(f"  {cat['id']:30s}  {cat['name']}")
        sys.exit(0)

    if not args.path:
        parser.error("path is required (unless using --list-categories)")

    path = Path(args.path)

    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    # Build suppress set from CLI flags and voice.md
    suppress = set(args.suppress) if args.suppress else None
    if args.voice:
        voice_suppress = parse_voice_suppressions(args.voice)
        if voice_suppress:
            suppress = (suppress or set()) | voice_suppress

    # Collect text
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        source = path.name
    elif path.is_dir():
        texts = []
        for f in path.glob("**/*.txt"):
            texts.append(f.read_text(encoding="utf-8", errors="replace"))
        for f in path.glob("**/*.md"):
            texts.append(f.read_text(encoding="utf-8", errors="replace"))
        text = "\n\n".join(texts)
        source = str(path)
    else:
        print(f"Error: {path} is not a file or directory", file=sys.stderr)
        sys.exit(1)

    if not text.strip():
        print("Error: No text content found", file=sys.stderr)
        sys.exit(1)

    metrics = analyze_text(text, suppress_categories=suppress)

    if args.json:
        output = {
            "source": source,
            "word_count": metrics.word_count,
            "sentence_count": metrics.sentence_count,
            "sentence_metrics": metrics.sentence_metrics,
            "dialogue_metrics": metrics.dialogue_metrics,
            "vocabulary_metrics": metrics.vocabulary_metrics,
            "paragraph_metrics": metrics.paragraph_metrics,
            "forbidden_patterns": metrics.forbidden_patterns,
            "repetition_metrics": metrics.repetition_metrics,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print(format_markdown_report(metrics, source))


if __name__ == "__main__":
    main()
