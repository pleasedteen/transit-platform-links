"""Atomic, idempotent batch import and version correction handling."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .clusters import now_iso, rebuild_clusters
from .observability import ApiError, StopMeter
from .timeutil import parse_clock
from .transactions import write_transaction


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) != 0 else 0
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1", "accessible", "pass"):
        return 1
    if text in ("false", "no", "n", "0", "inaccessible", "blocked"):
        return 0
    if text in ("unknown", "none", "null", ""):
        return None
    raise ApiError(400, "INVALID_ACCESSIBILITY", f"invalid accessible value: {value!r}")


def _source_kind(item: dict[str, Any], payload: dict[str, Any], default: str = "operator") -> str:
    kind = item.get("source_kind") or item.get("kind") or payload.get("source_kind")
    if kind in ("census", "survey"):
        return "census"
    if kind == "operator":
        return "operator"
    return kind or default


def import_batch(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    meter: StopMeter | None = None,
) -> dict[str, Any]:
    meter = meter or StopMeter("import")
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_BODY", "request body must be a JSON object")

    payload_hash = canonical_hash(payload)
    batch_id = str(payload.get("batch_id") or f"auto-{payload_hash[:16]}")

    with write_transaction(conn):
        old_batch = conn.execute(
            "SELECT payload_hash,summary_json FROM import_batch WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if old_batch:
            if old_batch["payload_hash"] != payload_hash:
                raise ApiError(
                    409,
                    "BATCH_ID_HASH_CONFLICT",
                    f"batch_id {batch_id!r} was already used with different content",
                )
            summary = json.loads(old_batch["summary_json"])
            summary["duplicate_batch"] = True
            return summary

        summary: dict[str, Any] = {
            "batch_id": batch_id,
            "duplicate_batch": False,
            "sites_received": 0,
            "sites_new_version": 0,
            "sites_duplicate_version": 0,
            "sites_older_ignored": 0,
            "trips_received": 0,
            "trips_new_version": 0,
            "trips_duplicate_version": 0,
            "trips_older_ignored": 0,
            "stop_times_inserted": 0,
            "stop_times_deleted_for_correction": 0,
            "cluster": None,
        }

        default_source = payload.get("source")
        source_name = payload.get("source_name")
        source_kind = payload.get("source_kind", "operator")
        if default_source:
            conn.execute(
                """
                INSERT INTO source_registry(source,kind,name)
                VALUES (?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                  kind=COALESCE(NULLIF(excluded.kind,'other'), source_registry.kind),
                  name=COALESCE(excluded.name, source_registry.name)
                """,
                (default_source, source_kind, source_name),
            )

        site_arrays = ("sites", "stops", "records", "facilities", "accessibility", "surveys")
        site_items: list[tuple[dict[str, Any], str]] = []
        for field in site_arrays:
            value = payload.get(field)
            if not value:
                continue
            if not isinstance(value, list):
                raise ApiError(400, "INVALID_FIELD", f"{field} must be a list")
            array_default_kind = "census" if field in ("facilities", "accessibility", "surveys") else "operator"
            for item in value:
                if not isinstance(item, dict):
                    raise ApiError(400, "INVALID_FIELD", f"{field} items must be objects")
                site_items.append((item, array_default_kind))

        sites_changed = False
        for item, array_default_kind in site_items:
            summary["sites_received"] += 1
            source = str(item.get("source") or default_source or "")
            stop_id = str(item.get("stop_id") or item.get("id") or item.get("code") or "")
            if not source or not stop_id:
                raise ApiError(400, "MISSING_SOURCE_ID", "site requires source and stop_id/id/code")
            version = int(item.get("version", 1))
            name = item.get("name")
            if name is None or "lat" not in item or "lon" not in item:
                raise ApiError(400, "MISSING_SITE_FIELDS",
                               f"site {source}:{stop_id} requires name, lat, lon")
            lat = float(item["lat"])
            lon = float(item["lon"])
            kind = _source_kind(item, payload, array_default_kind)
            accessible = parse_bool(item.get("accessible", item.get("wheelchair_accessible")))
            observed_at = item.get("observed_at") or item.get("surveyed_at")
            record_hash = canonical_hash(item)

            conn.execute(
                "INSERT INTO source_registry(source,kind) VALUES (?,?) ON CONFLICT(source) DO NOTHING",
                (source, kind if kind in ("operator", "census", "survey", "other") else "other"),
            )
            conn.execute(
                "INSERT INTO site(source,stop_id) VALUES (?,?) ON CONFLICT(source,stop_id) DO NOTHING",
                (source, stop_id),
            )
            site_pk = conn.execute(
                "SELECT id FROM site WHERE source=? AND stop_id=?", (source, stop_id)
            ).fetchone()[0]

            existing_version = conn.execute(
                "SELECT payload_hash FROM site_version WHERE source=? AND stop_id=? AND version=?",
                (source, stop_id, version),
            ).fetchone()
            if existing_version:
                if existing_version["payload_hash"] != record_hash:
                    raise ApiError(
                        409,
                        "SAME_VERSION_CONTENT_CONFLICT",
                        f"{source}:{stop_id} version {version} already exists with different content",
                    )
                summary["sites_duplicate_version"] += 1
                continue

            conn.execute(
                """
                INSERT INTO site_version(
                  source,stop_id,version,source_kind,name,lat,lon,
                  accessible,observed_at,payload_hash,batch_id,imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (source, stop_id, version, kind, name, lat, lon,
                 accessible, observed_at, record_hash, batch_id, now_iso()),
            )
            summary["sites_new_version"] += 1

            current = conn.execute(
                "SELECT version FROM site_current WHERE source=? AND stop_id=?",
                (source, stop_id),
            ).fetchone()
            if current is None or version >= int(current["version"]):
                conn.execute(
                    """
                    INSERT INTO site_current(
                      site_pk,source,stop_id,version,source_kind,name,lat,lon,
                      accessible,observed_at,payload_hash,batch_id,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source,stop_id) DO UPDATE SET
                      site_pk=excluded.site_pk,
                      version=excluded.version,
                      source_kind=excluded.source_kind,
                      name=excluded.name,lat=excluded.lat,lon=excluded.lon,
                      accessible=excluded.accessible,
                      observed_at=excluded.observed_at,
                      payload_hash=excluded.payload_hash,
                      batch_id=excluded.batch_id,
                      updated_at=excluded.updated_at
                    """,
                    (site_pk, source, stop_id, version, kind, name, lat, lon,
                     accessible, observed_at, record_hash, batch_id, now_iso()),
                )
                summary["sites_new_version"] += 0  # already counted as stored version
                sites_changed = True
            else:
                summary["sites_older_ignored"] += 1

        trips = payload.get("trips") or []
        if not isinstance(trips, list):
            raise ApiError(400, "INVALID_FIELD", "trips must be a list")

        for trip in trips:
            summary["trips_received"] += 1
            source = str(trip.get("source") or default_source or "")
            trip_code = str(trip.get("trip_id") or trip.get("id") or trip.get("trip_code") or "")
            if not source or not trip_code:
                raise ApiError(400, "MISSING_TRIP_ID", "trip requires source and trip_id/id")
            version = int(trip.get("version", 1))
            service_date = str(trip.get("service_date") or payload.get("service_date") or "offline-day")
            route_code = trip.get("route_id") or trip.get("route_code")
            headsign = trip.get("headsign")
            record_hash = canonical_hash(trip)

            existing_version = conn.execute(
                "SELECT payload_hash FROM trip_version WHERE source=? AND trip_code=? AND version=?",
                (source, trip_code, version),
            ).fetchone()
            if existing_version:
                if existing_version["payload_hash"] != record_hash:
                    raise ApiError(
                        409,
                        "SAME_TRIP_VERSION_CONTENT_CONFLICT",
                        f"{source}:{trip_code} version {version} exists with different content",
                    )
                summary["trips_duplicate_version"] += 1
                continue

            conn.execute(
                """
                INSERT INTO trip_version(
                  source,trip_code,version,service_date,route_code,headsign,
                  payload_hash,batch_id,imported_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (source, trip_code, version, service_date, route_code, headsign,
                 record_hash, batch_id, now_iso()),
            )

            current = conn.execute(
                "SELECT id,version FROM trip WHERE source=? AND trip_code=?",
                (source, trip_code),
            ).fetchone()
            if current is None or version >= int(current["version"]):
                cursor = conn.execute(
                    """
                    INSERT INTO trip(source,trip_code,version,service_date,
                      route_code,headsign,payload_hash,batch_id,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source,trip_code) DO UPDATE SET
                      version=excluded.version,
                      service_date=excluded.service_date,
                      route_code=excluded.route_code,
                      headsign=excluded.headsign,
                      payload_hash=excluded.payload_hash,
                      batch_id=excluded.batch_id,
                      updated_at=excluded.updated_at
                    """,
                    (source, trip_code, version, service_date, route_code,
                     headsign, record_hash, batch_id, now_iso()),
                )
                trip_pk = conn.execute(
                    "SELECT id FROM trip WHERE source=? AND trip_code=?",
                    (source, trip_code),
                ).fetchone()["id"]
                if current is not None:
                    # ON CONFLICT cursor lastrowid may not be reliable.
                    trip_pk = current["id"]
                summary["trips_new_version"] += 1

                stop_rows = trip.get("stop_times")
                if stop_rows is None:
                    stop_rows = trip.get("stops")
                if stop_rows is not None:
                    if not isinstance(stop_rows, list):
                        raise ApiError(400, "INVALID_FIELD", "trip.stop_times must be a list")
                    deleted = conn.execute(
                        "DELETE FROM stop_time WHERE trip_pk=?", (trip_pk,)
                    ).rowcount
                    meter.add_deleted(deleted if deleted and deleted > 0 else 0)
                    normalized = []
                    for idx, st in enumerate(stop_rows, start=1):
                        if not isinstance(st, dict):
                            raise ApiError(400, "INVALID_FIELD", "stop_time must be an object")
                        seq = int(st.get("seq") or st.get("sequence") or idx)
                        stop_id = str(st.get("stop_id") or st.get("id") or st.get("code") or "")
                        if not stop_id:
                            raise ApiError(400, "MISSING_STOP_ID", "stop_time requires stop_id")
                        departure_raw = st.get("departure", st.get("departure_time", st.get("time")))
                        arrival_raw = st.get("arrival", st.get("arrival_time"))
                        if departure_raw is None:
                            departure_raw = arrival_raw
                        if arrival_raw is None:
                            arrival_raw = departure_raw
                        if departure_raw is None:
                            raise ApiError(400, "MISSING_TIME", "stop_time requires departure/time")
                        try:
                            arrival_s = parse_clock(arrival_raw)
                            departure_s = parse_clock(departure_raw)
                        except ValueError as exc:
                            raise ApiError(
                                400,
                                "INVALID_TIME",
                                f"invalid time in {source}:{trip_code} seq {seq}: {exc}",
                            ) from exc
                        if arrival_s > departure_s:
                            # Dwell cannot be negative; normalize obvious typo.
                            arrival_s, departure_s = departure_s, arrival_s
                        stop_source = str(st.get("stop_source") or st.get("source") or source)
                        normalized.append((
                            trip_pk, source, trip_code, seq,
                            stop_source, stop_id, arrival_s, departure_s,
                        ))
                        meter.add_generated(1)
                    conn.executemany(
                        """
                        INSERT INTO stop_time(
                          trip_pk,source,trip_code,seq,stop_source,stop_id,
                          arrival_s,departure_s)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        normalized,
                    )
                    meter.add_inserted(len(normalized))
                    summary["stop_times_inserted"] += len(normalized)
            else:
                summary["trips_older_ignored"] += 1

        if sites_changed:
            rebuild_result = rebuild_clusters(
                conn,
                meter,
                operation_type="import",
                operation_ref=batch_id,
                reason=str(payload.get("reason") or f"导入批次 {batch_id}"),
                cause={"batch_id": batch_id, "source": default_source},
            )
            summary["cluster"] = {
                key: rebuild_result[key]
                for key in ("cluster_count", "auto_edge_count", "changes", "event_id")
            }

        conn.execute(
            "INSERT INTO import_batch(batch_id,payload_hash,summary_json,created_at) VALUES (?,?,?,?)",
            (batch_id, payload_hash, json.dumps(summary, ensure_ascii=False), now_iso()),
        )
        summary["processing"] = meter.report()
        return summary
