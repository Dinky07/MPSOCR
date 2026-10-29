"""Keys that differ only in case ("Ce" and "CE" in one chemistry row) break the PC sync: PowerShell's ConvertFrom-Json
is case-insensitive and refuses the whole sidecar (DOC101123, 28 Oct). dedupe() merges such keys, recursively:
the first non-empty value keeps the key; a second non-empty value is kept as "<key>_2" (then _3 ...)."""


def _empty(v):
    return v is None or v == "" or v == [] or v == {}


def dedupe(o):
    if isinstance(o, list):
        return [dedupe(x) for x in o]
    if not isinstance(o, dict):
        return o
    out, low = {}, {}
    for k, v in o.items():
        v = dedupe(v)
        kl = str(k).lower()
        if kl not in low:
            low[kl] = k
            out[k] = v
            continue
        k0 = low[kl]
        if _empty(v):
            continue
        if _empty(out[k0]):
            out[k0] = v
            continue
        n = 2
        while str(k0).lower() + f"_{n}" in low:
            n += 1
        k2 = f"{k0}_{n}"
        low[k2.lower()] = k2
        out[k2] = v
    return out


def pairs_hook(kv):
    """json.load(..., object_pairs_hook=pairs_hook): sees every key, also the duplicates json.load would drop."""
    out, low = [], {}
    for k, v in kv:
        kl = str(k).lower()
        if kl in low:
            i = low[kl]
            if _empty(v):
                continue
            if _empty(out[i][1]):
                out[i] = (out[i][0], v)
                continue
            n = 2
            while f"{kl}_{n}" in low:
                n += 1
            low[f"{kl}_{n}"] = len(out)
            out.append((f"{out[i][0]}_{n}", v))
            continue
        low[kl] = len(out)
        out.append((k, v))
    return dict(out)


def has_dupes_text(path):
    """True when the JSON file has keys that differ only in case anywhere (exact duplicates count too)."""
    import json
    found = []

    def hook(kv):
        seen = set()
        for k, _ in kv:
            if str(k).lower() in seen:
                found.append(k)
            seen.add(str(k).lower())
        return dict(kv)
    json.load(open(path, encoding="utf-8"), object_pairs_hook=hook)
    return bool(found)
