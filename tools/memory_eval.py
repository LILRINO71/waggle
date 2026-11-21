"""Retrieval eval for Waggle's memory search, run on a COPY of the real data (never the live home or vault).

    python tools/memory_eval.py --home <copy of ~/.waggle> [--embed-model nomic-embed-text] [--json out.json]

The home folder must hold a config.json whose "vault" points at a COPY of the vault, plus (optionally) a
knowledge.db with the quiz bank. WAGGLE_HOME is set from --home before anything is imported, so nothing under the
real ~/.waggle can be read or written. Re-embedding (a changed --embed-model, or edited notes) writes only to the
copy.

Cases: tools/memory_eval_cases.json (hand-derived questions with known target notes, off-topic queries) plus the quiz
bank (quiz.py cloze items whose answer is in the vault; the held-out third is reported separately).

Reported per mode (embeddings only = memory_hybrid off, BM25 only, hybrid):
  recall@5  share of queries with a correct chunk among the top 5 (at most 2 chunks per note, as search() returns)
  MRR       mean of 1 / rank of the first correct chunk (rank 1..10, else 0)
and for the gated retrieval a turn actually gets (embeddings only vs hybrid):
  answered  share of queries whose injected chunks contain a correct one
  false     share of off-topic queries that still got something injected (should be 0)
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path


def _early_home():
    for i, a in enumerate(sys.argv):
        if a == "--home" and i + 1 < len(sys.argv):
            os.environ["WAGGLE_HOME"] = str(Path(sys.argv[i + 1]).resolve())
            return
        if a.startswith("--home="):
            os.environ["WAGGLE_HOME"] = str(Path(a.split("=", 1)[1]).resolve())
            return
    sys.exit("--home <copy of the data home> is required (this tool never touches the live ~/.waggle)")


_early_home()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402

from waggle import kstore, memory, ollama  # noqa: E402
from waggle.config import HOME, Settings  # noqa: E402

CASES = Path(__file__).with_name("memory_eval_cases.json")


def cached_embedder(s):
    cache = {}

    def embed(texts):
        out = []
        for t in texts:
            if t not in cache:
                cache[t] = ollama.embed([t], "query", s)[0]
            out.append(cache[t])
        return out
    return embed


def quiz_cases(s):
    bank = kstore.get("quiz.bank", {}) or {}
    text = memory_text(s)
    items = []
    for it in sorted(bank.values(), key=lambda i: i["id"]):
        if it.get("active", True) and it["answer"].lower() in text:
            items.append({"q": it["question"], "answer": it["answer"].lower(), "id": it["id"],
                          "held_out": int(it["id"][:8], 16) % 3 == 0})
    return items


def memory_text(s):
    con = memory._db()
    rows = con.execute("SELECT text FROM chunks WHERE idx >= 0").fetchall()
    con.close()
    return "\n".join(r[0] for r in rows).lower()


def ranked(mode, q, s, embed, now):
    """The top-10 chunk list (best first, two per note at most) for a mode."""
    if mode == "bm25":
        lex, _, _ = memory.fts_search(q, limit=60)
        rows = {(r["path"], r["idx"]): r for r in memory._rank(q, s, embed, now)[0]}
        scored = [rows[(p, i)] for p, i, _ in lex if (p, i) in rows]
    else:
        s.memory_hybrid = mode == "hybrid"
        scored, _ = memory._rank(q, s, embed, now)
    return memory._per_note(scored, 10)


def evaluate(s, embed, now, args):
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    derived = [{"q": c["q"], "hit": (lambda ch, t=set(c["targets"]): ch["path"] in t), "kind": "note"}
               for c in cases["queries"]]
    quiz = [{"q": c["q"], "hit": (lambda ch, a=c["answer"]: a in ch["text"].lower()), "kind": "quiz",
             "held_out": c["held_out"]} for c in quiz_cases(s)]
    groups = {"notes": derived, "quiz": quiz, "quiz held-out": [c for c in quiz if c["held_out"]]}
    out = {"embed_model": s.embed_model, "n": {k: len(v) for k, v in groups.items()}, "rank": {}, "gated": {},
           "latency_ms": {}}
    for mode in ("dense", "bm25", "hybrid"):
        for name, group in groups.items():
            rec = mrr = 0
            for c in group:
                top = ranked(mode, c["q"], s, embed, now)
                rank = next((i for i, ch in enumerate(top, 1) if c["hit"](ch)), 0)
                rec += bool(rank and rank <= 5)
                mrr += 1 / rank if rank else 0
            n = len(group) or 1
            out["rank"].setdefault(name, {})[mode] = {"recall@5": round(rec / n, 3), "mrr": round(mrr / n, 3)}
    for mode in ("dense", "hybrid"):
        s.memory_hybrid = mode == "hybrid"
        for name, group in groups.items():
            got = 0
            for c in group:
                hits, _ = memory.gate(*memory._rank(c["q"], s, embed, now))
                got += any(c["hit"](h) for h in hits)
            out["gated"].setdefault(name, {})[mode] = round(got / (len(group) or 1), 3)
        false = sum(bool(memory.gate(*memory._rank(q, s, embed, now))[0]) for q in cases["negative"])
        out["gated"].setdefault("false injections", {})[mode] = f"{false}/{len(cases['negative'])}"
        times = []
        for c in derived:
            t0 = time.perf_counter()
            memory._rank(c["q"], s, embed, now)
            times.append((time.perf_counter() - t0) * 1000)
        out["latency_ms"][mode] = round(statistics.median(times), 1)
    s.memory_hybrid = True
    return out


def show(res):
    print(f"\nembedding model: {res['embed_model']}   cases: {res['n']}")
    print(f"{'ranking (top 5 / MRR)':<28}{'embeddings only':>18}{'BM25 only':>14}{'hybrid':>14}")
    for name, modes in res["rank"].items():
        cells = [f"{modes[m]['recall@5']:.2f} / {modes[m]['mrr']:.2f}" for m in ("dense", "bm25", "hybrid")]
        print(f"{name:<28}{cells[0]:>18}{cells[1]:>14}{cells[2]:>14}")
    print(f"\n{'gated injection':<28}{'embeddings only':>18}{'hybrid':>14}")
    for name, modes in res["gated"].items():
        print(f"{name:<28}{str(modes['dense']):>18}{str(modes['hybrid']):>14}")
    print(f"\nrank time per query (median, embedding cached): {res['latency_ms']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True)
    ap.add_argument("--embed-model", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--no-index", action="store_true", help="use the copy's index as it is (no re-embedding)")
    args = ap.parse_args()
    if "waggle" in str(HOME).lower() and str(HOME).lower().endswith((".waggle", ".waggle\\")):
        sys.exit("refusing to run against a folder named like the live data home")
    s = Settings.load()
    if args.embed_model:
        s.embed_model = args.embed_model
    t0 = time.time()
    if not args.no_index:
        n = memory.reindex(s)
        print(f"indexed {n} note(s) with {s.embed_model} in {time.time() - t0:.0f}s")
    now = dt.datetime.fromisoformat(json.loads(CASES.read_text(encoding="utf-8"))["now"])
    res = evaluate(s, cached_embedder(s), now, args)
    show(res)
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
