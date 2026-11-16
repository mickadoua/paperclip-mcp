# paperclip-mcp

An MCP bridge to a [Paperclip](https://github.com/paperclipai/paperclip) instance:
its companies, its issues and whatever awaits a human decision, exposed to a remote
MCP client (claude.ai, Claude Code, any compatible client).

[![48-second walkthrough](docs/demo.jpg)](https://mickadoua.github.io/paperclip-mcp/)

A 48-second walkthrough is on the [project page](https://mickadoua.github.io/paperclip-mcp/).

## Why not the official MCP server

`@paperclipai/mcp-server` speaks stdio and exposes everything, approvals included,
plus free access to the API (`paperclipApiRequest`). This bridge exposes a closed
set of tools, each one subject to a policy.

## Policy per tool

`policy.yaml` gives each tool a mode:

| Mode | Effect |
|---|---|
| `allow` | the tool runs |
| `ask` | the server asks for confirmation through MCP elicitation; a client that cannot ask is refused |
| `deny` | the tool is not exposed (default for any tool missing from the file) |

Decision tools (`issue_update`, `approval_decision`, `interaction_respond`,
`plan_confirm`, `action_request_decision`, `agent_resume`, `routine_run`) only
accept `ask` or `deny`: otherwise the server refuses to start.

`issue_update_without_confirmation` in `policy.yaml` lists the `issue_update`
changes that run directly: assignment (`assign: true`) and the listed statuses.
Any other change asks for confirmation. Most clients cannot ask yet, so those
changes are refused there.

The Paperclip token is a board token, with every board right: this server is the
only thing that restricts them.

## Tools

| Tool | Effect |
|---|---|
| `companies` | companies, issue prefix, id |
| `attention` | what awaits a human, across companies |
| `issues` / `issue` / `document` | issues, detail, comments, documents, pending cards |
| `agents` | agents of a company |
| `approvals` / `action_requests` | pending approvals and tool actions |
| `issue_comment` | comment on an issue |
| `issue_create` | create an issue, always unassigned, optionally in a project |
| `issue_update` | assign an issue (agent or `me`) and/or change its status, optional comment; changes outside `issue_update_without_confirmation` ask for confirmation |
| `projects` / `project_create` | list a company's projects, create one (optional GitHub repository) |
| `runs` / `run` | runs in progress across companies, an agent's last runs, a run's status, error and last agent messages |
| `issue_history` | what happened on an issue: status changes, assignments, comments, runs |
| `spend` | tokens and runs per agent over the last days (subscription runs cost 0 cents) |
| `activity` / `routines` | latest events of a company, its recurring routines |
| `agent_pause` / `agent_wake` | pause an agent (cancels its run in progress), wake it without a comment |
| `agent_unblock` | clear an agent's error so it can run again |
| `run_stop` | stop a run in progress |
| `approval_comment` | comment on an approval without deciding it |
| `agent_resume` / `routine_run` | resume a paused agent, run a routine now; ask for confirmation |
| `approval_decision` | approve or reject an approval |
| `interaction_respond` | answer a question card |
| `plan_confirm` | accept or reject a plan |
| `action_request_decision` | authorize or decline a suspended tool action |
| `sessions` | Claude Code sessions outside Paperclip (CLI, desktop...) seen in the last minutes, fed by a hook |
| `nas` | state of the machine that hosts Paperclip: CPU, load, memory, ZFS pools, disk space, Docker containers; read-only |
| `dependencies` | what waits on what, across companies: blocked issues, the issues they wait on, who holds each one and who must unblock it; read-only |

## Claude Code sessions outside Paperclip

`hooks/qg-ping.py` is a Claude Code hook that tells the bridge a session is alive:
session id, working folder name, event, tool name, host, entry point. Never the
prompt, the tool input or a file. It answers at once and posts from a detached
process, so an unreachable bridge never slows Claude Code down. The bridge keeps
the sessions in memory for `SESSIONS_TTL` seconds (default 1800) and `sessions`
lists them.

On each machine:

1. Copy `hooks/qg-ping.py` to `~/.claude/hooks/`.
2. Merge `hooks/settings.example.json` into `~/.claude/settings.json`.
3. Create `~/.config/qg-paperclip.env` (`chmod 600`):
   ```
   QG_URL=https://<bridge hostname>/sessions/event
   CF_ACCESS_CLIENT_ID=...
   CF_ACCESS_CLIENT_SECRET=...
   QG_TOKEN=...
   ```

The route sits behind the same Cloudflare Access check as `/mcp`: create an Access
service token and add a Service Auth policy for it on the bridge application.
`QG_TOKEN` must equal the bridge's `SESSIONS_INGEST_TOKEN`; when that variable is
set, events without it are refused.

## Dependencies

`dependencies` lists, per company, the open issues that are blocked, wait on other
issues or are assigned to a user, plus the issues they wait on:

```
## DM-Consulting (3)
DMC-482 [blocked] by=Sacha after=DMC-474 waits=- since=2026-11-09 project=Templates_(template-site,_template-android) · template-site : recopier les modèles légaux validés
DMC-474 [in_review] by=user after=- waits=- since=2026-11-12 project=Templates_(template-site,_template-android) · Modèles de textes légaux et de fiche store
DMC-110 [blocked] by=Sacha after=- waits=board since=2026-11-02 project=- · Brancher l'API Google Search Console
  awaited: Créer la clé dans la console Google
```

`by` is the assignee (an agent's name, `user`, or `-`), `after` the issues it waits
on, `waits` who must unblock it (`board`, `user`, an agent's name, or `-`), `since`
the day it got blocked, or last changed when it is not blocked, `project` the name of
the issue's project with spaces turned into `_` (`-` when it has none). An issue that blocks
another one is listed even when it is closed, so every `after` has its own line.

## Host state

`nas` describes the machine the bridge runs on. It never writes and never calls
Paperclip:

```
host name=nas cpu=12% cores=4 load=0.42,0.38,0.35 mem=5.6/15.5GiB mem_pct=36% arc=6.0GiB temp=44C uptime=12d4h
pool tank [ONLINE]
disk tank used=2.4TiB free=1.2TiB total=3.6TiB pct=67%
containers running=18 total=20 unhealthy=0 source=docker
container paperclip [running] healthy · Up 3 days · stack=paperclip · image=ghcr.io/paperclipai/paperclip:latest
```

- **Host line**: `/proc` of the host, which a container sees as is. No mount and no
  privilege needed. `mem` leaves out the ZFS cache, reported as `arc`.
- **Pools**: ZFS pool states from `/proc/spl/kstat/zfs`, when the host runs ZFS.
- **Disks**: `statvfs` on each path of `NAS_DISK_PATHS`. To measure a volume, mount
  any folder of it read-only in the bridge container, an empty one is enough. On ZFS
  `statvfs` describes one dataset: `free` is the pool's free space, `used` only what
  that dataset holds. For the pool's fill rate, add its usable size after `@`
  (USED + AVAIL of its root dataset in `zfs list`): `tank=/probe/tank@7.2T`.
- **Containers**: one `GET /containers/json?all=1` on Docker, through the first of
  these that is set. Without any, the line says `source=none`.

| Source | Setting | What the bridge can reach |
|---|---|---|
| a socket proxy that only lets the list through, recommended | `NAS_DOCKER_URL=http://dockerproxy:2375` | the container list, nothing else |
| the Docker socket mounted in the bridge | `NAS_DOCKER_SOCKET=/var/run/docker.sock` | the whole Docker API, so the host: avoid it on a bridge reachable from outside. A `:ro` mount does not restrict the API |
| Portainer | `PORTAINER_URL`, `PORTAINER_API_KEY` | whatever the key's user can do in Portainer |

A proxy limited to the list, here [wollomatic/socket-proxy](https://github.com/wollomatic/socket-proxy),
on a network shared with the bridge only:

```yaml
services:
  dockerproxy:
    image: wollomatic/socket-proxy:<version>
    restart: unless-stopped
    user: "65534:<docker group id>"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges]
    command:
      - '-listenip=0.0.0.0'
      - '-allowfrom=paperclip-mcp'
      - '-allowGET=/containers/json'
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [dockerproxy]
networks:
  dockerproxy:
    internal: true
```

In Paperclip, a comment only wakes the assigned agent, and an issue assigned to a
user wakes nobody. To make an agent act, `issue_update` assigns it, sets the status
to `todo` and adds a comment saying what to do.

## Configuration

| Variable | Role |
|---|---|
| `PAPERCLIP_API_URL` | default `http://paperclip:3100` |
| `PAPERCLIP_API_KEY` | dedicated board token |
| `POLICY_FILE` | default `/app/policy.yaml` |
| `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` | Cloudflare Access JWT validation, required |
| `MCP_ALLOWED_HOSTS` | public hostname, otherwise the SDK answers 421 |
| `MCP_INSECURE_NO_AUTH=1` | local test without Cloudflare only |
| `SESSIONS_INGEST_TOKEN` | shared secret the session hook sends (`X-QG-Token`), recommended |
| `SESSIONS_TTL` | seconds a silent session stays listed, default 1800 |
| `NAS_NAME` | name shown for the host, default `nas` |
| `NAS_DISK_PATHS` | paths measured by `nas`: `label=path`, comma-separated, optional `@size`; default `docker=/` |
| `NAS_DOCKER_URL` / `NAS_DOCKER_SOCKET` | Docker API for the container list: an HTTP address (socket proxy) or a socket path |
| `PORTAINER_URL` / `PORTAINER_API_KEY` | container list through Portainer instead; `PORTAINER_ENDPOINT_ID` (default: every Docker environment), `PORTAINER_INSECURE_TLS=1` for a self-signed certificate |
| `NAS_PROC` / `NAS_SYS` | where to read the host's `/proc` and `/sys`, default `/proc` and `/sys` |

Without `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD`, the server refuses to start.

## Run

```sh
docker build -t paperclip-mcp .
docker run -e PAPERCLIP_API_KEY=... -e CF_ACCESS_TEAM_DOMAIN=... -e CF_ACCESS_AUD=... \
  -v ./policy.yaml:/app/policy.yaml:ro -p 9200:9200 paperclip-mcp
```

The MCP endpoint is `/mcp`, the health check `/healthz`.

## License

MIT
