#!/usr/bin/env python3
"""
MPS NAS collector  (runs on mps-frigate as the systemd service mps-ocr-collector)

Watches the scanner folders on the NAS, which are mounted READ-ONLY, and copies each new scan into
the worker's  <root>/in/<Type>/  queue. The NAS itself is never written to.

Duplicates are prevented entirely on this box, in the same SQLite state DB the worker uses:
- every NAS file is remembered by path + size + modified time, so it is looked at once
- before copying, its SHA-256 is checked against everything already queued or processed, so a
  file that was renamed, moved to another folder or scanned into two places is not processed twice
- a re-scan of the same paper is a new file (different bytes), so it IS processed again

Only files newer than a source's "only_newer_than" date are queued, so turning this on doesn't
re-process the archive. Files are only copied once their size has stopped changing (scanner finished).
"""
import fnmatch, hashlib, json, logging, os, shutil, signal, sqlite3, sys, time
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("MPS_OCR_CONFIG", "/srv/ocr/pipeline/config.json"))
ACCEPT = {".pdf", ".jpg", ".jpeg", ".png"}
log = logging.getLogger("mps-collector")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ccfg = cfg.get("collector", {})
        self.root = Path(cfg["root"])
        self.sources = self.ccfg.get("sources", [])
        self.db = sqlite3.connect(cfg.get("state_db", "/srv/ocr/pipeline/state.db"), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS processed(
            sha256 TEXT PRIMARY KEY, folder TEXT, source TEXT, status TEXT, outputs TEXT, at TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS nas_seen(
            path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT, status TEXT, type_folder TEXT, at TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_nas_seen_sha ON nas_seen(sha256)")
        self.db.commit()
        self.pending = {}     # path -> ((size, mtime), first time that signature was seen)

    def record(self, path, size, mtime, digest, status, folder):
        self.db.execute("INSERT OR REPLACE INTO nas_seen VALUES(?,?,?,?,?,?,?)",
                        (str(path), size, mtime, digest, status, folder, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def already_have(self, digest):
        if self.db.execute("SELECT 1 FROM processed WHERE sha256=? AND status='ok'", (digest,)).fetchone():
            return "processed"
        if self.db.execute("SELECT 1 FROM nas_seen WHERE sha256=? AND status='queued'", (digest,)).fetchone():
            return "queued"
        return None

    def files(self, src):
        base = Path(src["path"])
        pattern = src.get("pattern", "*")
        walker = base.rglob("*") if src.get("recursive", True) else base.glob("*")
        for p in walker:
            if p.is_file() and p.suffix.lower() in ACCEPT and fnmatch.fnmatch(p.name.lower(), pattern.lower()) \
                    and not p.name.startswith(("~$", ".")):
                yield p

    def scan_source(self, src):
        folder = src["type_folder"]
        stable = float(self.ccfg.get("stable_seconds", 60))
        cutoff = datetime.fromisoformat(src["only_newer_than"]).timestamp() if src.get("only_newer_than") else 0
        if not Path(src["path"]).is_dir():
            log.warning("source not reachable (NAS mounted?): %s", src["path"])
            return "unreachable", 0
        queued = 0
        for p in self.files(src):
            if _stop:
                break
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            row = self.db.execute("SELECT size, mtime FROM nas_seen WHERE path=?", (str(p),)).fetchone()
            if row and (row[0], row[1]) == sig:
                continue                                   # already handled this exact file
            if st.st_mtime < cutoff:
                self.record(p, *sig, None, "before_cutoff", folder)
                continue
            prev = self.pending.get(p)
            if not prev or prev[0] != sig:
                self.pending[p] = (sig, time.time())       # new or still changing: wait
                continue
            if time.time() - prev[1] < stable or time.time() - st.st_mtime < stable:
                continue
            self.pending.pop(p, None)
            try:
                digest = sha256(p)
            except OSError as e:
                log.warning("can't read %s: %s", p, e)
                continue
            dup = self.already_have(digest)
            if dup:
                log.info("skip (%s already): %s", dup, p)
                self.record(p, *sig, digest, "duplicate", folder)
                continue
            dst_dir = self.root / "in" / folder
            dst = dst_dir / p.name
            n = 2
            while dst.exists():
                dst = dst_dir / f"{p.stem}_{n}{p.suffix}"
                n += 1
            tmp = dst.with_name(dst.name + ".part")          # worker ignores .part files
            shutil.copyfile(p, tmp)
            os.replace(tmp, dst)
            self.record(p, *sig, digest, "queued", folder)
            log.info("queued %s -> %s/%s", p, folder, dst.name)
            queued += 1
        return "ok", queued

    def write_status(self, per_source):
        st = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "pid": os.getpid(),
              "sources": per_source}
        tmp = self.root / "collector_status.json.tmp"
        try:
            tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
            os.replace(tmp, self.root / "collector_status.json")
        except OSError as e:
            log.warning("status write failed: %s", e)

    def loop(self):
        if not self.sources:
            log.error("no collector.sources in %s; nothing to watch", CONFIG_PATH)
            return
        poll = float(self.ccfg.get("poll_seconds", 60))
        log.info("watching %s", ", ".join(f"{s['path']} -> {s['type_folder']}" for s in self.sources))
        per_source = {}
        while not _stop:
            flags = self.root / "flags"
            for src in self.sources:
                key = src["type_folder"]
                prev = per_source.get(key, {})
                info = {"path": src["path"], "state": "ok", "last_scan": prev.get("last_scan"),
                        "last_queued_at": prev.get("last_queued_at"), "queued_total": prev.get("queued_total", 0)}
                # dashboard switches: flags/collect_off stops all, flags/collect_off_<Type> stops one type
                if (flags / "collect_off").exists() or (flags / f"collect_off_{key}").exists():
                    info["state"] = "off"
                    per_source[key] = info
                    continue
                try:
                    state, n = self.scan_source(src)
                    info["state"] = state
                    info["last_scan"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    if n:
                        info["last_queued_at"] = info["last_scan"]
                        info["queued_total"] += n
                except OSError as e:               # NAS dropped mid-scan: try again next poll
                    log.warning("scan of %s interrupted: %s", src["path"], e)
                    info["state"] = f"error: {e}"[:200]
                per_source[key] = info
            self.write_status(per_source)
            for _ in range(int(poll)):
                if _stop:
                    break
                time.sleep(1)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    Collector(cfg).loop()


if __name__ == "__main__":
    main()
