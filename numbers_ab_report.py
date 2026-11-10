import json, sys
from datetime import datetime
from pathlib import Path
ab, rep_p, qa, qb, active = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
def side(k):
    p = ab / k / "eval.json"
    if not p.exists():
        return None
    e = json.load(open(p))
    res = e["results"]
    out = {}
    for kind in ("numbers", "combined", "main"):
        if kind in res:
            q, pr = res[kind]["qty"]["acc"], res[kind]["price"]["acc"]
            d = (res[kind].get("description") or {}).get("acc")
            out[kind] = {"line_qty": q, "line_price": pr, "line_description": d,
                         "overall": round((q + pr) / 2, 4) if q is not None and pr is not None else None,
                         "supplier": (res[kind].get("supplier") or {}).get("acc"), "date": (res[kind].get("date") or {}).get("acc"),
                         "job": (res[kind].get("job") or {}).get("acc")}
    out["secs_per_order"] = res.get("secs_per_order") or e.get("secs_per_order")
    out["tests_scored"], out["scoring_stopped_at_time_limit"] = res.get("tests"), res.get("stopped_at_time_limit")
    ti = ab / k / "train_info.json"
    if ti.exists():
        out["train"] = json.load(open(ti))
    return out
plan = json.load(open(ab / "plan.json")) if (ab / "plan.json").exists() else None
cps = {k: {p.name.split("-")[1]: side(f"{k}/{p.name}") for p in sorted((ab / k).glob("epoch-*")) if (p / "eval.json").exists()}
       for k in ("A", "B") if (ab / k).is_dir()}                                       # 569: every saved epoch scored
r = {"id": ab.name, "kind": "numbers-ab", "at": datetime.now().isoformat(timespec="seconds"), "data": json.load(open(ab.parent / "data-sheet" / "source.json")),
     "plan": plan, "A": {"base": qa, **(side("A") or {"failed": True})}, "B": {"base": qb, **(side("B") or {"failed": True})},
     "live": {"run": active, **(side("live") or {"failed": True})}, "checkpoints": cps, "installed": False}
def ov(x):
    c = (x or {}).get("combined") or {}
    return c.get("overall") if c.get("overall") is not None else -1
r["best"] = {}
for k in ("A", "B"):
    cand = {f"final ({(r[k].get('train') or {}).get('epochs_done')} epochs)": r[k]} | {f"epoch {n}": v for n, v in cps.get(k, {}).items()}
    cand = {n: v for n, v in cand.items() if v and not v.get("failed")}
    if cand:
        n = max(cand, key=lambda n: ov(cand[n]))
        r["best"][k] = {"which": n, "qty_price": ov(cand[n]), "path": n}
pc = lambda v: "-" if v is None else f"{v * 100:.1f} %"
lines = []
for k in ("live", "A", "B"):
    s, nm = r[k].get("combined") or r[k].get("numbers"), r[k].get("numbers") or {}
    lines.append(f"{k}: " + ("failed" if not s else f"qty {pc(s['line_qty'])}, price {pc(s['line_price'])}, description {pc(s.get('line_description'))}, "
                             f"supplier {pc(nm.get('supplier'))}, date {pc(nm.get('date'))}, job {pc(nm.get('job'))}"
                             + (f" ({r[k]['tests_scored']} orders)" if r[k].get("tests_scored") else "")))
reader = (r["live"].get("main") or r["A"].get("main") or {})
r["reader"] = reader
best_txt = " | best: " + ", ".join(f"{k} {v['which']} {v['qty_price'] * 100:.1f} % qty + price" for k, v in r["best"].items()) if r["best"] else ""
r["why"] = "scored only, not installed (Dino picks): " + (f"{plan['epochs']} epochs each | " if plan else "") + " | ".join(lines) + best_txt + (
    f" | reader alone: qty {pc(reader.get('line_qty'))}, price {pc(reader.get('line_price'))}, supplier {pc(reader.get('supplier'))}, date {pc(reader.get('date'))}" if reader else "")
json.dump(r, open(rep_p, "w"), indent=1)
print("report:", rep_p, "|", r["why"])
