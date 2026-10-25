# Public bridge/Pi runtime

Buildable source for a small Linux container, not a pre-published registry image.
It uses only public base images/packages and this repository. It does not import
internal runtime layers, agent roles, deployment configuration or workspaces.

## Included and deliberately absent

- Digest-pinned Python 3.12 and Node 24 bases; Pi **0.99.1**, matching this
  repository's tested SDK baseline, installed with an npm lockfile.
- Bridge, exported extensions and skills at `/opt/pi-chat`. Python dependencies,
  Supervisor, uv and **Python Playwright 1.64.1** are hash-locked.
- Git (HTTPS), curl and tmux. No SSH client/server, sudo, browser binaries,
  browser service, Chroma, Redis, Docker daemon or systemd.
- Supervisor manages `bridge`; the bridge starts Pi in RPC mode on demand and
  owns its stdin/stdout, checkpointing, process groups and shutdown. There is
  intentionally no second independently supervised Pi daemon.
- Tini forwards termination to Supervisor and reaps orphans. Supervisor forwards
  SIGTERM to the bridge, which stops its separately grouped Pi children.

The default profile disables automatic extensions, skills and context-file
loading. To enable selected exports, edit `pi_extra_args` explicitly, for example
`["--offline", "--no-extensions", "--no-skills", "--no-context-files",
"--extension", "/opt/pi-chat/extensions/attention-bell.ts"]`.
Pi still loads explicitly selected extensions when `--no-extensions` is set.
Configure the extension's dependencies and credentials separately; pre-installing
source does not configure a provider or enable every exported tool.

## Private configuration and persistent state

Run these commands as a **non-root** Linux user, from the repository root:

```sh
export PI_CHAT_UID="$(id -u)" PI_CHAT_GID="$(id -g)"
export PI_CHAT_STATE="$HOME/.local/share/pi-chat-container/state"
export PI_CHAT_WORKSPACE="$HOME/.local/share/pi-chat-container/workspace"
export PI_CHAT_PROFILE="$HOME/.config/pi-chat-container/profile.json"
export PI_CHAT_ENV_FILE="$HOME/.config/pi-chat-container/runtime.env"
install -d -m 700 "$PI_CHAT_STATE" "$PI_CHAT_WORKSPACE" "$(dirname "$PI_CHAT_PROFILE")"
install -m 600 runtime/profile.example.json "$PI_CHAT_PROFILE"
install -m 600 /dev/null "$PI_CHAT_ENV_FILE"
# Edit the profile and environment file privately before starting.
docker compose -f runtime/compose.yaml build
docker compose -f runtime/compose.yaml up -d
```

The sample uses Matrix placeholders; replace the account, allowed rooms,
provider and model. Set `MATRIX_TOKEN=...` in the private environment file; do not
put the token in the profile. Discord can be configured using the main bridge
examples instead. No real account, provider, room or credential is supplied.

For provider authentication, either provision Pi's private auth under
`$PI_CHAT_STATE/pi`, or put the provider's key in the environment file and list
its variable name in `pi_pass_env`. Transport credentials must **not** be in
`pi_pass_env`. See [installation](../docs/installation.md) for the environment
allowlist and other profile settings. Provider use may incur charges; this
container grants no spending permission and no model is contacted at startup.

The runtime UID must own the mode-0600 profile and private directories. Newly
created private directories use mode 0700. Startup refuses root, insecure
permissions and inaccessible workspaces; it never chowns caller data. If using
UID/GID 1000 directly, ensure bind mounts already have matching ownership. The
Compose example selects your UID/GID instead and refuses missing bind sources.

`/state/home`, `/state/pi` and `/state/bridge` persist separately from `/workspace`.
Back up `/state` privately: it holds auth, sessions, transcripts and transport
state. Mount only a dedicated workspace, never your entire home, SSH directory,
Docker socket, host device tree or another agent's state. Build from a clean
public checkout; `.dockerignore` default-denies unrelated context, but is not a
secret scanner for files deliberately placed in allowed source directories.

## Operate and verify

```sh
docker compose -f runtime/compose.yaml exec bridge supervisorctl -c /opt/pi-chat/runtime/supervisord.conf status
docker compose -f runtime/compose.yaml exec bridge python /opt/pi-chat/runtime/healthcheck.py
docker compose -f runtime/compose.yaml exec bridge pi --version
docker compose -f runtime/compose.yaml logs --tail 50 bridge
docker compose -f runtime/compose.yaml stop
```

Docker health checks both `/health/live` and `/health/ready` on the private Unix
socket. This proves local bridge startup, **not** Matrix/Discord connectivity,
provider availability or successful inference. Docker does not restart unhealthy
containers automatically. Supervisor retries unexpected bridge exits; a process
that repeatedly fails during startup becomes FATAL. Restart policy is deliberately
`no`; inspect the error before restarting. `stop` allows 60 seconds for graceful
shutdown; active work can be interrupted, so stop when idle where possible.

Compose drops all capabilities, enables no-new-privileges, makes the root
filesystem read-only and leaves only `/tmp`, `/state` and `/workspace` writable.
It publishes no ports and does not use host networking. Outbound networking is
still available to the bridge, Pi and tools. **This is not a hostile-code or
credential-isolation sandbox**: processes share one UID, Pi can access mounted
state and same-UID files, Docker administrators can inspect environment variables,
and logs/resolved Compose configuration may disclose private data. Do not publish
logs, state or `docker compose config` output.

Python Playwright can connect to an explicitly configured external browser via
CDP, or a Playwright server. There is no default endpoint, exposed debugging port
or browser download. A Playwright server must use a compatible version. Keep its
access private; browser control can expose authenticated sessions. Local browser
launches will fail until you separately supply a suitable browser and libraries.

The systemd background-job extension is **unsupported** in this ordinary
container. Supervisor is not a systemd replacement for that extension; do not
add privileged mode or mount a host user bus to bypass the limitation.

## Tests and dependency updates

`python -m unittest discover -s tests -v` runs unit contracts and, when Linux
Docker is available, builds the image and tests non-root startup, read-only
operation, a synthetic Matrix/real-Pi offline turn, installed Pi RPC contracts,
health, shutdown, persistence, Playwright-without-browser and rejected root/public
profiles. CI requires a working Docker daemon; no inference provider is called.
Without Docker the lifecycle class is explicitly skipped. Linux/amd64 is tested;
other architectures/operating systems are not verified.

Update Python locks from the repository root with:
`uv pip compile runtime/requirements.in --python-version 3.12 --generate-hashes --output-file runtime/requirements.lock --no-emit-index-url`.
Update Pi's manifest and npm lock together, keep the SDK baseline aligned, then
rerun the build/lifecycle and existing extension contracts. Base digests and apt
packages need periodic security review; pinned versions are not a claim that an
image is free of vulnerabilities. No image publication or live deployment is
performed by these tests.
