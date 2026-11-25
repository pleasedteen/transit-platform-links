"""HTTP loader for deterministic offline fixtures or JSON/JSONL batch files."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from .fixtures import small_batch, stress_batches


def post_json(base_url: str, path: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {path} failed {exc.code}: {body}") from exc


def iter_file_batches(path: str | Path) -> Iterator[dict[str, Any]]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)
    else:
        value = json.loads(text)
        if isinstance(value, list):
            for batch in value:
                yield batch
        else:
            yield value


def load_named_fixture(base_url: str, name: str) -> dict[str, Any]:
    if name == "small":
        batches = [small_batch()]
    elif name == "stress":
        batches = stress_batches()
    else:
        raise ValueError("fixture name must be small or stress")

    count = 0
    stops = 0
    started = time.time()
    last: dict[str, Any] = {}
    for batch in batches:
        last = post_json(base_url, "/v1/imports", batch)
        count += 1
        stops += int(last.get("stop_times_inserted") or 0)
    return {
        "batches": count,
        "stop_times_inserted": stops,
        "elapsed_seconds": round(time.time() - started, 3),
        "last_summary": last,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Load offline fixture batches over HTTP")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", choices=["small", "stress"])
    source.add_argument("--file", help="JSON batch or JSONL batches file")
    args = parser.parse_args()

    started = time.time()
    batches = 0
    stops = 0
    if args.fixture:
        result = load_named_fixture(args.base_url, args.fixture)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    for batch in iter_file_batches(args.file):
        result = post_json(args.base_url, "/v1/imports", batch)
        batches += 1
        stops += int(result.get("stop_times_inserted") or 0)
        print(f"loaded batch {batches}: {result.get('batch_id')} "
              f"stops+={result.get('stop_times_inserted', 0)}")
    print(json.dumps({
        "batches": batches,
        "stop_times_inserted": stops,
        "elapsed_seconds": round(time.time() - started, 3),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
