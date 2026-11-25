"""Earliest-arrival accessible itinerary search."""
from __future__ import annotations

import heapq
import sqlite3
from typing import Any

from .observability import ApiError, StopMeter
from .timeutil import format_clock, parse_clock

TRANSFER_SECONDS = 180
INF = 10**18


def _departures_at(
    conn: sqlite3.Connection,
    meter: StopMeter,
    source: str,
    stop_id: str,
    min_departure_s: int,
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT st.id AS stop_time_id, st.trip_pk, st.seq,
               st.arrival_s, st.departure_s,
               st.stop_source, st.stop_id,
               t.source AS trip_source, t.trip_code, t.route_code, t.headsign
        FROM stop_time st
        JOIN trip t ON t.id = st.trip_pk
        WHERE st.stop_source=?
          AND st.stop_id=?
          AND st.departure_s >= ?
        ORDER BY st.departure_s ASC, st.seq ASC
        """,
        (source, stop_id, int(min_departure_s)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    meter.sql_statements += 1
    meter.add_read_rows(len(rows))
    return rows


def _trip_suffix(
    conn: sqlite3.Connection, meter: StopMeter, trip_pk: int, after_seq: int
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT seq, stop_source, stop_id, arrival_s, departure_s
        FROM stop_time
        WHERE trip_pk=? AND seq > ?
        ORDER BY seq ASC
        """,
        (trip_pk, after_seq),
    )
    rows = [dict(r) for r in cur.fetchall()]
    meter.sql_statements += 1
    meter.add_read_rows(len(rows))
    return rows


def _trip_segment(
    conn: sqlite3.Connection, meter: StopMeter, trip_pk: int, from_seq: int, to_seq: int
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT seq, stop_source, stop_id, arrival_s, departure_s
        FROM stop_time
        WHERE trip_pk=? AND seq >= ? AND seq <= ?
        ORDER BY seq ASC
        """,
        (trip_pk, from_seq, to_seq),
    )
    rows = [dict(r) for r in cur.fetchall()]
    meter.sql_statements += 1
    meter.add_read_rows(len(rows))
    return rows


def _ref(source: str, stop_id: str) -> tuple[str, str]:
    return str(source), str(stop_id)


def plan_itinerary(
    conn: sqlite3.Connection,
    *,
    from_source: str,
    from_stop_id: str,
    to_source: str,
    to_stop_id: str,
    earliest_departure: Any = "00:00:00",
    meter: StopMeter | None = None,
) -> dict[str, Any]:
    meter = meter or StopMeter("itinerary")
    t0 = parse_clock(earliest_departure)
    origin_ref = _ref(from_source, from_stop_id)
    dest_ref = _ref(to_source, to_stop_id)

    origin_site = conn.execute(
        "SELECT * FROM site_current WHERE source=? AND stop_id=?", origin_ref
    ).fetchone()
    if not origin_site:
        raise ApiError(404, "ORIGIN_NOT_FOUND", f"unknown source stop {origin_ref[0]}:{origin_ref[1]}")
    dest_site = conn.execute(
        "SELECT * FROM site_current WHERE source=? AND stop_id=?", dest_ref
    ).fetchone()
    if not dest_site:
        raise ApiError(404, "DESTINATION_NOT_FOUND", f"unknown source stop {dest_ref[0]}:{dest_ref[1]}")

    member_rows = conn.execute(
        "SELECT cluster_id, source, stop_id FROM cluster_member"
    ).fetchall()
    if not member_rows:
        return _infeasible("NO_CLUSTERS", "站群尚未计算", meter, t0)

    members_by_cluster: dict[str, list[tuple[str, str]]] = {}
    cluster_of_ref: dict[tuple[str, str], str] = {}
    for r in member_rows:
        ref = (r["source"], r["stop_id"])
        members_by_cluster.setdefault(r["cluster_id"], []).append(ref)
        cluster_of_ref[ref] = r["cluster_id"]

    origin_cid = cluster_of_ref.get(origin_ref)
    dest_cid = cluster_of_ref.get(dest_ref)
    if not origin_cid or not dest_cid:
        return _infeasible("STOP_WITHOUT_CLUSTER", "来源站台尚未归入站群", meter, t0)

    cluster_rows = conn.execute("SELECT cluster_id,accessible,status,basis FROM cluster_current").fetchall()
    accessible = {r["cluster_id"]: r["accessible"] == 1 for r in cluster_rows}
    cluster_info = {
        r["cluster_id"]: {
            **dict(r),
            "accessible": None if r["accessible"] is None else bool(r["accessible"]),
        }
        for r in cluster_rows
    }

    if not accessible.get(origin_cid):
        return _infeasible(
            "ORIGIN_NOT_ACCESSIBLE",
            "起点站群没有设施普查或运营商确认的无障碍通行结论",
            meter, t0, origin_cid, dest_cid, cluster_info,
        )
    if not accessible.get(dest_cid):
        return _infeasible(
            "DESTINATION_NOT_ACCESSIBLE",
            "终点站群没有设施普查或运营商确认的无障碍通行结论",
            meter, t0, origin_cid, dest_cid, cluster_info,
        )

    site_name_rows = conn.execute("SELECT source,stop_id,name FROM site_current").fetchall()
    names = {(r["source"], r["stop_id"]): r["name"] for r in site_name_rows}

    best: dict[str, int] = {origin_cid: t0}
    predecessor: dict[str, dict[str, Any]] = {}
    heap: list[tuple[int, int, str, tuple[str, str] | None]] = []
    serial = 0

    # Exact destination-record arrival.  Reaching another member of the
    # destination cluster still leaves the standard three-minute interchange
    # to the requested source platform.  Evaluate every alight row against this
    # bound so a later arrival directly at the requested stop is not discarded
    # by an earlier cluster-level label.
    best_final: int | None = None
    final_pred: dict[str, Any] | None = None
    if origin_cid == dest_cid:
        if origin_ref == dest_ref:
            best_final = t0
        else:
            walk_arrival = t0 + TRANSFER_SECONDS
            best_final = walk_arrival
            final_pred = {
                "type": "walk",
                "from_cluster_id": origin_cid,
                "to_cluster_id": dest_cid,
                "from_ref": origin_ref,
                "to_ref": dest_ref,
                "departure_s": t0,
                "arrival_s": walk_arrival,
            }

    serial += 1
    heapq.heappush(heap, (t0, serial, origin_cid, origin_ref))
    settled: set[str] = set()
    boardings_considered = 0
    trip_scans = 0
    clusters_expanded = 0

    while heap:
        current_time, _, cid, initial_ref = heapq.heappop(heap)
        if best_final is not None and current_time >= best_final:
            break
        if cid in settled:
            continue
        if current_time > best.get(cid, INF):
            continue
        settled.add(cid)
        if cid == dest_cid:
            # The exact destination candidate was evaluated when its alight
            # row was scanned.  Do not expand departures from the destination,
            # but continue processing equal-time competing labels.
            continue
        clusters_expanded += 1

        for ref in members_by_cluster.get(cid, []):
            if initial_ref is not None:
                min_departure = t0 if ref == initial_ref else t0 + TRANSFER_SECONDS
            else:
                min_departure = current_time + TRANSFER_SECONDS

            boards = _departures_at(conn, meter, ref[0], ref[1], min_departure)
            for board in boards:
                boardings_considered += 1
                # No suffix can arrive before this trip departs.
                if best_final is not None and board["departure_s"] >= best_final:
                    continue
                trip_scans += 1
                suffix = _trip_suffix(conn, meter, int(board["trip_pk"]), int(board["seq"]))
                for alight in suffix:
                    to_ref = (alight["stop_source"], alight["stop_id"])
                    to_cid = cluster_of_ref.get(to_ref)
                    if not to_cid or not accessible.get(to_cid):
                        # Passengers may remain onboard through an inaccessible
                        # intermediate stop; we simply do not label/alight here.
                        continue
                    arrival_s = int(alight["arrival_s"])
                    if arrival_s < int(board["departure_s"]):
                        continue

                    leg_pred = {
                        "type": "transit",
                        "from_cluster_id": cid,
                        "to_cluster_id": to_cid,
                        "board_ref": ref,
                        "alight_ref": to_ref,
                        "board": board,
                        "alight": alight,
                    }

                    if to_cid == dest_cid:
                        final_arrival = (
                            arrival_s if to_ref == dest_ref else arrival_s + TRANSFER_SECONDS
                        )
                        if final_arrival < (best_final if best_final is not None else INF):
                            best_final = final_arrival
                            final_pred = leg_pred

                    if arrival_s < best.get(to_cid, INF):
                        best[to_cid] = arrival_s
                        predecessor[to_cid] = leg_pred
                        serial += 1
                        heapq.heappush(heap, (arrival_s, serial, to_cid, None))

    if best_final is None:
        result = _infeasible(
            "NO_FEASIBLE_ITINERARY",
            "当天没有满足无障碍站群和三分钟换乘要求的行程",
            meter, t0, origin_cid, dest_cid, cluster_info,
        )
        result["search_stats"] = {
            "clusters_expanded": clusters_expanded,
            "boardings_considered": boardings_considered,
            "trip_suffix_scans": trip_scans,
            "clusters_settled": len(settled),
        }
        return result

    # Reconstruct predecessor chain backwards from the exact final candidate.
    chain: list[dict[str, Any]] = []
    if final_pred is not None:
        chain.append(final_pred)
        cursor = final_pred["from_cluster_id"]
        guard = 0
        while cursor != origin_cid:
            pred = predecessor.get(cursor)
            if not pred:
                return _infeasible("RECONSTRUCTION_FAILED", "无法重建行程", meter, t0,
                                   origin_cid, dest_cid, cluster_info)
            chain.append(pred)
            cursor = pred["from_cluster_id"]
            guard += 1
            if guard > 1000:
                raise ApiError(500, "ITINERARY_CYCLE", "itinerary reconstruction exceeded limit")
        chain.reverse()

    legs: list[dict[str, Any]] = []
    transfers: list[dict[str, Any]] = []
    for index, pred in enumerate(chain):
        if pred["type"] == "walk":
            legs.append({
                "type": "walk_within_cluster",
                "cluster_id": pred["to_cluster_id"],
                "from": _stop_descriptor(pred["from_ref"], names, cluster_of_ref),
                "to": _stop_descriptor(pred["to_ref"], names, cluster_of_ref),
                "departure_time": format_clock(pred["departure_s"]),
                "arrival_time": format_clock(pred["arrival_s"]),
                "duration_seconds": pred["arrival_s"] - pred["departure_s"],
                "minimum_allowed_seconds": TRANSFER_SECONDS,
            })
            continue

        board = pred["board"]
        alight = pred["alight"]
        segment = _trip_segment(
            conn, meter, int(board["trip_pk"]), int(board["seq"]), int(alight["seq"])
        )
        intermediate = []
        for st in segment:
            if int(st["seq"]) == int(board["seq"]) or int(st["seq"]) == int(alight["seq"]):
                continue
            ref = (st["stop_source"], st["stop_id"])
            cid = cluster_of_ref.get(ref)
            intermediate.append({
                **_stop_descriptor(ref, names, cluster_of_ref),
                "seq": st["seq"],
                "arrival_time": format_clock(st["arrival_s"]),
                "departure_time": format_clock(st["departure_s"]),
                "cluster_accessible": bool(accessible.get(cid)) if cid else None,
                "note": "乘车经过，不下车" if cid and not accessible.get(cid) else None,
            })

        board_ref = pred["board_ref"]
        alight_ref = pred["alight_ref"]
        legs.append({
            "type": "transit",
            "leg_index": index + 1,
            "source": board.get("trip_source"),
            "trip_id": board.get("trip_code"),
            "route_id": board.get("route_code"),
            "headsign": board.get("headsign"),
            "from_cluster_id": pred["from_cluster_id"],
            "to_cluster_id": pred["to_cluster_id"],
            "board": {
                **_stop_descriptor(board_ref, names, cluster_of_ref),
                "seq": board["seq"],
                "departure_time": format_clock(board["departure_s"]),
            },
            "alight": {
                **_stop_descriptor(alight_ref, names, cluster_of_ref),
                "seq": alight["seq"],
                "arrival_time": format_clock(alight["arrival_s"]),
            },
            "duration_seconds": int(alight["arrival_s"]) - int(board["departure_s"]),
            "intermediate_count": len(intermediate),
            "intermediate_stops": intermediate,
        })

    if final_pred and final_pred.get("type") == "transit" and final_pred["alight_ref"] != dest_ref:
        walk_from = final_pred["alight_ref"]
        walk_start = int(final_pred["alight"]["arrival_s"])
        legs.append({
            "type": "walk_within_cluster",
            "cluster_id": dest_cid,
            "from": _stop_descriptor(walk_from, names, cluster_of_ref),
            "to": _stop_descriptor(dest_ref, names, cluster_of_ref),
            "departure_time": format_clock(walk_start),
            "arrival_time": format_clock(best_final),
            "duration_seconds": int(best_final) - walk_start,
            "minimum_allowed_seconds": TRANSFER_SECONDS,
        })

    for i in range(1, len(legs)):
        prev = legs[i - 1]
        cur_leg = legs[i]
        if prev["type"] != "transit" or cur_leg["type"] != "transit":
            continue
        prev_arr = parse_clock(prev["alight"]["arrival_time"])
        next_dep = parse_clock(cur_leg["board"]["departure_time"])
        transfer_seconds = next_dep - prev_arr
        transfers.append({
            "transfer_index": i,
            "cluster_id": prev["to_cluster_id"],
            "from_stop": prev["alight"],
            "to_stop": cur_leg["board"],
            "arrival_time": prev["alight"]["arrival_time"],
            "departure_time": cur_leg["board"]["departure_time"],
            "transfer_seconds": transfer_seconds,
            "minimum_seconds": TRANSFER_SECONDS,
            "valid": transfer_seconds >= TRANSFER_SECONDS,
            "same_trip_continuation": (
                prev.get("trip_id") == cur_leg.get("trip_id")
                and prev.get("source") == cur_leg.get("source")
            ),
        })

    first_dep = _first_departure_seconds(legs, t0)
    last_arr = _last_arrival_seconds(legs, t0)
    result = {
        "feasible": True,
        "origin": {
            **_stop_descriptor(origin_ref, names, cluster_of_ref),
            "cluster_id": origin_cid,
            "accessibility": cluster_info.get(origin_cid),
        },
        "destination": {
            **_stop_descriptor(dest_ref, names, cluster_of_ref),
            "cluster_id": dest_cid,
            "accessibility": cluster_info.get(dest_cid),
        },
        "earliest_requested_departure": format_clock(t0),
        "departure_time": format_clock(first_dep),
        "arrival_time": format_clock(last_arr),
        "total_travel_seconds": max(0, last_arr - first_dep),
        "transfer_count": len(transfers),
        "minimum_transfer_seconds": TRANSFER_SECONDS,
        "legs": legs,
        "transfers": transfers,
        "search_stats": {
            "clusters_expanded": clusters_expanded,
            "clusters_settled": len(settled),
            "boardings_considered": boardings_considered,
            "trip_suffix_scans": trip_scans,
        },
        "stats": meter.report(),
    }
    return result


def _first_departure_seconds(legs: list[dict[str, Any]], fallback: int) -> int:
    if not legs:
        return fallback
    first = legs[0]
    if first["type"] == "transit":
        return parse_clock(first["board"]["departure_time"])
    return parse_clock(first["departure_time"])


def _last_arrival_seconds(legs: list[dict[str, Any]], fallback: int) -> int:
    if not legs:
        return fallback
    last = legs[-1]
    if last["type"] == "transit":
        return parse_clock(last["alight"]["arrival_time"])
    return parse_clock(last["arrival_time"])


def _stop_descriptor(
    ref: tuple[str, str],
    names: dict[tuple[str, str], str],
    cluster_of_ref: dict[tuple[str, str], str],
) -> dict[str, Any]:
    return {
        "source": ref[0],
        "stop_id": ref[1],
        "name": names.get(ref),
        "cluster_id": cluster_of_ref.get(ref),
    }


def _infeasible(
    reason: str,
    message: str,
    meter: StopMeter,
    t0: int,
    origin_cid: str | None = None,
    dest_cid: str | None = None,
    cluster_info: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "feasible": False,
        "reason": reason,
        "message": message,
        "earliest_requested_departure": format_clock(t0),
        "origin_cluster_id": origin_cid,
        "destination_cluster_id": dest_cid,
        "cluster_accessibility": {
            "origin": (cluster_info or {}).get(origin_cid) if origin_cid else None,
            "destination": (cluster_info or {}).get(dest_cid) if dest_cid else None,
        },
        "stats": meter.report(),
    }
