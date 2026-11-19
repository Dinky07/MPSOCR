# MPS OCR pipeline (mps-frigate)

The code in `/srv/ocr/pipeline` on mps-frigate: scanned documents from the NAS are read (CPU first, then the
vision model through Ollama), checked on the web dashboard, and handed to the PC sync, which puts them in SharePoint
and SQL. This repository is that folder. The pipeline's data (`/srv/ocr/scans/pipeline`: in, out, synced, deleted,
flags, state.db) is not in it, and neither are the PC sync scripts, the SQL build or the SharePoint build scripts.

## Where things are

| | |
|---|---|
| `collector.py`, `worker.py` | scans in, documents out; one reader per document type (`po_reader.py`, `jt_reader.py`, `tc_reader.py`, `ws_reader.py`, `dn_reader.py`, `co_reader.py`, `rn_reader.py`, `pop_reader.py`, `cpu_invoice.py`) |
| `dashboard.py` | the dashboard on http://mps-frigate:8090 (one file: server and page) |
| `config.json` | document types, sources, switches. No secrets: the dashboard password's hash is in `dashboard_password.hash`, which git ignores |
| `units/` | copies of the systemd units installed on the box |
| `ops/` | scripts run by hand on the box or the PC, dry run unless `--apply` / `-Apply` |
| `tests/` | `python3 -m pytest tests` (no GPU, no box needed) |

## How a change gets to the box

1. A branch and a pull request on GitHub. The `tests` check runs on it: every `.py` compiles, the dashboard page's
   script parses, the tests pass.
2. Merge the pull request.
3. On mps-frigate, as dinoc: `/srv/ocr/pipeline/ops/deploy.sh` shows what would come in and what would restart;
   `--apply` does it. It commits what is uncommitted on the box first, merges, restarts the dashboard (and the
   collector) when their code changed, and pushes the result. The two workers are restarted only with
   `--restart-workers`.

The box also changes by itself (runner scripts, rules and supplier lists the pipeline learns, config switches).
`commit.sh` commits that at the end of every runner script and once an hour, and pushes it to GitHub and to the
mirrors (`box`, and `origin` on the NAS). It never brings GitHub's commits in. When the box and GitHub each hold
commits the other lacks, nothing can be pushed until the next deploy: `commit.sh` then leaves `flags/alert_git`,
which shows on the dashboard's Alerts card, and the overview's "Deployed" line says how many commits are waiting
either way.

## Rules that are easy to break

- `worker.py` on the box carries steps added there (the upright step, the JT copy field, the PaddleOCR hint).
  Start every change from the current file, never from an older copy.
- Readers' prompts and models change only when asked for, with a before / after measurement (`regression.py`).
- Nothing that identifies a person or holds a credential goes in the repository: no passwords, no `.cred` files,
  no scans. `.gitignore` keeps data (`*.csv`, `state.db`, training sets, example images) out.
- Runner scripts in `O:\pipeline\_src\run` are numbered; look for the highest number in `run\` and `run\done\`
  before picking the next.
