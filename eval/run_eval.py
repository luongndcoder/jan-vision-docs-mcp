#!/usr/bin/env python3
"""Retrieval eval for search_notes.

    .venv/bin/python eval/run_eval.py                      # run + print report
    .venv/bin/python eval/run_eval.py --save               # also save eval/results/<ts>.json
    .venv/bin/python eval/run_eval.py --compare eval/results/<ts>.json

questions.jsonl: {"id","query","type","expect":[{"file","pages":[..]}],"note"}
`expect: []` = negative (answer is NOT in the vault).

Metrics
- positives: hit@1, hit@3, hit@6 (right file AND page), MRR@6
- negatives: pass = 0 matches; "low" = only low-confidence (semantic-low)
  results returned; "fail" = confident results returned
- latency p50 / p95 (first query reported separately as cold start)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import server_extra as se  # noqa: E402

K = 6


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rank_of(results: list[dict], expect: list[dict]) -> int | None:
    """1-based rank of the first result matching an expected file+page."""
    for i, r in enumerate(results[:K], start=1):
        name = os.path.basename(r.get("file", ""))
        page = str(r.get("page", ""))
        for e in expect:
            if name == e["file"] and page in [str(p) for p in e["pages"]]:
                return i
    return None


def run(questions: list[dict]) -> list[dict]:
    rows = []
    for q in questions:
        t = time.time()
        raw = se.search_notes(q["query"])
        dt = time.time() - t
        try:
            res = json.loads(raw)
        except ValueError:
            res = {"mode": "error", "results": [], "matches": 0}
        results = res.get("results") or []
        row = {"id": q["id"], "type": q["type"], "query": q["query"],
               "mode": res.get("mode"), "matches": res.get("matches", 0),
               "seconds": round(dt, 2),
               "top": [f"{os.path.basename(r.get('file',''))}#p{r.get('page','')}"
                       for r in results[:3]]}
        if q["expect"]:
            row["rank"] = rank_of(results, q["expect"])
        else:
            if not results:
                row["neg"] = "pass"
            elif res.get("mode") == "semantic-low":
                row["neg"] = "low"
            else:
                row["neg"] = "fail"
        rows.append(row)
        mark = (("✓" if row.get("rank") == 1 else f"@{row['rank']}" if row.get("rank") else "✗")
                if q["expect"] else {"pass": "✓", "low": "~", "fail": "✗"}[row["neg"]])
        print(f"  {mark:3} {q['id']:5} {q['type']:10} {row['mode'] or '':18} {dt:5.1f}s  {q['query'][:60]}",
              flush=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    pos = [r for r in rows if "rank" in r]
    neg = [r for r in rows if "neg" in r]
    lat = [r["seconds"] for r in rows[1:]] or [0]
    s = {
        "n": len(rows), "positives": len(pos), "negatives": len(neg),
        "hit@1": sum(r["rank"] == 1 for r in pos) / max(1, len(pos)),
        "hit@3": sum(bool(r["rank"]) and r["rank"] <= 3 for r in pos) / max(1, len(pos)),
        "hit@6": sum(bool(r["rank"]) for r in pos) / max(1, len(pos)),
        "mrr@6": sum(1 / r["rank"] for r in pos if r["rank"]) / max(1, len(pos)),
        "neg_pass": sum(r["neg"] == "pass" for r in neg) / max(1, len(neg)),
        "neg_low": sum(r["neg"] == "low" for r in neg) / max(1, len(neg)),
        "neg_fail": sum(r["neg"] == "fail" for r in neg) / max(1, len(neg)),
        "cold_s": rows[0]["seconds"] if rows else 0,
        "p50_s": statistics.median(lat),
        "p95_s": sorted(lat)[min(len(lat) - 1, -(-len(lat) * 95 // 100) - 1)],
    }
    by_type = {}
    for r in pos:
        b = by_type.setdefault(r["type"], [0, 0])
        b[0] += r["rank"] == 1
        b[1] += 1
    s["hit@1_by_type"] = {k: f"{a}/{n}" for k, (a, n) in sorted(by_type.items())}
    return s


def report(s: dict) -> None:
    print(f"\n  positives {s['positives']}: hit@1 {s['hit@1']:.0%}  hit@3 {s['hit@3']:.0%}  "
          f"hit@6 {s['hit@6']:.0%}  MRR {s['mrr@6']:.2f}")
    print(f"  by type (hit@1): {s['hit@1_by_type']}")
    print(f"  negatives {s['negatives']}: pass {s['neg_pass']:.0%}  low-conf {s['neg_low']:.0%}  "
          f"FAIL {s['neg_fail']:.0%}")
    print(f"  latency: cold {s['cold_s']}s  p50 {s['p50_s']}s  p95 {s['p95_s']}s")


def compare(rows: list[dict], old_path: str) -> None:
    old = {r["id"]: r for r in json.load(open(old_path))["rows"]}

    def ok(r):
        return r.get("rank") == 1 if "rank" in r else r.get("neg") == "pass"
    fixed = [r["id"] for r in rows if r["id"] in old and ok(r) and not ok(old[r["id"]])]
    broken = [r["id"] for r in rows if r["id"] in old and not ok(r) and ok(old[r["id"]])]
    print(f"\n  vs {os.path.basename(old_path)}: fixed {fixed or '-'}  BROKEN {broken or '-'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.jsonl"))
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--compare")
    a = ap.parse_args()
    qs = load(a.questions)
    print(f"{len(qs)} questions, vault {se.VAULT_DEFAULT}\n")
    rows = run(qs)
    s = summarize(rows)
    report(s)
    if a.compare:
        compare(rows, a.compare)
    if a.save:
        out = os.path.join(HERE, "results", time.strftime("%Y%m%d-%H%M%S") + ".json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        json.dump({"summary": s, "rows": rows}, open(out, "w"), ensure_ascii=False, indent=1)
        print(f"\n  saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
