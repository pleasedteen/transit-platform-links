"""Deterministic offline fixtures, including the 200k-stop stress fixture.

The stress fixture has exactly 200,010 stop_time rows:

* 10,000 calls at ``OA:HUB_EAST`` plus 10,000 paired pressure calls;
* 10,000 calls at ``OB:HUB_WEST`` plus 10,000 paired pressure calls;
* 10 hand-authored cross-operator itinerary calls;
* 64 x 50 x 50 = 160,000 unrelated decoy calls on other lines.

Bulk hub service ends before the 22:00 query.  Only ten affected-line calls
remain at/after that time.  The decoy routes still have late service.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterator

DATE = "2026-11-25"


def _iso_clock(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def small_batch() -> dict:
    """A compact functional fixture used by the automatic checks."""
    return {
        "batch_id": "small-001",
        "service_date": DATE,
        "reason": "small functional fixture",
        "sites": [
            {"source": "SA", "stop_id": "O", "version": 1, "source_kind": "operator",
             "name": "清河路口", "lat": 31.1000, "lon": 121.1000,
             "accessible": True, "observed_at": f"{DATE}T08:00:00Z"},
            {"source": "SA", "stop_id": "H", "version": 1, "source_kind": "operator",
             "name": "中央公园东门", "lat": 31.1100, "lon": 121.1100,
             "accessible": True, "observed_at": f"{DATE}T08:00:00Z"},
            {"source": "SB", "stop_id": "H1", "version": 1, "source_kind": "operator",
             "name": "中央公园东", "lat": 31.1104, "lon": 121.1100,
             "accessible": None, "observed_at": f"{DATE}T08:00:00Z"},
            {"source": "SB", "stop_id": "H2", "version": 1, "source_kind": "operator",
             "name": "中央公园东站", "lat": 31.1109, "lon": 121.1100,
             "accessible": True, "observed_at": f"{DATE}T08:00:00Z"},
            {"source": "SB", "stop_id": "D", "version": 1, "source_kind": "operator",
             "name": "园东终点", "lat": 31.1120, "lon": 121.1120,
             "accessible": True, "observed_at": f"{DATE}T08:00:00Z"},
            {"source": "SP", "stop_id": "P1", "version": 1, "source_kind": "operator",
             "name": "和平里", "lat": 31.1200, "lon": 121.1200,
             "accessible": False, "observed_at": f"{DATE}T01:00:00Z"},
            {"source": "SP", "stop_id": "P2", "version": 1, "source_kind": "operator",
             "name": "和平里", "lat": 31.1201, "lon": 121.1200,
             "accessible": True, "observed_at": f"{DATE}T05:00:00Z"},
            {"source": "CENSUS", "stop_id": "C-P", "version": 1, "source_kind": "census",
             "name": "和平里", "lat": 31.1200, "lon": 121.1201,
             "accessible": False, "observed_at": f"{DATE}T00:00:00Z"},
        ],
        "trips": [
            {
                "source": "SA", "trip_id": "T1", "version": 1, "route_id": "R1",
                "headsign": "中央公园",
                "stop_times": [
                    {"seq": 1, "stop_id": "O", "departure": "08:00:00"},
                    {"seq": 2, "stop_id": "H", "arrival": "08:09:00", "departure": "08:10:00"},
                ],
            },
            {
                "source": "SB", "trip_id": "T2", "version": 1, "route_id": "R2",
                "headsign": "东站",
                "stop_times": [
                    {"seq": 1, "stop_id": "H1", "arrival": "08:12:30", "departure": "08:12:30"},
                    {"seq": 2, "stop_id": "H2", "arrival": "08:17:00", "departure": "08:17:30"},
                    {"seq": 3, "stop_id": "D", "arrival": "08:22:00", "departure": "08:22:00"},
                ],
            },
        ],
    }


def stress_manifest() -> dict:
    return {
        "total_stop_times": 200_010,
        "hub_east_stop_calls": 10_001,  # 10k pressure + one target call
        "hub_west_stop_calls": 10_001,
        "query_time": "22:00:00",
        "merged_arrival": "22:27:00",
        "split_arrival": "22:45:00",
        "confirm_pair": {
            "a": {"source": "OA", "stop_id": "HUB_EAST"},
            "b": {"source": "OB", "stop_id": "HUB_WEST"},
        },
        "query": {
            "from_source": "OA",
            "from_stop_id": "WEST_ORIGIN",
            "to_source": "OB",
            "to_stop_id": "EAST_DEST",
            "earliest_departure": "22:00:00",
        },
    }


def stress_core_batch() -> dict:
    sites = [
        {"source": "OA", "stop_id": "WEST_ORIGIN", "version": 1, "source_kind": "operator",
         "name": "西柳苑", "lat": 31.2000, "lon": 121.2000, "accessible": True,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OA", "stop_id": "MID_PASS", "version": 1, "source_kind": "operator",
         "name": "过境桥", "lat": 31.2100, "lon": 121.2100, "accessible": False,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OA", "stop_id": "HUB_EAST", "version": 1, "source_kind": "operator",
         "name": "中央枢纽东站台", "lat": 31.2200, "lon": 121.2200, "accessible": True,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OA", "stop_id": "XFER_A", "version": 1, "source_kind": "operator",
         "name": "博览街", "lat": 31.2300, "lon": 121.2300, "accessible": True,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OA", "stop_id": "PRESSURE_PA", "version": 1, "source_kind": "operator",
         "name": "东部压力车场", "lat": 31.1900, "lon": 121.1900, "accessible": None,
         "observed_at": f"{DATE}T05:00:00Z"},

        # About 335 m away and a different name core: not automatic evidence.
        {"source": "OB", "stop_id": "HUB_WEST", "version": 1, "source_kind": "operator",
         "name": "中央西场站", "lat": 31.2230, "lon": 121.2206, "accessible": True,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OB", "stop_id": "XFER_B", "version": 1, "source_kind": "operator",
         "name": "博览街", "lat": 31.2300, "lon": 121.2300, "accessible": True,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OB", "stop_id": "MID_NORTH", "version": 1, "source_kind": "operator",
         "name": "过境北", "lat": 31.2350, "lon": 121.2350, "accessible": False,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OB", "stop_id": "MID_SLOW", "version": 1, "source_kind": "operator",
         "name": "慢车巷", "lat": 31.2360, "lon": 121.2360, "accessible": False,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OB", "stop_id": "EAST_DEST", "version": 1, "source_kind": "operator",
         "name": "终点湾", "lat": 31.2400, "lon": 121.2400, "accessible": True,
         "observed_at": f"{DATE}T05:00:00Z"},
        {"source": "OB", "stop_id": "PRESSURE_PB", "version": 1, "source_kind": "operator",
         "name": "西部压力车场", "lat": 31.1800, "lon": 121.1800, "accessible": None,
         "observed_at": f"{DATE}T05:00:00Z"},
    ]

    trips = [
        {
            "source": "OA", "trip_id": "TARGET_A_1", "version": 1,
            "route_id": "A1", "headsign": "博览街",
            "stop_times": [
                {"seq": 1, "stop_id": "WEST_ORIGIN", "arrival": 79320, "departure": 79320},
                {"seq": 2, "stop_id": "MID_PASS", "arrival": 79560, "departure": 79620},
                {"seq": 3, "stop_id": "HUB_EAST", "arrival": 79800, "departure": 79830},
                {"seq": 4, "stop_id": "XFER_A", "arrival": 80400, "departure": 80430},
            ],
        },
        {
            "source": "OB", "trip_id": "TARGET_B_FAST", "version": 1,
            "route_id": "BFAST", "headsign": "终点湾快车",
            "stop_times": [
                {"seq": 1, "stop_id": "HUB_WEST", "arrival": 80040, "departure": 80040},
                {"seq": 2, "stop_id": "MID_NORTH", "arrival": 80280, "departure": 80280},
                {"seq": 3, "stop_id": "EAST_DEST", "arrival": 80820, "departure": 80820},
            ],
        },
        {
            "source": "OB", "trip_id": "TARGET_B_SLOW", "version": 1,
            "route_id": "BSLOW", "headsign": "终点湾慢车",
            "stop_times": [
                {"seq": 1, "stop_id": "XFER_B", "arrival": 80700, "departure": 80700},
                {"seq": 2, "stop_id": "MID_SLOW", "arrival": 81300, "departure": 81300},
                {"seq": 3, "stop_id": "EAST_DEST", "arrival": 81900, "departure": 81900},
            ],
        },
    ]
    return {
        "batch_id": "stress-core",
        "service_date": DATE,
        "reason": "deterministic stress core",
        "sites": sites,
        "trips": trips,
    }


def pressure_batches(chunk: int = 1000) -> Iterator[dict]:
    """Yield batches with 10k hub calls on each of two sides."""
    for operator, hub, yard, prefix in (
        ("OA", "HUB_EAST", "PRESSURE_PA", "APRESS"),
        ("OB", "HUB_WEST", "PRESSURE_PB", "BPRESS"),
    ):
        trips: list[dict] = []
        batch_index = 0
        for i in range(10_000):
            if operator == "OA":
                start = 5 * 3600 + i * 5
            else:
                start = 5 * 3600 + 30 + i * 5
            trips.append({
                "source": operator,
                "trip_id": f"{prefix}_{i:05d}",
                "version": 1,
                "route_id": "PRESSURE",
                "headsign": "pressure yard",
                "stop_times": [
                    {"seq": 1, "stop_id": hub, "arrival": start, "departure": start + 30},
                    {"seq": 2, "stop_id": yard, "arrival": start + 120, "departure": start + 150},
                ],
            })
            if len(trips) >= chunk:
                yield {
                    "batch_id": f"stress-pressure-{operator}-{batch_index:02d}",
                    "service_date": DATE,
                    "trips": trips,
                }
                trips = []
                batch_index += 1
        if trips:
            yield {
                "batch_id": f"stress-pressure-{operator}-{batch_index:02d}",
                "service_date": DATE,
                "trips": trips,
            }


def decoy_sites_batch() -> dict:
    sites = []
    for j in range(128):
        lat = 32.0 + (j // 16) * 0.004
        lon = 120.0 + (j % 16) * 0.004
        sites.append({
            "source": "DC",
            "stop_id": f"D{j:04d}",
            "version": 1,
            "source_kind": "operator",
            "name": f"远端干扰站 {j:04d}",
            "lat": lat,
            "lon": lon,
            "accessible": True,
            "observed_at": f"{DATE}T05:00:00Z",
        })
    return {"batch_id": "stress-decoy-sites", "service_date": DATE, "sites": sites}


def decoy_batches() -> Iterator[dict]:
    """64 routes * 50 trips * 50 stops = 160,000 stop times."""
    for route in range(64):
        stop_ids = [(route * 13 + k * 7) % 128 for k in range(50)]
        trips = []
        for trip_index in range(50):
            start = 5 * 3600 + trip_index * 21 * 60 + (route % 7) * 45
            stop_times = []
            for k, stop_number in enumerate(stop_ids):
                t = start + k * 60
                stop_times.append({
                    "seq": k + 1,
                    "stop_id": f"D{stop_number:04d}",
                    "arrival": t,
                    "departure": t + 20,
                })
            trips.append({
                "source": "DC",
                "trip_id": f"DR{route:02d}T{trip_index:03d}",
                "version": 1,
                "route_id": f"DR{route:02d}",
                "headsign": "decoy",
                "stop_times": stop_times,
            })
        yield {
            "batch_id": f"stress-decoy-route-{route:02d}",
            "service_date": DATE,
            "trips": trips,
        }


def stress_batches() -> Iterator[dict]:
    yield stress_core_batch()
    yield from pressure_batches()
    yield decoy_sites_batch()
    yield from decoy_batches()


def write_fixture_files(out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "small.json").write_text(
        json.dumps(small_batch(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "stress-manifest.json").write_text(
        json.dumps(stress_manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out / "stress-batches.jsonl").open("w", encoding="utf-8") as f:
        for batch in stress_batches():
            f.write(json.dumps(batch, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Write deterministic offline fixture files")
    parser.add_argument("--out", default="fixtures", help="output directory")
    args = parser.parse_args()
    write_fixture_files(args.out)
    print(f"wrote fixtures to {args.out}")
    print(json.dumps(stress_manifest(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
