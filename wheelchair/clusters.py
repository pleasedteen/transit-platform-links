"""Dynamic station-cluster graph, accessibility conclusions and history."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .matching import (
    RULE_VERSION,
    canonical_pair,
    candidate_pairs,
    key_of,
    match_pair,
    pair_key_text,
    split_key,
)
from .observability import StopMeter


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cluster_id_for_site_pk(site_pk: int) -> str:
    return f"cl_{int(site_pk)}"


def _observation_sort(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return text


def _accessibility_conclusion(members: list[dict[str, Any]]) -> dict[str, Any]:
    facts = [m for m in members if m.get("accessible") in (0, 1)]

    def category_rank(m: dict[str, Any]) -> int:
        kind = (m.get("source_kind") or "other").lower()
        if kind in ("census", "survey"):
            return 0
        if kind == "operator":
            return 1
        return 2

    if not facts:
        return {
            "accessible": None,
            "status": "unknown",
            "basis": None,
            "selected": None,
            "sources": [],
        }
    best_rank = min(category_rank(m) for m in facts)
    same_kind = [f for f in facts if category_rank(f) == best_rank]
    same_kind.sort(
        key=lambda m: (
            _observation_sort(m.get("observed_at")),
            int(m.get("version") or 0),
            str(m.get("source") or ""),
            str(m.get("stop_id") or ""),
        ),
        reverse=True,
    )
    selected = same_kind[0]
    accessible = int(selected["accessible"])
    basis = "census" if best_rank == 0 else "operator" if best_rank == 1 else "other"
    # Stable two-level ordering: newest observation first, then priority class.
    sources = sorted(
        facts,
        key=lambda m: (
            _observation_sort(m.get("observed_at")),
            int(m.get("version") or 0),
            str(m.get("source") or ""),
            str(m.get("stop_id") or ""),
        ),
        reverse=True,
    )
    sources.sort(key=category_rank)
    return {
        "accessible": None if accessible is None else bool(accessible),
        "status": "accessible" if accessible == 1 else "inaccessible",
        "basis": basis,
        "selected": {
            "source": selected["source"],
            "stop_id": selected["stop_id"],
            "source_kind": selected.get("source_kind"),
            "accessible": bool(accessible),
            "observed_at": selected.get("observed_at"),
            "version": selected.get("version"),
        },
        "sources": [
            {
                "source": f["source"],
                "stop_id": f["stop_id"],
                "source_kind": f.get("source_kind"),
                "accessible": bool(f["accessible"]),
                "observed_at": f.get("observed_at"),
                "version": f.get("version"),
            }
            for f in sources
        ],
    }


class DSU:
    def __init__(self, keys: list[str]):
        self.parent = {k: k for k in keys}
        self.rank = {k: 0 for k in keys}

    def find(self, x: str) -> str:
        parent = self.parent[x]
        if parent != x:
            self.parent[x] = self.find(parent)
        return self.parent[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def rebuild_clusters(
    conn: sqlite3.Connection,
    meter: StopMeter | None = None,
    *,
    operation_type: str,
    reason: str,
    operation_ref: str | None = None,
    adjudication_id: int | None = None,
    cause: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the complete site graph and materialize a new snapshot.

    Caller must own the surrounding transaction.  This function only reads and
    writes site/graph tables; stress rulings therefore touch zero stop_time
    rows.
    """
    old_rows = conn.execute(
        "SELECT cluster_id, source, stop_id FROM cluster_member"
    ).fetchall()
    old_map = {(r["source"], r["stop_id"]): r["cluster_id"] for r in old_rows}
    old_sets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for (source, stop_id), cid in old_map.items():
        old_sets[cid].add((source, stop_id))

    site_rows = conn.execute("SELECT * FROM site_current").fetchall()
    sites = [dict(r) for r in site_rows]
    keys = [key_of(s["source"], s["stop_id"]) for s in sites]

    # Recompute and replace public automatic evidence.
    auto_edges: list[dict[str, Any]] = []
    for a, b in candidate_pairs(sites):
        evidence = match_pair(a, b)
        if not evidence["matched"]:
            continue
        ka, kb = canonical_pair(a["source"], a["stop_id"],
                                b["source"], b["stop_id"])
        a_source, a_id = split_key(ka)
        b_source, b_id = split_key(kb)
        edge = {
            "a_source": a_source,
            "a_stop_id": a_id,
            "b_source": b_source,
            "b_stop_id": b_id,
            **evidence,
        }
        auto_edges.append(edge)

    conn.execute("DELETE FROM same_stop_edge")
    conn.executemany(
        """
        INSERT INTO same_stop_edge(
          a_source,a_stop_id,b_source,b_stop_id,
          distance_m,name_score,rule_version)
        VALUES (:a_source,:a_stop_id,:b_source,:b_stop_id,
                :distance_m,:name_score,:rule_version)
        """,
        auto_edges,
    )

    # Active adjudications.  Iterating id order leaves the newest ruling for a
    # pair as its effective verdict.
    adj_rows = conn.execute(
        """
        SELECT * FROM adjudication
        WHERE revoked_at IS NULL
        ORDER BY id ASC
        """
    ).fetchall()
    effective: dict[str, sqlite3.Row] = {}
    for row in adj_rows:
        ka, kb = canonical_pair(row["a_source"], row["a_stop_id"],
                                row["b_source"], row["b_stop_id"])
        effective[pair_key_text(ka, kb)] = row

    # Build the graph in one controlled pass: automatic edges are included
    # unless the latest effective ruling for that pair is reject; explicit
    # confirm edges are then added even when they fail the automatic rule.
    dsu = DSU(keys)
    rejected_pairs = {
        pair_text
        for pair_text, row in effective.items()
        if row["verdict"] == "reject"
    }
    for edge in auto_edges:
        ka = key_of(edge["a_source"], edge["a_stop_id"])
        kb = key_of(edge["b_source"], edge["b_stop_id"])
        if pair_key_text(ka, kb) not in rejected_pairs:
            dsu.union(ka, kb)
    for pair_text, row in effective.items():
        if row["verdict"] == "confirm":
            left, right = pair_text.split("|", 1)
            dsu.union(left, right)

    groups_by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for site in sites:
        k = key_of(site["source"], site["stop_id"])
        groups_by_root[dsu.find(k)].append(site)

    new_map: dict[tuple[str, str], str] = {}
    new_sets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    cluster_rows: list[dict[str, Any]] = []
    member_rows: list[tuple[str, str, str]] = []
    conclusions: dict[str, dict[str, Any]] = {}

    for members in groups_by_root.values():
        min_pk = min(int(m["site_pk"]) for m in members)
        cid = cluster_id_for_site_pk(min_pk)
        conclusion = _accessibility_conclusion(members)
        conclusions[cid] = conclusion
        ordered = sorted(members, key=lambda m: (m["source"], m["stop_id"]))
        label = ordered[0]["name"] if ordered else cid
        selected = conclusion.get("selected") or {}
        cluster_rows.append({
            "cluster_id": cid,
            "label": label,
            "accessible": conclusion["accessible"],
            "status": conclusion["status"],
            "basis": conclusion.get("basis"),
            "selected_source": selected.get("source"),
            "selected_stop_id": selected.get("stop_id"),
            "selected_observed_at": selected.get("observed_at"),
            "selected_version": selected.get("version"),
            "member_count": len(members),
            "updated_at": now_iso(),
        })
        for m in members:
            ref = (m["source"], m["stop_id"])
            new_map[ref] = cid
            new_sets[cid].add(ref)
            member_rows.append((cid, m["source"], m["stop_id"]))

    conn.execute("DELETE FROM cluster_member")
    conn.execute("DELETE FROM cluster_current")
    conn.executemany(
        """
        INSERT INTO cluster_current(
          cluster_id,label,accessible,status,basis,
          selected_source,selected_stop_id,selected_observed_at,
          selected_version,member_count,updated_at)
        VALUES (
          :cluster_id,:label,:accessible,:status,:basis,
          :selected_source,:selected_stop_id,:selected_observed_at,
          :selected_version,:member_count,:updated_at)
        """,
        cluster_rows,
    )
    conn.executemany(
        "INSERT INTO cluster_member(cluster_id,source,stop_id) VALUES (?,?,?)",
        member_rows,
    )

    changes = _diff_clusters(old_map, old_sets, new_map, new_sets)
    event_id = None
    if changes or operation_type in ("adjudicate", "undo"):
        cursor = conn.execute(
            """
            INSERT INTO cluster_event(
              operation_type,operation_ref,adjudication_id,
              created_at,reason,cause_json)
            VALUES (?,?,?,?,?,?)
            """,
            (
                operation_type,
                operation_ref,
                adjudication_id,
                now_iso(),
                reason,
                json.dumps(cause or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        for change in changes:
            affected_ids = set()
            if change["type"] == "merge":
                affected_ids.add(change["cluster_id"])
                affected_ids.update(change.get("old_cluster_ids", []))
            elif change["type"] == "split":
                affected_ids.add(change["cluster_id"])
                affected_ids.update(change.get("new_cluster_ids", []))
            else:
                affected_ids.add(change.get("cluster_id"))
            for cid in sorted(x for x in affected_ids if x):
                conn.execute(
                    """
                    INSERT INTO cluster_change(
                      event_id,change_type,cluster_id,
                      old_cluster_ids_json,new_cluster_ids_json,detail_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        event_id,
                        change["type"],
                        cid,
                        json.dumps(change.get("old_cluster_ids", [])),
                        json.dumps(change.get("new_cluster_ids", [])),
                        json.dumps(change, ensure_ascii=False, sort_keys=True),
                    ),
                )

    return {
        "event_id": event_id,
        "rule_version": RULE_VERSION,
        "cluster_count": len(cluster_rows),
        "auto_edge_count": len(auto_edges),
        "changes": changes,
        "conclusions": conclusions,
    }


def _diff_clusters(
    old_map: dict[tuple[str, str], str],
    old_sets: dict[str, set[tuple[str, str]]],
    new_map: dict[tuple[str, str], str],
    new_sets: dict[str, set[tuple[str, str]]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    # New cluster absorbs two or more old clusters.
    for new_id, refs in sorted(new_sets.items()):
        old_ids = sorted({old_map[r] for r in refs if r in old_map})
        if len(old_ids) >= 2:
            changes.append({
                "type": "merge",
                "cluster_id": new_id,
                "old_cluster_ids": old_ids,
                "new_cluster_ids": [new_id],
                "member_count": len(refs),
            })
        elif not old_ids:
            changes.append({
                "type": "create",
                "cluster_id": new_id,
                "old_cluster_ids": [],
                "new_cluster_ids": [new_id],
                "member_count": len(refs),
            })

    # An old cluster is distributed across two or more new clusters.
    for old_id, refs in sorted(old_sets.items()):
        new_ids = sorted({new_map[r] for r in refs if r in new_map})
        if len(new_ids) >= 2:
            changes.append({
                "type": "split",
                "cluster_id": old_id,
                "old_cluster_ids": [old_id],
                "new_cluster_ids": new_ids,
                "member_count": len(refs),
            })

    # Record ordinary additions when an old cluster maps one-to-one.
    for old_id, refs in sorted(old_sets.items()):
        new_ids = {new_map[r] for r in refs if r in new_map}
        if len(new_ids) == 1:
            new_id = next(iter(new_ids))
            added = sorted([list(r) for r in new_sets[new_id] - refs])
            removed = sorted([list(r) for r in refs - new_sets[new_id]])
            if added or removed:
                changes.append({
                    "type": "membership_update",
                    "cluster_id": new_id,
                    "old_cluster_ids": [old_id],
                    "new_cluster_ids": [new_id],
                    "added": [{"source": s, "stop_id": i} for s, i in added],
                    "removed": [{"source": s, "stop_id": i} for s, i in removed],
                })
    return changes


def cluster_detail(conn: sqlite3.Connection, cluster_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM cluster_current WHERE cluster_id=?", (cluster_id,)
    ).fetchone()
    if not row:
        return None
    cluster = dict(row)
    members = [
        dict(r)
        for r in conn.execute(
            """
            SELECT s.* FROM cluster_member cm
            JOIN site_current s
              ON s.source=cm.source AND s.stop_id=cm.stop_id
            WHERE cm.cluster_id=?
            ORDER BY s.source,s.stop_id
            """,
            (cluster_id,),
        ).fetchall()
    ]
    conclusion = _accessibility_conclusion(members)
    cluster["accessible"] = conclusion["accessible"]
    cluster["status"] = conclusion["status"]
    cluster["conclusion"] = conclusion
    cluster["members"] = [
        {
            "source": m["source"],
            "stop_id": m["stop_id"],
            "name": m["name"],
            "lat": m["lat"],
            "lon": m["lon"],
            "version": m["version"],
            "source_kind": m["source_kind"],
            "accessible": None if m["accessible"] is None else bool(m["accessible"]),
            "observed_at": m["observed_at"],
        }
        for m in members
    ]
    return cluster


def cluster_for_ref(
    conn: sqlite3.Connection, source: str, stop_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT cluster_id FROM cluster_member WHERE source=? AND stop_id=?",
        (source, stop_id),
    ).fetchone()
    if not row:
        return None
    return cluster_detail(conn, row["cluster_id"])


def list_clusters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT cc.*, COUNT(cm.source) AS actual_member_count
        FROM cluster_current cc
        LEFT JOIN cluster_member cm ON cm.cluster_id=cc.cluster_id
        GROUP BY cc.cluster_id
        ORDER BY cc.cluster_id
        """
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["accessible"] = None if r["accessible"] is None else bool(r["accessible"])
        result.append(d)
    return result


def cluster_history(
    conn: sqlite3.Connection, cluster_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    if cluster_id:
        rows = conn.execute(
            """
            SELECT e.*, c.change_type, c.cluster_id AS affected_cluster_id,
                   c.old_cluster_ids_json, c.new_cluster_ids_json,
                   c.detail_json
            FROM cluster_change c
            JOIN cluster_event e ON e.id=c.event_id
            WHERE c.cluster_id=?
            ORDER BY e.id DESC, c.id DESC
            LIMIT ?
            """,
            (cluster_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.*, NULL AS change_type, NULL AS affected_cluster_id,
                   NULL AS old_cluster_ids_json, NULL AS new_cluster_ids_json,
                   NULL AS detail_json
            FROM cluster_event e
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    events: dict[int, dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        eid = d["id"]
        if eid not in events:
            events[eid] = {
                "event_id": eid,
                "operation_type": d["operation_type"],
                "operation_ref": d["operation_ref"],
                "adjudication_id": d["adjudication_id"],
                "created_at": d["created_at"],
                "reason": d["reason"],
                "cause": json.loads(d["cause_json"] or "{}"),
                "changes": [],
            }
        if d.get("change_type"):
            events[eid]["changes"].append(json.loads(d["detail_json"]))
    return list(events.values())
