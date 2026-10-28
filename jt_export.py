#!/usr/bin/env python3
"""Label Studio 'JT REVIEW' export -> <root>/training/train.jsonl + test.jsonl (the corrected review set that eval_jt.py,
the worked examples and the fine-tune use). Rebuilds both files from the export every time, so one run after each
correcting session keeps them current.

    python3 jt_export.py <export.json> [more exports...]          # dry run: what would change
    python3 jt_export.py <export.json> --apply                    # write, with a backup of the old files

Rules: writer spellings are merged (DEKLAN/DEACON -> DICAN, SANET/DAVID -> DAVID/SANET); forms marked "Not a JT /
unusable scan" are left out; forms already in the set keep their train/test side, new ones go 1 in 5 to test by a hash
of the id (stable, so re-running never moves a form). customer, transferred_by and receiving_stamp are not fields in
Label Studio: they are carried from the reader's prediction unverified, as the first export did, and eval_jt.py no
longer scores them.
"""
import argparse, collections, datetime, hashlib, json, os, re, shutil, sys

WRITER_MERGE = {"DEKLAN": "DICAN", "DEACON": "DICAN", "DEKLEN": "DICAN", "SANET/DAVID": "DAVID/SANET", "MELISSA": "MELYSSA",
                "MELISSA/IVOR": "MELYSSA/IVOR", "IVOR/MELYSSA": "MELYSSA/IVOR", "IVOR/MELISSA": "MELYSSA/IVOR"}
UNUSABLE = re.compile(r"not a jt|unusable", re.I)
HEAD = {"jt_number": "jt_number", "date": "date", "job_no": "mps_job_no", "transferred_from": "transferred_from",
        "transferred_to": "transferred_to", "received_by": "received_by", "remarks": "remarks"}
CARRIED = ("customer", "transferred_by", "receiving_stamp")


def writer_name(w):
    w = re.sub(r"\s+", "", str(w or "").upper().strip()) or "UNKNOWN"
    return WRITER_MERGE.get(w, w)


def clean(v):
    s = str(v).strip() if v is not None else ""
    return s if s else None


def slots(*dicts):
    """Line slot numbers present in a Label Studio result (qty_N / item_N / desc_N): 15 in the current layout."""
    n = [int(m.group(2)) for d in dicts for k in d for m in [re.match(r"(qty|item|desc)_(\d+)$", k)] if m]
    return range(1, max(n or [0]) + 1)


def values(result):
    out = {}
    for x in result or []:
        v = x.get("value") or {}
        t = v.get("text") or v.get("choices") or [None]
        out[x["from_name"]] = t[0] if t else None
    return out


def row_from_task(t, existing, root):
    anns = [a for a in t.get("annotations") or [] if not a.get("was_cancelled")]
    if not anns:
        return None, "no annotation"
    ann = max(anns, key=lambda a: a.get("updated_at") or a.get("created_at") or "")
    a = values(ann.get("result"))
    status = clean(a.get("status")) or "Corrected"
    if UNUSABLE.search(status):
        return None, "unusable"
    data = t.get("data") or {}
    page = str(data.get("page") or "1")
    rid = f"{data.get('file')}#p{page}"
    img = str(data.get("image") or "")
    m = re.search(r"[?&]d=(.+)$", img)
    old = existing.get(rid) or {}
    if old.get("image") and os.path.exists(old["image"]):
        image = old["image"]
    elif m:                     # Label Studio serves local files from /srv/ocr (the parent of <root>), e.g. d=scans/review/images/X.png
        cands = [os.path.join(os.path.dirname(root.rstrip("/")), m.group(1)), os.path.join(root, m.group(1))]
        image = next((c for c in cands if os.path.exists(c)), cands[0])
    else:
        image = img
    pred = values((ann.get("prediction") or {}).get("result")) if isinstance(ann.get("prediction"), dict) else {}
    baseline = old.get("baseline")
    if pred and not baseline:
        baseline = {"jt_number": pred.get("jt_number"), "customer": pred.get("customer"), "mps_job_no": pred.get("job_no"),
                    "date": pred.get("date"), "transferred_from": pred.get("transferred_from"), "transferred_to": pred.get("transferred_to"),
                    "line_items": [{"quantity": clean(pred.get(f"qty_{i}")), "item_no": clean(pred.get(f"item_{i}")),
                                    "description": clean(pred.get(f"desc_{i}"))} for i in slots(pred)
                                   if clean(pred.get(f"desc_{i}")) or clean(pred.get(f"qty_{i}"))],
                    "remarks": pred.get("remarks"), "transferred_by": pred.get("transferred_by"),
                    "received_by": pred.get("received_by"), "receiving_stamp": pred.get("receiving_stamp")}
    target = {k2: clean(a.get(k1)) for k1, k2 in HEAD.items()}
    target["line_items"] = [{"quantity": clean(a.get(f"qty_{i}")), "item_no": clean(a.get(f"item_{i}")),
                             "description": clean(a.get(f"desc_{i}"))} for i in slots(a)
                            if clean(a.get(f"desc_{i}")) or clean(a.get(f"qty_{i}"))]
    for k in CARRIED:        # unverified: what the first export carried, else the reader's prediction
        target[k] = (old.get("target") or {}).get(k) if old else (baseline or {}).get(k)
    order = ["jt_number", "customer", "mps_job_no", "date", "transferred_from", "transferred_to", "line_items",
             "remarks", "transferred_by", "received_by", "receiving_stamp"]
    target = {k: target.get(k) for k in order}
    return {"id": rid, "image": image, "writer": writer_name(a.get("writer") or data.get("writer")),
            "book_writer": writer_name(data.get("writer")), "status": status, "set": data.get("set") or "review",
            "prompt": old.get("prompt"), "target": target, "baseline": baseline,
            "_at": ann.get("updated_at") or ann.get("created_at") or ""}, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exports", nargs="+")
    ap.add_argument("--root", default="/srv/ocr/scans")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    tdir = os.path.join(a.root, "training")
    existing, side = {}, {}
    for s in ("train", "test"):
        p = os.path.join(tdir, f"{s}.jsonl")
        if os.path.exists(p):
            for l in open(p, encoding="utf-8"):
                r = json.loads(l); existing[r["id"]] = r; side[r["id"]] = s
    prompt = next((r["prompt"] for r in existing.values() if r.get("prompt")), None)
    if not prompt:
        sys.exit("no prompt found in the existing jsonl files; cannot build rows")
    tasks = {}
    for f in a.exports:
        for t in json.load(open(f, encoding="utf-8")):
            tasks[t["id"]] = t                      # later exports win
    rows, why = {}, collections.Counter()
    for t in tasks.values():
        r, w = row_from_task(t, existing, a.root)
        why[w] += 1
        if r:
            r["prompt"] = prompt
            if r["id"] in rows and rows[r["id"]]["_at"] > r["_at"]:
                why["duplicate task (older kept out)"] += 1
                continue
            rows[r["id"]] = r
    out = {"train": [], "test": []}
    changed = added = 0
    for rid, r in sorted(rows.items()):
        s = side.get(rid) or ("test" if int(hashlib.sha1(rid.encode()).hexdigest(), 16) % 5 == 0 else "train")
        if rid not in existing:
            added += 1
        elif json.dumps(existing[rid]["target"], sort_keys=True) != json.dumps(r["target"], sort_keys=True) or existing[rid]["writer"] != r["writer"]:
            changed += 1
        out[s].append(r)
    gone = [i for i in existing if i not in rows]
    print(f"export tasks {len(tasks)}: {dict(why)}")
    print(f"rows {len(rows)}: new {added}, changed {changed}, unchanged {len(rows) - added - changed}; "
          f"in old files but not in export: {len(gone)} {gone[:5]}")
    print(f"train {len(out['train'])} (was {sum(1 for v in side.values() if v == 'train')}), test {len(out['test'])} (was {sum(1 for v in side.values() if v == 'test')})")
    byw = collections.Counter(r["writer"] for r in rows.values())
    print("writers: " + ", ".join(f"{w} {n}" for w, n in byw.most_common()))
    missing = [r["id"] for r in rows.values() if not os.path.exists(r["image"])]
    if missing:
        print(f"WARNING {len(missing)} images missing, e.g. {missing[:3]}")
    if not a.apply:
        print("dry run - nothing written (add --apply)")
        return
    if gone:
        print(f"keeping the {len(gone)} rows that are not in this export")
        for rid in gone:
            out[side[rid]].append(existing[rid])
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    bdir = os.path.join(tdir, f"backup-{stamp}")
    os.makedirs(bdir, exist_ok=True)
    for s in ("train", "test"):
        p = os.path.join(tdir, f"{s}.jsonl")
        if os.path.exists(p):
            shutil.copy2(p, bdir)
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            for r in sorted(out[s], key=lambda r: r["id"]):
                fh.write(json.dumps({k: v for k, v in r.items() if k != "_at"}, ensure_ascii=False) + "\n")
        os.replace(p + ".tmp", p)
    print(f"written: train.jsonl {len(out['train'])}, test.jsonl {len(out['test'])}; old files in {bdir}")


if __name__ == "__main__":
    main()
