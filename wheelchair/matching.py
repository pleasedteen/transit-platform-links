"""Public, deterministic same-physical-stop matching rule.

Rule version: name-location-v1

Two source records are automatic same-stop evidence only when both hold:

* great-circle distance <= 200 m, and
* normalized name similarity >= 0.72.

When the normalized core names are exactly equal, the location limit is
relaxed to 300 m.  Name normalization uses Unicode NFKC, case folding,
punctuation removal, whitespace removal, and removal of a small public list
of generic station words.  Similarity is :class:`difflib.SequenceMatcher`
ratio on those normalized cores.

Connected records joined by any chain of such evidence form one station
cluster; direct confirm/reject rulings can add or remove an edge.
"""
from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

RULE_VERSION = "name-location-v1"
DISTANCE_LIMIT_M = 200.0
EXACT_NAME_DISTANCE_LIMIT_M = 300.0
NAME_SCORE_LIMIT = 0.72

GENERIC_WORDS = {
    "station", "stop", "platform", "bus", "terminal", "center", "centre",
    "east", "west", "south", "north",
    "公交站", "巴士站", "汽车站", "火车站", "地铁站", "站台", "车站",
    "总站", "枢纽站", "公交", "巴士",
    # Direction words are intentionally not stripped in the Chinese compact
    # form?  They are included here as isolated latin tokens only.  Chinese
    # suffix stripping below handles common suffixes without removing 东南西北.
}

LAT_DEG_M = 111_320.0


def normalize_name(name: str | None) -> str:
    if name is None:
        return ""
    text = unicodedata.normalize("NFKC", str(name)).casefold()
    # Replace all non word/CJK characters with spaces, then split.
    text = re.sub(r"[^\w一-鿿]+", " ", text, flags=re.UNICODE)
    raw_tokens = [t for t in text.split() if t]
    kept: list[str] = []
    for token in raw_tokens:
        if token in GENERIC_WORDS:
            continue
        # Strip common Chinese generic suffixes, but retain the meaningful
        # place/direction core such as 中央公园东.
        compact = token
        for suffix in ("公交枢纽站", "公交总站", "公交车站", "枢纽站",
                       "公交站", "巴士站", "汽车站", "火车站", "地铁站",
                       "站台", "车站", "总站", "站"):
            if compact.endswith(suffix) and len(compact) > len(suffix):
                compact = compact[: -len(suffix)]
                break
        if compact and compact not in GENERIC_WORDS:
            kept.append(compact)
    # For mixed latin/CJK input this gives a compact deterministic core.
    return "".join(kept).replace(" ", "")


def name_similarity(a: str | None, b: str | None) -> float:
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def match_pair(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    distance = haversine_m(float(a["lat"]), float(a["lon"]),
                           float(b["lat"]), float(b["lon"]))
    score = name_similarity(a.get("name"), b.get("name"))
    core_a = normalize_name(a.get("name"))
    core_b = normalize_name(b.get("name"))
    exact_core = core_a == core_b and bool(core_a)
    limit = EXACT_NAME_DISTANCE_LIMIT_M if exact_core else DISTANCE_LIMIT_M
    matched = distance <= limit and score >= NAME_SCORE_LIMIT
    return {
        "matched": matched,
        "distance_m": round(distance, 3),
        "name_score": round(score, 4),
        "normalized_a": core_a,
        "normalized_b": core_b,
        "distance_limit_m": limit,
        "name_score_limit": NAME_SCORE_LIMIT,
        "rule_version": RULE_VERSION,
    }


def key_of(source: str, stop_id: str) -> str:
    return f"{source}\x1f{stop_id}"


def split_key(key: str) -> tuple[str, str]:
    source, stop_id = key.split("\x1f", 1)
    return source, stop_id


def canonical_pair(a_source: str, a_id: str, b_source: str, b_id: str) -> tuple[str, str]:
    ka = key_of(a_source, a_id)
    kb = key_of(b_source, b_id)
    return (ka, kb) if ka <= kb else (kb, ka)


def pair_key_text(ka: str, kb: str) -> str:
    return f"{ka}|{kb}"


def candidate_pairs(sites: list[dict[str, Any]]) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield nearby pairs using a deterministic latitude/longitude grid.

    The grid is only a performance filter.  ``match_pair`` applies the public
    exact rule to every yielded pair, so correctness does not depend on grid
    boundaries.
    """
    cell_lat = 0.0025  # about 278 m north-south
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}

    def cell_for(site: dict[str, Any]) -> tuple[int, int]:
        lat = float(site["lat"])
        lon = float(site["lon"])
        cell_lon = cell_lat / max(0.2, math.cos(math.radians(lat)))
        return math.floor(lat / cell_lat), math.floor(lon / cell_lon)

    decorated = []
    for site in sites:
        cx, cy = cell_for(site)
        decorated.append((site, cx, cy))
        buckets.setdefault((cx, cy), []).append(site)

    seen: set[tuple[str, str]] = set()
    for site, cx, cy in decorated:
        # +/-2 cells is a conservative neighborhood for the 300 m limit.
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                for other in buckets.get((cx + dx, cy + dy), ()):  # type: ignore[union-attr]
                    if site is other:
                        continue
                    ka = key_of(site["source"], site["stop_id"])
                    kb = key_of(other["source"], other["stop_id"])
                    pair = (ka, kb) if ka <= kb else (kb, ka)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    if ka <= kb:
                        yield site, other
                    else:
                        yield other, site
