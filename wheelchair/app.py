"""Standard-library HTTP interface for the wheelchair transfer service."""
from __future__ import annotations

import argparse
import functools
import json
import sqlite3
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .adjudicate import create_adjudication, list_adjudications, undo_latest
from .clusters import cluster_detail, cluster_history, list_clusters
from .db import connect, init_db
from .importer import import_batch
from .matching import (
    DISTANCE_LIMIT_M,
    EXACT_NAME_DISTANCE_LIMIT_M,
    NAME_SCORE_LIMIT,
    RULE_VERSION,
    match_pair,
)
from .observability import ApiError, StopMeter
from .planner import plan_itinerary
from .transactions import read_transaction, write_transaction

MAX_BODY_BYTES = 200 * 1024 * 1024


def reset_database(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        "cluster_change", "cluster_event", "cluster_member", "cluster_current",
        "same_stop_edge", "adjudication", "stop_time", "trip_version", "trip",
        "site_version", "site_current", "site", "import_batch", "source_registry",
        "meta",
    ]
    with write_transaction(conn):
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.execute(
            "INSERT INTO meta(key,value) VALUES ('schema_version','1')"
        )
    return {"reset": True}


class WheelchairHandler(BaseHTTPRequestHandler):
    server_version = "WheelchairSQLite/1.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, db_path: str, **kwargs):
        self.db_path = db_path
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        # The automated test prints its own readable report; keep server output
        # quiet except startup and unexpected errors.
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "BODY_TOO_LARGE", f"body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ApiError(400, "INVALID_JSON", f"could not parse JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "INVALID_BODY", "JSON body must be an object")
        return value

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        return {k: v[-1] for k, v in qs.items()}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/README.md", "/docs"):
            readme = Path(__file__).resolve().parent.parent / "README.md"
            if readme.exists():
                self._send_text(200, readme.read_text(encoding="utf-8"))
            else:
                self._send_text(404, "README.md not found\n", "text/plain; charset=utf-8")
            return
        query = self._query()
        conn = connect(self.db_path)
        try:
            with read_transaction(conn):
                payload, status = self._handle_get(conn, path, query)
            self._send_json(status, payload)
        except ApiError as exc:
            self._send_json(exc.status, {
                "error": {"code": exc.code, "message": exc.message, "details": exc.details}
            })
        except Exception as exc:
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "INTERNAL", "message": str(exc)}})
        finally:
            conn.close()

    def do_POST(self) -> None:  # noqa: N802
        conn = connect(self.db_path)
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            payload = self._read_json()
            result, status = self._handle_post(conn, path, payload)
            self._send_json(status, result)
        except ApiError as exc:
            self._send_json(exc.status, {
                "error": {"code": exc.code, "message": exc.message, "details": exc.details}
            })
        except Exception as exc:
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "INTERNAL", "message": str(exc)}})
        finally:
            conn.close()

    def _handle_get(
        self, conn: sqlite3.Connection, path: str, q: dict[str, str]
    ) -> tuple[Any, int]:
        if path == "/health":
            counts = {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("site_current", "trip", "stop_time", "cluster_current", "adjudication")
            }
            return {"status": "ok", "counts": counts}, 200

        if path == "/" or path == "/v1":
            return {
                "service": "wheelchair-transfer-sqlite",
                "docs": "/README.md",
                "endpoints": [
                    "GET /health",
                    "GET /v1/matching-rules",
                    "POST /v1/imports",
                    "GET/POST /v1/adjudications",
                    "POST /v1/adjudications/undo",
                    "GET /v1/clusters",
                    "GET /v1/clusters/{cluster_id}",
                    "GET /v1/clusters/{cluster_id}/history",
                    "GET /v1/history",
                    "GET /v1/records?source=&stop_id=",
                    "GET /v1/itinerary?from_source=&from_stop_id=&to_source=&to_stop_id=&earliest_departure=",
                ],
            }, 200

        if path == "/v1/matching-rules":
            return {
                "rule_version": RULE_VERSION,
                "distance_limit_m": DISTANCE_LIMIT_M,
                "exact_normalized_name_distance_limit_m": EXACT_NAME_DISTANCE_LIMIT_M,
                "name_score_limit": NAME_SCORE_LIMIT,
                "method": [
                    "NFKC + casefold",
                    "remove punctuation/generic station words",
                    "Difflib SequenceMatcher ratio",
                    "haversine great-circle distance",
                    "connected components of automatic and adjudicated edges",
                ],
                "transfer_seconds": 180,
            }, 200

        if path == "/v1/clusters":
            return {"clusters": list_clusters(conn)}, 200

        if path.startswith("/v1/clusters/") and path.endswith("/history"):
            cluster_id = unquote(path[len("/v1/clusters/"):-len("/history")])
            return {
                "cluster_id": cluster_id,
                "history": cluster_history(conn, cluster_id, limit=int(q.get("limit", 100))),
            }, 200

        if path.startswith("/v1/clusters/"):
            cluster_id = unquote(path[len("/v1/clusters/"):])
            detail = cluster_detail(conn, cluster_id)
            if not detail:
                raise ApiError(404, "CLUSTER_NOT_FOUND", cluster_id)
            return detail, 200

        if path == "/v1/history":
            return {"events": cluster_history(conn, None, int(q.get("limit", 100)))}, 200

        if path == "/v1/adjudications":
            return {"adjudications": list_adjudications(conn)}, 200

        if path == "/v1/records":
            source = q.get("source") or q.get("source_id")
            stop_id = q.get("stop_id") or q.get("id")
            if not source or not stop_id:
                raise ApiError(400, "MISSING_PARAM", "source and stop_id are required")
            row = conn.execute(
                "SELECT * FROM site_current WHERE source=? AND stop_id=?",
                (source, stop_id),
            ).fetchone()
            if not row:
                raise ApiError(404, "RECORD_NOT_FOUND", f"{source}:{stop_id}")
            record = dict(row)
            cid = conn.execute(
                "SELECT cluster_id FROM cluster_member WHERE source=? AND stop_id=?",
                (source, stop_id),
            ).fetchone()
            record["cluster_id"] = cid["cluster_id"] if cid else None
            if cid:
                record["cluster"] = cluster_detail(conn, cid["cluster_id"])
            return record, 200

        if path == "/v1/itinerary":
            required = ("from_source", "from_stop_id", "to_source", "to_stop_id")
            missing = [k for k in required if not q.get(k)]
            if missing:
                raise ApiError(400, "MISSING_PARAM", f"missing parameters: {', '.join(missing)}")
            meter = StopMeter("itinerary")
            result = plan_itinerary(
                conn,
                from_source=q["from_source"],
                from_stop_id=q["from_stop_id"],
                to_source=q["to_source"],
                to_stop_id=q["to_stop_id"],
                earliest_departure=q.get("earliest_departure") or q.get("time") or "00:00:00",
                meter=meter,
            )
            return result, 200

        if path == "/v1/admin/stop-counts":
            cluster_id = q.get("cluster_id")
            if cluster_id:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM stop_time st
                    JOIN cluster_member cm
                      ON cm.source=st.stop_source
                     AND cm.stop_id=st.stop_id
                    WHERE cm.cluster_id=?
                    """,
                    (cluster_id,),
                ).fetchone()
                members = conn.execute(
                    """
                    SELECT cm.source, cm.stop_id, COUNT(st.id) AS n
                    FROM cluster_member cm
                    LEFT JOIN stop_time st
                      ON st.stop_source=cm.source AND st.stop_id=cm.stop_id
                    WHERE cm.cluster_id=?
                    GROUP BY cm.source, cm.stop_id
                    ORDER BY n DESC
                    """,
                    (cluster_id,),
                ).fetchall()
                return {
                    "cluster_id": cluster_id,
                    "stop_time_count": row["n"],
                    "members": [dict(r) for r in members],
                    "note": "fixture-size verification aggregate, separate from metered query/ruling work",
                }, 200
            total = conn.execute("SELECT COUNT(*) AS n FROM stop_time").fetchone()["n"]
            return {"stop_time_count": total}, 200

        raise ApiError(404, "NOT_FOUND", f"unknown path {path}")

    def _handle_post(
        self, conn: sqlite3.Connection, path: str, payload: dict[str, Any]
    ) -> tuple[Any, int]:
        if path == "/v1/matching/evaluate":
            def resolve(side: dict[str, Any]) -> dict[str, Any]:
                if all(k in side for k in ("name", "lat", "lon")):
                    return side
                source = side.get("source")
                stop_id = side.get("stop_id") or side.get("id")
                row = conn.execute(
                    "SELECT source,stop_id,name,lat,lon FROM site_current WHERE source=? AND stop_id=?",
                    (source, stop_id),
                ).fetchone() if source and stop_id else None
                if not row:
                    raise ApiError(404, "MATCH_RECORD_NOT_FOUND",
                                   f"cannot resolve {source}:{stop_id}; provide name/lat/lon or import the pair")
                return dict(row)

            a = resolve(payload.get("a") or {})
            b = resolve(payload.get("b") or {})
            return {"rule_version": RULE_VERSION, "evaluation": match_pair(a, b)}, 200

        if path == "/v1/imports":
            meter = StopMeter("import")
            return import_batch(conn, payload, meter), 200

        if path == "/v1/adjudications":
            meter = StopMeter("adjudicate")
            return create_adjudication(conn, payload, meter), 200

        if path == "/v1/adjudications/undo":
            meter = StopMeter("undo")
            return undo_latest(conn, payload, meter), 200

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "adjudications" and parts[3] == "undo":
            try:
                payload.setdefault("adjudication_id", int(parts[2]))
            except ValueError as exc:
                raise ApiError(400, "INVALID_ID", "adjudication id must be an integer") from exc
            meter = StopMeter("undo")
            return undo_latest(conn, payload, meter), 200

        if path == "/v1/admin/reset":
            if payload.get("confirm") != "reset":
                raise ApiError(400, "CONFIRM_REQUIRED", "send {'confirm':'reset'}")
            return reset_database(conn), 200

        raise ApiError(404, "NOT_FOUND", f"unknown path {path}")


def create_server(db_path: str | Path, host: str, port: int) -> ThreadingHTTPServer:
    db_path = str(db_path)
    conn = connect(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    handler = functools.partial(WheelchairHandler, db_path=db_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Wheelchair cross-operator transfer service")
    parser.add_argument("--db", default="data/wheelchair.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    init_db(conn)
    conn.close()

    handler = functools.partial(WheelchairHandler, db_path=args.db)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True
    actual_port = httpd.server_address[1]
    print(json.dumps({
        "event": "ready",
        "host": args.host,
        "port": actual_port,
        "db": str(Path(args.db).resolve()),
        "endpoints": "http://{}:{}/".format(args.host, actual_port),
    }), flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
