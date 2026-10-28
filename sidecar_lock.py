"""One lock per sidecar for everyone who rewrites it (review F10, 28 Oct): the dashboard's edits, the numbers pass,
the checking-file import, the worker's held-DN recheck and the PC sync (Lock-Sidecar in Sync-OcrOutput.ps1).

    with sidecar_lock.locked(json_path):
        sc = json.load(open(json_path)); ...; atomic write

The lock is <json>.lock, created exclusively (O_EXCL), which Samba honours, so the PC and the box take the same lock.
A lock older than 10 minutes belongs to a crashed holder and is taken over. Waiting longer than `wait` seconds raises
Locked (the caller reports "being synced / edited, try again"). Nothing here touches the sidecar itself.
"""
import contextlib, os, socket, time

STALE_SEC = 600


class Locked(RuntimeError):
    pass


def _try(lock, who):
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{who} {socket.gethostname()} {os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    return True


@contextlib.contextmanager
def locked(json_path, who="box", wait=20.0):
    lock = f"{json_path}.lock"
    until = time.time() + wait
    while not _try(lock, who):
        try:
            if time.time() - os.stat(lock).st_mtime > STALE_SEC:
                os.unlink(lock)
                continue
        except FileNotFoundError:
            continue
        if time.time() > until:
            try:
                holder = open(lock, encoding="utf-8", errors="replace").read().strip()
            except OSError:
                holder = "?"
            raise Locked(f"{os.path.basename(json_path)} is locked by another process ({holder})")
        time.sleep(0.3)
    try:
        yield lock
    finally:
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass
