"""Sample-timeline EV-018 scoring for locally annotated VAD fixtures (#54).

All coordinates are half-open integer sample ranges. Fixture payloads carry
only annotations and metadata, not audio samples or transcripts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

RATE = 16000


@dataclass(frozen=True)
class FixtureMetrics:
    fixture_id: str
    category: str
    reference_samples: int
    recovered_samples: int
    recall: float | None
    missed_utterances: int
    onset_median_ms: float | None
    onset_p95_ms: float | None
    false_positive_samples: int
    false_positive_ratio: float | None
    onset_late_ms: tuple[float, ...] = ()


def _validate_ranges(items: Sequence[tuple[int, int]], total: int) -> None:
    last = -1
    for a, b in items:
        if (type(a) is not int or type(b) is not int or
                not 0 <= a < b <= total or a < last):
            raise ValueError("invalid annotation range")
        last = b


def _union(items: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    combined: list[tuple[int, int]] = []
    for a, b in sorted(items):
        if combined and a <= combined[-1][1]:
            combined[-1] = (combined[-1][0], max(b, combined[-1][1]))
        else:
            combined.append((a, b))
    return combined


def _intersection_count(left: Sequence[tuple[int, int]],
                        right: Sequence[tuple[int, int]]) -> int:
    total = i = j = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        total += max(0, min(b, d) - max(a, c))
        if b <= d:
            i += 1
        else:
            j += 1
    return total


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def score_fixture(fixture_id: str, category: str, *, total_samples: int,
                  reference: Sequence[tuple[int, int]],
                  predicted: Sequence[tuple[int, int]]) -> FixtureMetrics:
    if type(total_samples) is not int or total_samples < 0:
        raise ValueError("invalid total sample count")
    _validate_ranges(reference, total_samples)
    _validate_ranges(predicted, total_samples)
    r, p = _union(reference), _union(predicted)
    ref_samples = sum(b - a for a, b in r)
    recovered = _intersection_count(r, p)
    false_positive = sum(b - a for a, b in p) - recovered
    late: list[float] = []
    missed = 0
    for a, b in reference:
        overlap = next(((c, d) for c, d in p if c < b and d > a), None)
        if overlap is None:
            missed += 1
        else:
            late.append(max(0, overlap[0] - a) * 1000 / RATE)
    return FixtureMetrics(
        fixture_id, category, ref_samples, recovered,
        recovered / ref_samples if ref_samples else None,
        missed,
        _percentile(late, 0.5) if late else None,
        _percentile(late, 0.95) if late else None,
        false_positive,
        (false_positive / (total_samples - ref_samples)
         if total_samples > ref_samples else None),
        tuple(late),
    )


def aggregate_speech(fixtures: Sequence[FixtureMetrics]) -> dict:
    eligible = [x for x in fixtures if x.reference_samples > 0]
    denominator = sum(x.reference_samples for x in eligible)
    numerator = sum(x.recovered_samples for x in eligible)
    late = [value for metric in eligible for value in metric.onset_late_ms]
    median = _percentile(late, 0.5) if late else None
    p95 = _percentile(late, 0.95) if late else None
    return {
        "reference_samples": denominator,
        "recovered_samples": numerator,
        "speech_recall": numerator / denominator if denominator else None,
        "recall_pass": numerator / denominator >= 0.95 if denominator else False,
        "missed_utterances": sum(x.missed_utterances for x in eligible),
        "onset_median_ms": median,
        "onset_p95_ms": p95,
        "onset_pass": bool(late) and median <= 50 and p95 <= 200,
    }
