"""Dispatcher direct-pair rulings and reverse-chronological undo."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .clusters import now_iso, rebuild_clusters
from .matching import canonical_pair, pair_key_text
from .observability import ApiError, StopMeter
from .transactions import write_transaction


def _ref(value: Any, prefix: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_PAIR", f"{prefix} must be an object")
    source = str(value.get("source") or "")
    stop_id = str(value.get("stop_id") or value.get("id") or "")
    if not source or not stop_id:
        raise ApiError(400, "INVALID_PAIR", f"{prefix} requires source and stop_id")
    return source, stop_id


def create_adjudication(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    meter: StopMeter | None = None,
) -> dict[str, Any]:
    meter = meter or StopMeter("adjudicate")
    a = _ref(payload.get("a") or payload.get("left"), "a")
    b = _ref(payload.get("b") or payload.get("right"), "b")
    if a == b:
        raise ApiError(400, "INVALID_PAIR", "cannot adjudicate a record against itself")
    verdict = str(payload.get("verdict") or "").lower()
    if verdict not in ("confirm", "reject"):
        raise ApiError(400, "INVALID_VERDICT", "verdict must be confirm or reject")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        reason = "调度员确认同站" if verdict == "confirm" else "调度员驳回同站关系"
    decided_by = payload.get("by") or payload.get("decided_by") or "dispatcher"

    with write_transaction(conn):
        ka, kb = canonical_pair(a[0], a[1], b[0], b[1])
        pair_text = pair_key_text(ka, kb)
        cur = conn.execute(
            """
            INSERT INTO adjudication(
              a_source,a_stop_id,b_source,b_stop_id,pair_key,
              verdict,reason,decided_by,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (a[0], a[1], b[0], b[1], pair_text, verdict, reason,
             str(decided_by), now_iso()),
        )
        adjudication_id = cur.lastrowid
        rebuild = rebuild_clusters(
            conn,
            meter,
            operation_type="adjudicate",
            operation_ref=f"adjudication-{adjudication_id}",
            adjudication_id=adjudication_id,
            reason=reason,
            cause={
                "adjudication_id": adjudication_id,
                "verdict": verdict,
                "pair": [
                    {"source": a[0], "stop_id": a[1]},
                    {"source": b[0], "stop_id": b[1]},
                ],
            },
        )
        row = conn.execute("SELECT * FROM adjudication WHERE id=?", (adjudication_id,)).fetchone()
        return {
            "adjudication": dict(row),
            "cluster_changes": rebuild["changes"],
            "cluster_count": rebuild["cluster_count"],
            "auto_edge_count": rebuild["auto_edge_count"],
            "processing": meter.report(),
        }


def undo_latest(
    conn: sqlite3.Connection,
    payload: dict[str, Any] | None = None,
    meter: StopMeter | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    meter = meter or StopMeter("undo")
    reason = str(payload.get("reason") or "按最新到最旧顺序撤销裁决")
    requested_id = payload.get("adjudication_id")

    with write_transaction(conn):
        latest = conn.execute(
            """
            SELECT * FROM adjudication
            WHERE revoked_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if latest is None:
            raise ApiError(409, "NO_ACTIVE_ADJUDICATION", "没有可撤销的有效裁决")
        if requested_id is not None and int(requested_id) != int(latest["id"]):
            raise ApiError(
                409,
                "UNDO_ORDER_VIOLATION",
                "只能从最新裁决开始向旧撤销；当前最新有效裁决 id 为 "
                f"{latest['id']}",
                {"latest_active_id": latest["id"]},
            )

        cur = conn.execute(
            """
            UPDATE adjudication
            SET revoked_at=?, revoke_reason=?
            WHERE id=? AND revoked_at IS NULL
            """,
            (now_iso(), reason, latest["id"]),
        )
        if cur.rowcount != 1:
            raise ApiError(409, "UNDO_FAILED", "裁决已被撤销或不存在")

        rebuild = rebuild_clusters(
            conn,
            meter,
            operation_type="undo",
            operation_ref=f"undo-{latest['id']}",
            adjudication_id=latest["id"],
            reason=reason,
            cause={
                "undone_adjudication_id": latest["id"],
                "undone_verdict": latest["verdict"],
                "pair_key": latest["pair_key"],
            },
        )
        row = conn.execute("SELECT * FROM adjudication WHERE id=?", (latest["id"],)).fetchone()
        return {
            "undone": dict(row),
            "cluster_changes": rebuild["changes"],
            "cluster_count": rebuild["cluster_count"],
            "processing": meter.report(),
        }


def list_adjudications(conn: sqlite3.Connection, include_revoked: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM adjudication"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]
