# SSH execution and delivery

Use the same AEF modules on the selected host. This skill supplies an operating
procedure, not a remote scheduler or authorization to use a remembered server.

## Inspect the real execution host

Obtain the user's SSH alias/account, remote checkout and Python executable,
output/state/staging roots and intended action. Use existing SSH configuration;
do not copy private keys or disable host-key checking. Resolve first-use trust
through the user's normal SSH process. Connection to a port is not SSH success.
For an authorized host inspection, use bounded read-only commands, for example:

```text
ssh -o BatchMode=yes -o ConnectTimeout=10 HOST_ALIAS "hostname; id; uptime; free -b"
```

Then inspect `/proc/self/mountinfo`, relevant link targets and D-state processes
before touching suspect mounts. Check the resolved output/state/resource volumes,
available bytes, permissions, the selected interpreter/dependencies, repo dirty
state and active task identities. Do not use a literal Windows `D:` path on Linux
or assume interactive conda activation applies to noninteractive SSH.
Use separate commands or a reviewed remote script; do not interpolate untrusted
file names into shell code. Do not print full process environments/credentials.

`findmnt -T`, `df`, doctor and recursive walks can themselves block on an unhealthy
filesystem. If known NTFS-related D-state or I/O failures exist, avoid new probes
on that mount, stop new submissions and request administrator recovery. A timeout
does not make an uninterruptible kernel I/O process killable. The repo's mount
lookup is lexical, so verify symlink targets and actual mounts independently.

## Source and credentials

- Check remote `git status`, HEAD, package imports and dirty source hashes. Do
  not overwrite a dirty deployment or assume an old wheel contains local changes.
  Deploy only if requested; use a separate checkout/versioned directory when needed.
- Use an absolute Python path with `-u -m scripts.stream_aef_grid_ee` (or points).
  Run `--help` and offline checks on that host; local success does not validate it.
- Credentials must belong to the selected remote account. Run existing discovery
  and, when authorized, `auth_cli verify --check-aef` there. Local browser login
  does not automatically authorize a different machine.
- If missing, stop for an explicit login choice. Headless Earth Engine login can
  deliberately use `--auth-mode gcloud` or `notebook`; attached/admin-provided ADC
  is another option. Reuse `docs/authentication.md`, not a guessed `localhost`
  callback on an inaccessible server. No credential JSON committed or passed in
  chat, and no unattended process spawning an interactive login.

## Storage, concurrency and persistence

- Prefer healthy ext4/XFS for active data and all control/log/resource files.
  For Linux NTFS delivery, native `--state-dir` **and** `--staging-dir` are
  required; use the existing sequential commit mechanism. Never put PID/log files
  on the suspect volume. TIFF output to Linux NTFS is unsupported in this version.
- Set an explicit shared `--resource-state-dir` for cooperating jobs and use its
  existing global-request limit. Begin with one streamer per host; the grid CLI
  already processes its list sequentially. CLI request tokens and delivery leases
  are not a same-task write lock or shared memory admission. Sum concurrent job
  budgets explicitly before adding processes.
- Ordinary detached execution may use the host's existing user service, tmux or
  reviewed `nohup` procedure. Reuse what is installed; no new daemon required.
  Record command, interpreter, work directory, job identity/PID, logs and native
  state directory. A short local SSH command timeout must not be mistaken for
  the remote job's lifetime or exit code.
- Keep an exit receipt from the actual child command. Use bounded retries;
  avoid `Restart=always`. Grid per-item exceptions can allow the next grid to
  start; use one-grid task invocations with separate state/output when filesystem
  fault containment is required. Do not share a root catalog across them.
- After submission, verify the process identity and first real progress. During
  monitoring report UTC timestamp, stage, new committed blocks/shards, remaining
  count and observed rate. Explain retry/replay traffic; do not infer MB/s from
  blocks/s without actual response bytes. API deadlines do not bound `ee.Initialize`.
- Cancellation/recovery must target the recorded task, not every Python process.
  Wait for the writer to exit, preserve checkpoint/report/logs, verify files and
  identity, then resume the same command. A storage incident is not a reason for
  repeated `rm`, more retries, deleting locks or changing mount options.

## Transfer only if requested

Remote download means the files are on the server. It does not copy them to the
user's laptop. Establish the canonical source and target, healthy space on both,
and a whitelist including data **and** control/catalog metadata before transfer.
Wait for the store to be stable. For rsync, inspect a dry-run and use recoverable
ordinary copying without `--delete`; do not use `--append-verify` for mutable
Zarr. Use available trusted transport; do not install another transfer stack
just to execute a generic example.

After transfer compare file counts/bytes and the required hash coverage, fix
absolute catalog paths in a destination-specific copy if needed, and perform
actual destination reader checks. Structural success, complete finite data and
source coverage are separate. Preserve the source until agreed verification is
complete; cleanup is a distinct authorized operation.

Remote GUI access is optional: if requested, bind its server to loopback and use
an SSH tunnel such as `ssh -L 5556:127.0.0.1:5555 HOST_ALIAS`, then open the local
forwarded address. It still writes to the server, not the browser's computer.
Do not publicly expose the trusted-local GUI as a shortcut for agent operation.
