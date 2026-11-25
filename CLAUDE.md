# OCR pipeline on mps-frigate

<!-- claude-box-rules:start -->
## Claude Code on this box: house rules (25 Nov 2026)

Claude Code runs here as dinoc, started with `claude remote-control` in /srv/ocr/pipeline, and Dino approves its
commands from the Claude app. The cloud "MPS Business Database" chat also changes this box, through the runner
(`_src/run/NNN-*.sh`, logs in `_src/run/done/`). The same rules hold for both.

### Secrets
- Never read, print, copy or pass on a secret: the `/etc/mps-*.cred` files, `sqlpass.dat`, the Label Studio token,
  the dashboard password hash, `~/.claude/.credentials.json`, SSH keys, git credentials.
- `~/.claude/settings.json` blocks them from the file tools and from `cat`/`head`/`tail`, and makes `grep` ask.
  Don't work around it: no recursive grep over config folders, no script that dumps a config or the environment.
- To check a secret is in place, test the file (`test -s <file>`) or count the key (`grep -c '"<key>"' <file>`);
  never show the value. Passwords are typed by Dino, never by Claude.

### Every change to a live file (code, config, compose files, units and timers, models)
1. Look first: no script waiting in `_src/run/` that touches the same file, and no PAUSE held by someone else
   (training, a test, a runner script). Never remove a PAUSE you didn't create.
2. `md5sum` the file and note it.
3. Back it up beside itself: `cp -p <file> <file>.bak-cc<yyyymmdd-HHMM>`.
4. Change it, then check it: `python3 -m py_compile` (Python), `bash -n` (shell), `docker compose config -q` (compose).
5. If running services use it: pause the workers and wait until idle, restart only what needs it
   (`sudo -n systemctl restart <unit>`), and watch for 2-3 minutes (`journalctl -u <unit> --since "3 min ago"`, the
   dashboard). On any error put the `.bak` back, restart, and say so. The newest scripts in `_src/run/done/` are the
   template for pause, idle, restart, watch and rollback.
6. `md5sum` again, then commit just those files: `git add <files> && git commit -m "<what and why>"`.
   One change, one commit. Report the old md5, new md5, backup name and commit to Dino. Keep the `.bak`.

### Never without Dino's explicit yes for that exact step
- Deleting anything: move it to a `_moved/<date>/` subfolder instead.
- SharePoint or SQL loads, or any `-Apply` against them: Dino runs those.
- Anything that can't be undone: disks and mounts, users and sudoers, firewall, `apt` upgrades, `docker system prune`,
  removing images or models, git push, branch changes, history rewrites.
- Starting training or loading another GPU model while a PAUSE is held or a test is running (16 GB RAM, tight).
<!-- claude-box-rules:end -->
