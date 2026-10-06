# gatekeeper-strict-allowlist

Harden the host-side SSH agent gatekeeper (`deploy/secure_gatekeeper.sh`, installed at
`~/scripts/secure_gatekeeper.sh`) from a metacharacter denylist to a strict allowlist, and
narrow the reachable `journalctl` / `docker logs` / `docker compose logs` options.

Normative source: the "Known debt" paragraph in `deploy/README.md` plus the Engram decision
`TECH DEBT: gatekeeper denylist → allowlist estricta + acotar opciones de journalctl/docker logs`.
Consumer contract (ground truth for every allowed shape): `_construct_command` in
`tools/mcp-oranpi/src/mcp_oranpi/infrastructure/command_runner.py`.

## Specs

**S1 — Strict character allowlist.** The free-form argument branches (`docker logs`,
`journalctl`, `docker compose logs`) must reject any argument string containing a character
outside `A-Za-z0-9_./:@,+-`, space and single quote. Quote from `deploy/README.md`
(verbatim): "Known debt: the argument guard is a character denylist. It must move to a
strict allowlist, and the reachable `journalctl`/`docker logs` options must be narrowed."
Consequence: `$` `` ` `` `\` `;` `|` `&` `<` `>` `(` `)` `*` `?` `[` `]` `{` `}` `~` `!` `"`
and newlines are unreachable.

**S2 — `journalctl` options narrowed.** Only `-u <unit>`, `-n <lines>`, `-p <priority>`,
`--since <time>` and `--until <time>` are reachable. These must be denied:
`--vacuum-time=…`, `--vacuum-size=…`, `--vacuum-files=…`, `--rotate`, `--flush`, `--sync`,
`--root=…`, `--file …`, `-D/--directory`, `-M/--machine`, `-o/--output`, `--list-*`.
`<lines>` is an integer in 1..500 (the client's `validate_line_limit`); `<priority>` is one of
`emerg alert crit err warning notice info debug` or `0..7`; `<unit>` matches
`^[A-Za-z0-9_.@:-]+$`; `<time>` allows words separated by single spaces (for example
`2 hours ago`).

**S3 — `docker logs` / `docker compose logs` options narrowed.** Only `--tail <lines>`,
`--since <time>`, `--until <time>` and a positional name are reachable: exactly one positional
for `docker logs`, zero or one for `docker compose logs`. Every other flag (for example
`--follow`, `--timestamps`, `--details`) and every `--flag=value` form must be denied.

**S4 — No regression on the emitted shapes.** Every command the MCP client can build today must
keep working, with the arguments arriving at the command unchanged (quote-aware). The shapes are
`journalctl [-u UNIT] -n LINES -p PRIORITY [--since TIME] [--until TIME]`,
`docker logs [--since TIME] [--until TIME] --tail LINES CONTAINER`,
`docker compose logs [SERVICE] [--tail LINES]`, plus the untouched exact-match commands
(`docker ps --format json`, `ss -tulnp`, `tail -n N PATH`, `find …`, `stat -c %s PATH`,
`readlink -f PATH`, `systemctl status SERVICE`).

**S5 — Fail closed, never evaluate the argument string.** Any rejected, malformed or
unrecognized argument exits `1` with `Acceso denegado: Comando no autorizado.`. The argument
string is never executed: after the character allowlist (S1), the only processing is quote
removal through `set --`, and each resulting token is validated before the command runs.

**S6 — Documentation.** `deploy/README.md` must stop declaring the debt and state the allowlist
contract, the install step (mode `700`, LF only, backup first, no restart), and the two known
client gaps: an argument-less `journalctl` and an argument-less `docker compose logs` are denied
by the gate, so the MCP client must always send `-n`/`--tail`.

## Tasks

| ID | Specs | Route | Commit |
|----|-------|-------|--------|
| T1 | S1-S5 | tests first (`tests/test_gatekeeper_allowlist.py`): deny cases RED against the denylist, plus one equivalence test per emitted shape. RED observed: 43 failed / 15 passed / 3 skipped | working tree (uncommitted) |
| T2 | S1-S5 | implement `deploy/secure_gatekeeper.sh`: shared `gatekeeper_split_args` + per-branch token validators, `eval` removed from input handling | working tree (uncommitted) |
| T3 | S6 | update `deploy/README.md` (debt → contract + client gaps) | working tree (uncommitted) |
| T4 | S1-S5 | independent verification (`gentle-ai-verify`, read-only, own harness with canaries): S1/S2/S3/S5/S6 PASS, **S4 FAIL** — 3 client-emittable `--since/--until` values with repeated/edge spaces were rejected (the old gate accepted them). Bounded fix + re-verification: **all specs PASS**, one deliberate residual | done |
| T5 | S1-S6 | deploy authorized and executed: backup `secure_gatekeeper.sh.bak-20261006T204602`, pre-install checks (CR=0, `bash -n`), `install -m 700`, post-install verified (md5 `d295c4117c1ab1f3883cc518b6291b79`, CR=0, mode 700) | `2c39af0` (code) + evidence commit |

## Log

**L1 (verbatim, user):** "mira el ancla de Engram y seguimos en orden, de prioridades para este proyecto."

**L2 (verbatim, user choice):** "Cerrar el ancla: limpieza /c/tmp" — closed: `/c/tmp/MCP_Oranpi-retired-20261006T022720`,
`/c/tmp/mcp_smoke_new_path.py`, `/c/tmp/bookgraph-nul-artifact` deleted after verifying no unique
commits, identical tracked content and identical `.env` md5.

**L3 (evidence, 2026-10-06).** Installed host copy is byte-identical to the repo copy:
md5 `d2a41e6c5369d672b65d441e0cdc5cce`, mode `700`, owner `gonzalo`. Ground truth of the reachable
options read from `command_runner.py::_construct_command`. The gatekeeper is exempt from the
live-deployment textual guard by exact path (`tests/test_deploy_artifacts.py`
`_LIVE_DEPLOY_SCAN_EXCLUDES`), so `tests/` fixtures must build the guarded tokens from parts.
`deploy/secure_gatekeeper.sh` is LF (0 CR bytes) and the repo root has no `nul` artifact.

**L4 (checks, 2026-10-06).** Focused suites green: `tests/test_gatekeeper_allowlist.py`
(62 passed, 3 skipped: the compose accept-shapes need the host path), `test_deploy_artifacts.py`
and `test_docs_consistency.py` (103 passed total). Full suite green before the fix round:
2058 passed, 3 skipped. `ruff check .`, `ruff format --check` on the new file, `mypy .` (421 files)
and `scripts/validate_architecture.py` all pass. `bash -n` accepts the script.

**L5 (independent verification, 2026-10-06, `gentle-ai-verify`, read-only).** Verdict S1 PASS
(byte sweep: reachable characters are exactly the spec set), S2 PASS, S3 PASS, S5 PASS (~90
adversarial inputs, 0 escapes, canary commands never executed, all rejects `rc=1` with the exact
message), S6 PASS, **S4 FAIL**.

**L6 (defect found and fixed in the fix round).** The client's date grammar
(`^[a-zA-Z0-9_T:., -]+$`) admits repeated and edge spaces, so `--since '2  hours ago'`,
`--since 'now '` and `|docker logs|` with those values were emittable and accepted by the old
gate but rejected by `RX_TIME`. Fixed by relaxing `RX_TIME` to `^ *[A-Za-z0-9_:.,+-]+( +[A-Za-z0-9_:.,+-]+)* *$`;
the three counterexamples are now permanent cases asserting verbatim argv, plus one deny case for
a whitespace-only value. Re-verification: S1-S6 all PASS. Deliberate residual (asserted as a deny
case): a whitespace-only `TIME` value is client-emittable and gate-denied, because `LINES`/`TIME`
must carry at least one non-space character.

**L7 (pre-existing findings, out of scope, unchanged by this work).** `deploy guardian_rust` is
client-emittable and denied, exactly as before the diff. The untouched `tail -n`, `stat -c %s` and
`readlink -f` branches capture `(.*)` and pass the value **quoted**, so `tail -n 100 /var/log/x; id`
reads a file literally named `"/var/log/x; id"`: no escape, no change in behavior. The
argument-less `docker logs`, `journalctl` and `docker compose logs` templates stay denied; the
application layer always supplies defaults, so only the two documented gaps are real.

**L8 (deploy, 2026-10-06, authorized by the maintainer).** Host before: md5
`d2a41e6c5369d672b65d441e0cdc5cce`, 8875 bytes, mode `700`, CR=0. The repo copy was
transferred with `scp` (binary) to `~/scripts/.gk.new` and verified **before** installing
(md5, CR=0, `bash -n`), then installed with `install -m 700`. Host after: md5
`d295c4117c1ab1f3883cc518b6291b79` (byte-identical to the committed repo copy), 12332
bytes, mode `700`, CR=0, `bash -n` OK. Backup kept at
`~/scripts/secure_gatekeeper.sh.bak-20261006T204602`. No restart was needed (the forced
command runs per new connection); `mcp-server` stayed `active`, MainPID `3085521`,
`NRestarts=0`.

**L9 (post-deploy smoke through the real consumer, 2026-10-06).** Every row was recorded
before and after the install, using the agent key (the MCP's own key, forced through the
gatekeeper):

| Command (agent key) | Before | After |
|---|---|---|
| `journalctl -u mcp-server -n 5 -p info` | allowed, `-- No entries --` | allowed, same output |
| `journalctl -o json -n 1` | **allowed, returned journal JSON** | `Acceso denegado`, exit 1 |
| `docker logs --tail 1 bookgraph-neo4j; id` | blocked | `Acceso denegado`, exit 1 |
| `journalctl -n 100 -p info *` | allowed (glob-expands) | `Acceso denegado`, exit 1 |
| `cd …/Blog_Profesional && docker compose logs --tail 2` | allowed | allowed, same output |

MCP tools after the deploy returned the same data as the pre-deploy baseline:
`logs_systemd`, `docker_container_logs`, `workspace_logs` (the compose branch the local
suite skips) and `network_inspect_bindings`; `workspace_list` still reports 8 workspaces.
The destructive `--vacuum-time=1s` string was deliberately **not** sent to the host: it is
proven denied by the harness through the same `*) return 1` path as `-o json`, which is
denied live.
