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

# Cliché patterns
CLICHE_PATTERNS = [
    r"\blet out a breath\b",
    r"\bdidn't know (he|she|they) was holding\b",
    r"\bheart pounded\b",
    r"\bblood ran cold\b",
    r"\btime stood still\b",
    r"\bsent (a )?shivers?\b",
    r"\beyes widened\b",
    r"\bjaw dropped\b",
]


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

    # Split on sentence boundaries
    # Handles: periods, question marks, exclamation points
    # Preserves: Mr., Mrs., Dr., etc.
    abbrevs = r'(?<!\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|e\.g|i\.e))'
    pattern = abbrevs + r'[.!?]+(?=\s+[A-Z]|\s*$)'

    sentences = re.split(pattern, text)
    sentences = [s.strip() for s in sentences if s.strip()]

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


def detect_forbidden_patterns(text: str) -> dict:
    """Detect filter words, hedging, and clichés."""
    results = {
        "filter_words": [],
        "hedging": [],
        "cliches": [],
        "totals": {
            "filter_words": 0,
            "hedging": 0,
            "cliches": 0,
        }
    }

    # Filter words
    for pattern in FILTER_PATTERNS:
        matches = re.findall(pattern, text, re.I)
        if matches:
            results["filter_words"].extend(matches if isinstance(matches[0], str) else [m[0] for m in matches])
    results["totals"]["filter_words"] = len(results["filter_words"])

    # Hedging
    for pattern in HEDGE_PATTERNS:
        matches = re.findall(pattern, text, re.I)
        results["hedging"].extend(matches)
    results["totals"]["hedging"] = len(results["hedging"])

    # Clichés
    for pattern in CLICHE_PATTERNS:
        matches = re.findall(pattern, text, re.I)
        results["cliches"].extend(matches)
    results["totals"]["cliches"] = len(results["cliches"])

    # Limit examples in output
    results["filter_words"] = results["filter_words"][:20]
    results["hedging"] = results["hedging"][:20]
    results["cliches"] = results["cliches"][:20]

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


def analyze_text(text: str) -> StyleMetrics:
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
        forbidden_patterns=detect_forbidden_patterns(text),
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
        f"- Clichés: {metrics.forbidden_patterns['totals']['cliches']} occurrences",
    ]

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


def main():
    parser = argparse.ArgumentParser(description="Analyze prose style for voice.md generation")
    parser.add_argument("path", help="File or directory to analyze")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    path = Path(args.path)

    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    # Collect text
    if path.is_file():
        text = path.read_text(encoding='utf-8', errors='replace')
        source = path.name
    elif path.is_dir():
        texts = []
        for f in path.glob("**/*.txt"):
            texts.append(f.read_text(encoding='utf-8', errors='replace'))
        for f in path.glob("**/*.md"):
            texts.append(f.read_text(encoding='utf-8', errors='replace'))
        text = "\n\n".join(texts)
        source = str(path)
    else:
        print(f"Error: {path} is not a file or directory", file=sys.stderr)
        sys.exit(1)

    if not text.strip():
        print("Error: No text content found", file=sys.stderr)
        sys.exit(1)

    metrics = analyze_text(text)

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
