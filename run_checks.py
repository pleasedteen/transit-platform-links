#!/usr/bin/env python3
"""One-command readable end-to-end and stress checks.

Run:  python3 run_checks.py

The command starts the real HTTP service in a subprocess, imports data over
HTTP, checks domain behavior, loads the deterministic 200,010-stop fixture,
measures per-operation stop-touch counts, kills the server, restarts it, and
verifies persistence.  It is expected to finish well below 80 seconds.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wheelchair.fixtures import small_batch, stress_batches, stress_manifest  # noqa: E402
from wheelchair.load import post_json  # noqa: E402

MANIFEST = stress_manifest()


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str, str]] = []
        self.metrics: list[dict[str, Any]] = []
        self.started = time.time()

    def check(self, name: str, condition: bool, detail: Any = "") -> bool:
        detail_text = "" if detail in ("", None) else f" — {detail}"
        self.checks.append((bool(condition), name, str(detail)))
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {name}{detail_text}")
        return bool(condition)

    def metric(self, name: str, actual: int, limit: int = 5000, **extra: Any) -> None:
        ok = actual <= limit
        self.metrics.append({"operation": name, "actual": actual, "limit": limit, "ok": ok, **extra})
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] 处理量 {name}: {actual} / {limit} 次保守停靠触达"
              + (f" ({extra})" if extra else ""))

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")

    def finish(self) -> int:
        elapsed = time.time() - self.started
        failed = [c for c in self.checks if not c[0]]
        over = [m for m in self.metrics if not m["ok"]]
        print("\n" + "=" * 72)
        print(f"检查项: {len(self.checks) - len(failed)}/{len(self.checks)} 通过")
        print(f"性能项: {len(self.metrics) - len(over)}/{len(self.metrics)} 在 5000 上限内")
        print(f"总耗时: {elapsed:.2f}s (要求 < 80s)")
        if failed:
            print("失败检查:")
            for _, name, detail in failed:
                print(f"  - {name}: {detail}")
        if over:
            print("超量操作:")
            for m in over:
                print(f"  - {m['operation']}: {m['actual']} > {m['limit']}")
        print("=" * 72)
        return 1 if failed or over or elapsed >= 80 else 0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_health(base_url: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not become healthy: {last}")


def request(base_url: str, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def get_record(base: str, source: str, stop_id: str) -> dict[str, Any]:
    status, body = request(base, "GET", f"/v1/records?source={source}&stop_id={stop_id}")
    if status != 200:
        raise AssertionError(f"record {source}:{stop_id} failed: {status} {body}")
    return body


def itinerary(base: str, query: dict[str, str]) -> dict[str, Any]:
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in query.items())
    status, body = request(base, "GET", "/v1/itinerary?" + qs)
    if status != 200:
        raise AssertionError(f"itinerary HTTP {status}: {body}")
    return body


def start_server(db_path: Path, port: int, log_file: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    log = log_file.open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "wheelchair.app",
         "--db", str(db_path), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    proc._log_handle = log  # type: ignore[attr-defined]
    return proc


def stop_server(proc: subprocess.Popen, kill: bool = False) -> None:
    if proc.poll() is not None:
        return
    if kill:
        proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log = getattr(proc, "_log_handle", None)
    if log:
        log.close()


def run() -> int:
    report = Report()
    tmp = Path(tempfile.mkdtemp(prefix="wheelchair-check-"))
    db_path = tmp / "check.db"
    log_file = tmp / "server.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"工作目录: {tmp}")
    print(f"启动 HTTP 服务: {base}")
    proc = start_server(db_path, port, log_file)

    try:
        health = wait_health(base)
        report.check("服务启动并通过 /health", health.get("status") == "ok", health)

        report.section("1. 导入、版本与同站规则")
        status, rules = request(base, "GET", "/v1/matching-rules")
        report.check("匹配规则公开", status == 200 and rules.get("rule_version") == "name-location-v1",
                     f"距离≤{rules.get('distance_limit_m')}m, 名称相似度≥{rules.get('name_score_limit')}")

        batch = small_batch()
        status, imported = request(base, "POST", "/v1/imports", batch)
        report.check("小夹具分批导入成功", status == 200 and imported.get("sites_received") == 8,
                     f"sites={imported.get('sites_received')} trips={imported.get('trips_received')}")

        status, again = request(base, "POST", "/v1/imports", batch)
        report.check("同一 batch_id 重送只生效一次",
                     status == 200 and again.get("duplicate_batch") is True,
                     again.get("batch_id"))

        redeliver_version = {
            "batch_id": "small-redeliver-one-version",
            "sites": [batch["sites"][0]],
        }
        status, redelivered = request(base, "POST", "/v1/imports", redeliver_version)
        report.check("同来源同版本记录在新批次中也不重复生效",
                     status == 200 and redelivered.get("sites_duplicate_version") == 1,
                     f"duplicate_version={redelivered.get('sites_duplicate_version')}")

        request(base, "POST", "/v1/imports", {
            "batch_id": "version-v1",
            "sites": [{"source": "VV", "stop_id": "V1", "version": 1,
                       "name": "旧名称", "lat": 31.30, "lon": 121.30}],
        })
        request(base, "POST", "/v1/imports", {
            "batch_id": "version-v2",
            "sites": [{"source": "VV", "stop_id": "V1", "version": 2,
                       "name": "新名称", "lat": 31.30, "lon": 121.30}],
        })
        status, old_again = request(base, "POST", "/v1/imports", {
            "batch_id": "version-v1-redelivery",
            "sites": [{"source": "VV", "stop_id": "V1", "version": 1,
                       "name": "旧名称", "lat": 31.30, "lon": 121.30}],
        })
        current_v = get_record(base, "VV", "V1")
        report.check("较新版本为准，旧版本重送不回退",
                     old_again["sites_duplicate_version"] == 1 and current_v["name"] == "新名称",
                     f"current_version={current_v['version']}, name={current_v['name']}")

        correction_trip_v1 = {
            "source": "TC", "trip_id": "CORR", "version": 1,
            "stop_times": [
                {"seq": 1, "stop_id": "A", "departure": "12:00:00"},
                {"seq": 2, "stop_id": "B", "arrival": "12:10:00"},
            ],
        }
        request(base, "POST", "/v1/imports", {
            "batch_id": "trip-correction-v1",
            "sites": [
                {"source": "TC", "stop_id": "A", "version": 1, "name": "校正甲",
                 "lat": 31.60, "lon": 121.60, "accessible": True},
                {"source": "TC", "stop_id": "B", "version": 1, "name": "校正乙",
                 "lat": 31.61, "lon": 121.61, "accessible": True},
            ],
            "trips": [correction_trip_v1],
        })
        correction_v1_query = itinerary(base, {
            "from_source": "TC", "from_stop_id": "A", "to_source": "TC", "to_stop_id": "B",
            "earliest_departure": "11:00:00",
        })
        correction_trip_v2 = {
            "source": "TC", "trip_id": "CORR", "version": 2,
            "stop_times": [
                {"seq": 1, "stop_id": "A", "departure": "13:00:00"},
                {"seq": 2, "stop_id": "B", "arrival": "13:07:00"},
            ],
        }
        status, correction_import = request(base, "POST", "/v1/imports", {
            "batch_id": "trip-correction-v2", "trips": [correction_trip_v2],
        })
        correction_v2_query = itinerary(base, {
            "from_source": "TC", "from_stop_id": "A", "to_source": "TC", "to_stop_id": "B",
            "earliest_departure": "11:00:00",
        })
        status, correction_redelivery = request(base, "POST", "/v1/imports", {
            "batch_id": "trip-correction-v2-redelivery", "trips": [correction_trip_v2],
        })
        report.check("班次更正采用新版本，同版本重送不重复生效",
                     correction_v1_query.get("arrival_time") == "12:10:00"
                     and correction_import.get("trips_new_version") == 1
                     and correction_v2_query.get("departure_time") == "13:00:00"
                     and correction_v2_query.get("arrival_time") == "13:07:00"
                     and correction_redelivery.get("trips_duplicate_version") == 1,
                     f"{correction_v1_query.get('arrival_time')} -> "
                     f"{correction_v2_query.get('departure_time')}-{correction_v2_query.get('arrival_time')}")

        h = get_record(base, "SA", "H")
        member_ids = {(m["source"], m["stop_id"]) for m in h["cluster"]["members"]}
        report.check("连续同站证据连接三个记录",
                     member_ids == {("SA", "H"), ("SB", "H1"), ("SB", "H2")},
                     sorted(member_ids))

        p = get_record(base, "SP", "P1")
        conclusion = p["cluster"]["conclusion"]
        report.check("无障碍结论优先采用设施普查",
                     conclusion["accessible"] is False and conclusion["basis"] == "census",
                     conclusion.get("selected"))

        status, qbatch = request(base, "POST", "/v1/imports", {
            "batch_id": "operator-latest",
            "sites": [
                {"source": "QY", "stop_id": "Q1", "version": 1, "source_kind": "operator",
                 "name": "庆春门", "lat": 31.40, "lon": 121.40,
                 "accessible": False, "observed_at": "2026-01-01T00:00:00Z"},
                {"source": "QZ", "stop_id": "Q2", "version": 1, "source_kind": "operator",
                 "name": "庆春门", "lat": 31.4001, "lon": 121.40,
                 "accessible": True, "observed_at": "2026-05-01T00:00:00Z"},
            ],
        })
        q = get_record(base, "QY", "Q1")
        report.check("同类结论取最新观测",
                     q["cluster"]["conclusion"]["accessible"] is True
                     and q["cluster"]["conclusion"]["selected"]["stop_id"] == "Q2",
                     q["cluster"]["conclusion"].get("selected"))

        blocked = itinerary(base, {
            "from_source": "SP", "from_stop_id": "P1",
            "to_source": "SB", "to_stop_id": "D",
            "earliest_departure": "00:00:00",
        })
        report.check("只有确认无障碍通行的站群才能上车",
                     blocked.get("feasible") is False
                     and blocked.get("reason") == "ORIGIN_NOT_ACCESSIBLE",
                     blocked.get("reason"))

        report.section("2. 三分钟换乘与裁决撤销顺序")
        small_trip = itinerary(base, {
            "from_source": "SA", "from_stop_id": "O",
            "to_source": "SB", "to_stop_id": "D",
            "earliest_departure": "08:00:00",
        })
        report.check("小夹具返回跨运营商行程且换乘≥3分钟",
                     small_trip.get("feasible") is True
                     and small_trip.get("arrival_time") == "08:22:00"
                     and small_trip["transfers"][0]["transfer_seconds"] >= 180,
                     f"arrival={small_trip.get('arrival_time')}, "
                     f"transfer={small_trip['transfers'][0]['transfer_seconds'] if small_trip.get('transfers') else None}s")

        request(base, "POST", "/v1/imports", {
            "batch_id": "transfer-threshold",
            "sites": [
                {"source": "TT", "stop_id": "O", "version": 1, "name": "试车站",
                 "lat": 31.50, "lon": 121.50, "accessible": True},
                {"source": "TT", "stop_id": "H1", "version": 1, "name": "换乘枢纽",
                 "lat": 31.51, "lon": 121.51, "accessible": True},
                {"source": "TU", "stop_id": "H2", "version": 1, "name": "换乘枢纽",
                 "lat": 31.51, "lon": 121.51, "accessible": True},
                {"source": "TU", "stop_id": "D", "version": 1, "name": "阈值终点",
                 "lat": 31.52, "lon": 121.52, "accessible": True},
            ],
            "trips": [
                {"source": "TT", "trip_id": "IN", "version": 1, "stop_times": [
                    {"seq": 1, "stop_id": "O", "departure": "09:50:00"},
                    {"seq": 2, "stop_id": "H1", "arrival": "10:00:00", "departure": "10:00:30"},
                ]},
                {"source": "TU", "trip_id": "TOO_EARLY", "version": 1, "stop_times": [
                    {"seq": 1, "stop_id": "H2", "departure": "10:02:00"},
                    {"seq": 2, "stop_id": "D", "arrival": "10:08:00"},
                ]},
                {"source": "TU", "trip_id": "OK", "version": 1, "stop_times": [
                    {"seq": 1, "stop_id": "H2", "departure": "10:04:30"},
                    {"seq": 2, "stop_id": "D", "arrival": "10:11:00"},
                ]},
            ],
        })
        threshold_trip = itinerary(base, {
            "from_source": "TT", "from_stop_id": "O",
            "to_source": "TU", "to_stop_id": "D",
            "earliest_departure": "09:50:00",
        })
        boarded = [leg.get("trip_id") for leg in threshold_trip.get("legs", [])]
        report.check("不足三分钟的换车被排除，搭乘下一班",
                     threshold_trip.get("feasible") is True
                     and "TOO_EARLY" not in boarded and "OK" in boarded
                     and threshold_trip.get("arrival_time") == "10:11:00",
                     boarded)

        pair = {"a": {"source": "SA", "stop_id": "O"},
                "b": {"source": "SB", "stop_id": "H2"}}
        s1, c1 = request(base, "POST", "/v1/adjudications",
                          {**pair, "verdict": "confirm", "reason": "测试确认远处站实际连通"})
        report.check("确认一对记录后站群合并",
                     s1 == 200 and any(x["type"] == "merge" for x in c1.get("cluster_changes", [])),
                     [(x["type"], x.get("cluster_id")) for x in c1.get("cluster_changes", [])])
        s2, c2 = request(base, "POST", "/v1/adjudications",
                          {**pair, "verdict": "reject", "reason": "复核发现不连通"})
        report.check("更新为驳回后按最新裁决拆开",
                     s2 == 200 and any(x["type"] == "split" for x in c2.get("cluster_changes", [])),
                     [(x["type"], x.get("cluster_id")) for x in c2.get("cluster_changes", [])])
        s3, u1 = request(base, "POST", "/v1/adjudications/undo", {"reason": "撤销最新驳回"})
        report.check("第一次撤销从最新裁决开始并恢复前一确认",
                     s3 == 200 and any(x["type"] == "merge" for x in u1.get("cluster_changes", [])),
                     u1.get("undone", {}).get("id"))
        s4, u2 = request(base, "POST", "/v1/adjudications/undo", {"reason": "撤销确认"})
        report.check("第二次撤销旧确认后恢复自动站群",
                     s4 == 200 and any(x["type"] == "split" for x in u2.get("cluster_changes", [])),
                     [(x["type"]) for x in u2.get("cluster_changes", [])])
        s5, u3 = request(base, "POST", "/v1/adjudications/undo", {})
        report.check("没有有效裁决时继续撤销返回冲突", s5 == 409, u3.get("error", {}).get("code"))

        status, hist = request(base, "GET", "/v1/history?limit=20")
        report.check("可查历次拆并缘由",
                     status == 200 and any(e.get("reason") for e in hist.get("events", [])),
                     f"{len(hist.get('events', []))} events")

        request(base, "POST", "/v1/adjudications", {
            "a": {"source": "VV", "stop_id": "V1"},
            "b": {"source": "SA", "stop_id": "O"},
            "verdict": "confirm",
            "reason": "验证裁决后导入仍参与计算",
        })
        request(base, "POST", "/v1/imports", {
            "batch_id": "after-ruling-new-site",
            "sites": [{"source": "VV", "stop_id": "V2", "version": 1,
                       "name": "新名称", "lat": 31.30, "lon": 121.30,
                       "accessible": True}],
        })
        after_ruling_site = get_record(base, "VV", "V2")
        after_ruling_refs = {
            (m["source"], m["stop_id"]) for m in after_ruling_site["cluster"]["members"]
        }
        report.check("裁决之后导入的新记录继续参与站群计算",
                     {("VV", "V2"), ("VV", "V1"), ("SA", "O")} <= after_ruling_refs,
                     sorted(after_ruling_refs))

        report.section("3. 20 万停靠压力夹具")
        status, reset = request(base, "POST", "/v1/admin/reset", {"confirm": "reset"})
        report.check("压力测试前重置数据库", status == 200 and reset.get("reset") is True)

        load_started = time.time()
        batch_count = 0
        inserted = 0

        stop_readers = threading.Event()
        reader_state = {"ok": 0, "errors": []}
        reader_lock = threading.Lock()
        stress_qs = urllib.parse.urlencode(MANIFEST["query"])

        def concurrent_reader() -> None:
            while not stop_readers.is_set():
                try:
                    status1, _ = request(base, "GET", "/v1/clusters")
                    status2, _ = request(base, "GET", "/v1/itinerary?" + stress_qs)
                    if status1 >= 500 or status2 >= 500:
                        with reader_lock:
                            reader_state["errors"].append((status1, status2))
                    else:
                        with reader_lock:
                            reader_state["ok"] += 1
                except Exception as exc:  # noqa: BLE001
                    with reader_lock:
                        reader_state["errors"].append(repr(exc))
                # A small pause keeps this a correctness probe rather than a
                # request-generation stress test that could distort timings.
                time.sleep(0.05)

        readers = [threading.Thread(target=concurrent_reader, daemon=True) for _ in range(2)]
        for t in readers:
            t.start()
        for batch in stress_batches():
            result = post_json(base, "/v1/imports", batch, timeout=120)
            batch_count += 1
            inserted += int(result.get("stop_times_inserted") or 0)
        stop_readers.set()
        for t in readers:
            t.join(timeout=5)
        load_elapsed = time.time() - load_started
        report.check(
            "分批导入期间并发站群/行程读取无 5xx 或连接错误",
            not reader_state["errors"] and reader_state["ok"] > 0,
            f"successful_read_rounds={reader_state['ok']}, errors={reader_state['errors'][:2]}",
        )

        health = wait_health(base)
        total_stops = health["counts"]["stop_time"]
        report.check("压力夹具至少 200,000 次停靠",
                     total_stops >= 200_000 and inserted == MANIFEST["total_stop_times"],
                     f"HTTP 批次={batch_count}, 插入={inserted}, 库内={total_stops}, 装载={load_elapsed:.2f}s")

        hub_a = get_record(base, "OA", "HUB_EAST")["cluster_id"]
        hub_b = get_record(base, "OB", "HUB_WEST")["cluster_id"]
        _, counts_a = request(base, "GET", f"/v1/admin/stop-counts?cluster_id={hub_a}")
        _, counts_b = request(base, "GET", f"/v1/admin/stop-counts?cluster_id={hub_b}")
        report.check("拆开后 A 侧关联至少 10,000 次停靠",
                     counts_a["stop_time_count"] >= 10_000, counts_a["stop_time_count"])
        report.check("拆开后 B 侧关联至少 10,000 次停靠",
                     counts_b["stop_time_count"] >= 10_000, counts_b["stop_time_count"])

        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        late_affected = ro.execute(
            "SELECT COUNT(*) FROM stop_time WHERE stop_source IN ('OA','OB') AND departure_s>=79200"
        ).fetchone()[0]
        late_other = ro.execute(
            "SELECT COUNT(*) FROM stop_time WHERE stop_source='DC' AND departure_s>=79200"
        ).fetchone()[0]
        total_db = ro.execute("SELECT COUNT(*) FROM stop_time").fetchone()[0]
        ro.close()
        report.check("晚查询时段受影响线路剩余停靠少于 1,000",
                     late_affected < 1000, f"OA/OB at/after 22:00={late_affected}")
        report.check("同一较晚时段其他线路仍在运行", late_other > 0,
                     f"DC at/after 22:00={late_other}")
        print(f"  夹具规模核对: stop_time={total_db}, 晚段 OA/OB={late_affected}, 晚段 DC={late_other}")

        q = MANIFEST["query"]
        split = itinerary(base, q)
        split_first_leg = split.get("legs", [{}])[0]
        split_intermediate_ids = [
            s.get("stop_id") for s in split_first_leg.get("intermediate_stops", [])
        ]
        report.check("未裁决时走自动匹配的较慢跨运营商路线，并原车经过中途站",
                     split.get("feasible") is True
                     and split.get("arrival_time") == MANIFEST["split_arrival"]
                     and [l.get("trip_id") for l in split.get("legs", [])] == [
                         "TARGET_A_1", "TARGET_B_SLOW"]
                     and "MID_PASS" in split_intermediate_ids,
                     f"arrival={split.get('arrival_time')}, intermediate={split_intermediate_ids}")
        report.metric("拆分状态行程查询", split["stats"]["accounted_stop_touches"],
                      db_reads=split["stats"]["stop_time_db_reads"],
                      objects=split["stats"]["stop_objects_built"])

        t0 = time.perf_counter()
        status, confirm = request(base, "POST", "/v1/adjudications", {
            **MANIFEST["confirm_pair"], "verdict": "confirm",
            "reason": "压力枢纽：站厅连通，轮椅可通行", "by": "stress-dispatcher",
        })
        confirm_wall = time.perf_counter() - t0
        merged_change = any(x["type"] == "merge" for x in confirm.get("cluster_changes", []))
        report.check("裁决接通压力枢纽", status == 200 and merged_change,
                     f"HTTP {status}, {[(x['type']) for x in confirm.get('cluster_changes', [])]}, {confirm_wall*1000:.1f}ms")
        report.metric("裁决 confirm", confirm["processing"]["accounted_stop_touches"],
                      wall_ms=round(confirm_wall * 1000, 2),
                      db_reads=confirm["processing"]["stop_time_db_reads"],
                      writes=confirm["processing"]["stop_rows_written"])

        merged = itinerary(base, q)
        merged_transfer = merged.get("transfers", [{}])[0].get("transfer_seconds")
        report.check("接通后查询变为更快的枢纽换乘",
                     merged.get("feasible") is True
                     and merged.get("arrival_time") == MANIFEST["merged_arrival"]
                     and [l.get("trip_id") for l in merged.get("legs", [])] == [
                         "TARGET_A_1", "TARGET_B_FAST"]
                     and merged["transfers"][0]["transfer_seconds"] >= 180,
                     f"arrival={merged.get('arrival_time')}, transfer={merged_transfer}s")
        report.metric("接通状态行程查询", merged["stats"]["accounted_stop_touches"],
                      db_reads=merged["stats"]["stop_time_db_reads"],
                      objects=merged["stats"]["stop_objects_built"])

        t0 = time.perf_counter()
        status, undo = request(base, "POST", "/v1/adjudications/undo",
                               {"reason": "压力测试撤销枢纽裁决"})
        undo_wall = time.perf_counter() - t0
        report.check("撤销后枢纽拆开",
                     status == 200 and any(x["type"] == "split" for x in undo.get("cluster_changes", [])),
                     f"HTTP {status}, {[x['type'] for x in undo.get('cluster_changes', [])]}, {undo_wall*1000:.1f}ms")
        report.metric("撤销 undo", undo["processing"]["accounted_stop_touches"],
                      wall_ms=round(undo_wall * 1000, 2),
                      db_reads=undo["processing"]["stop_time_db_reads"],
                      writes=undo["processing"]["stop_rows_written"])

        after_undo = itinerary(base, q)
        report.check("撤销后查询恢复为较慢路线，随拆并改变",
                     after_undo.get("arrival_time") == MANIFEST["split_arrival"],
                     after_undo.get("arrival_time"))
        report.metric("撤销后行程查询", after_undo["stats"]["accounted_stop_touches"],
                      db_reads=after_undo["stats"]["stop_time_db_reads"],
                      objects=after_undo["stats"]["stop_objects_built"])

        report.section("4. 原子导入与意外退出重启")
        bad_status, bad = request(base, "POST", "/v1/imports", {
            "batch_id": "atomic-bad-batch",
            "sites": [{"source": "ATOM", "stop_id": "SHOULD_ROLLBACK", "version": 1,
                       "name": "应回滚", "lat": 1, "lon": 1, "accessible": True}],
            "trips": [{"source": "ATOM", "trip_id": "BAD_TIME", "version": 1,
                       "stop_times": [{"seq": 1, "stop_id": "X", "departure": "not-a-time"}]}],
        })
        missing_status, _ = request(base, "GET", "/v1/records?source=ATOM&stop_id=SHOULD_ROLLBACK")
        report.check("坏批次整体回滚，查询仍只读完整状态",
                     bad_status == 400 and missing_status == 404,
                     f"import={bad_status}, record_after={missing_status}")

        stop_server(proc, kill=True)
        report.check("已用 SIGKILL 模拟意外退出", proc.poll() is not None)
        port2 = free_port()
        base2 = f"http://127.0.0.1:{port2}"
        proc = start_server(db_path, port2, log_file)
        wait_health(base2)
        restarted = itinerary(base2, q)
        status, adjs = request(base2, "GET", "/v1/adjudications")
        revoked_persisted = any(a.get("revoked_at") for a in adjs.get("adjudications", []))
        report.check("重启后已确认的撤销继续有效，只读到完整状态",
                     restarted.get("arrival_time") == MANIFEST["split_arrival"]
                     and status == 200 and revoked_persisted,
                     f"arrival={restarted.get('arrival_time')}, revoked_persisted={revoked_persisted}")

        status, hub_detail = request(base2, "GET", f"/v1/clusters/{hub_a}/history?limit=10")
        report.check("重启后仍可查该站群拆并历史",
                     status == 200 and len(hub_detail.get("history", [])) >= 1,
                     f"{len(hub_detail.get('history', []))} history events")

    finally:
        stop_server(proc, kill=False)

    code = report.finish()
    if code != 0 and log_file.exists():
        print("\n服务器日志末尾:")
        lines = log_file.read_text(errors="replace").splitlines()
        print("\n".join(lines[-80:]))
    return code


if __name__ == "__main__":
    raise SystemExit(run())
