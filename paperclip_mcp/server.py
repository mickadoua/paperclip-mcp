"""MCP bridge to Paperclip.

Exposes companies, issues and whatever awaits a human decision to a remote MCP
client (claude.ai, Claude Code, any compatible client), through the Paperclip
REST API.

Each tool has a mode in policy.yaml: `allow` runs, `ask` requires the user's
confirmation through MCP elicitation, `deny` is not exposed.
An `ask` tool called by a client that cannot ask for confirmation is refused:
the action never runs without an explicit yes.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import jwt
import yaml
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

API = os.environ.get("PAPERCLIP_API_URL", "http://paperclip:3100").rstrip("/") + "/api"
TOKEN = os.environ.get("PAPERCLIP_API_KEY", "")
POLICY_FILE = os.environ.get("POLICY_FILE", "/app/policy.yaml")
TIMEOUT = float(os.environ.get("PAPERCLIP_TIMEOUT", "60"))

CF_TEAM = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
ALLOWED_HOSTS = os.environ.get("MCP_ALLOWED_HOSTS", "")
ALLOWED_ORIGINS = os.environ.get("MCP_ALLOWED_ORIGINS", "")
SESSIONS_TOKEN = os.environ.get("SESSIONS_INGEST_TOKEN", "")
SESSIONS_TTL = int(os.environ.get("SESSIONS_TTL", "1800"))

NAS_NAME = os.environ.get("NAS_NAME", "nas").strip().replace(" ", "_") or "nas"
NAS_DISK_PATHS = os.environ.get("NAS_DISK_PATHS", "docker=/")
NAS_DOCKER_URL = os.environ.get("NAS_DOCKER_URL", "").strip().rstrip("/")
NAS_DOCKER_SOCKET = os.environ.get("NAS_DOCKER_SOCKET", "").strip()
PORTAINER_URL = os.environ.get("PORTAINER_URL", "").strip().rstrip("/")
PORTAINER_KEY = os.environ.get("PORTAINER_API_KEY", "")
PORTAINER_ENDPOINT = os.environ.get("PORTAINER_ENDPOINT_ID", "")
PORTAINER_INSECURE = os.environ.get("PORTAINER_INSECURE_TLS") == "1"
NAS_PROC = os.environ.get("NAS_PROC", "/proc").rstrip("/")
NAS_SYS = os.environ.get("NAS_SYS", "/sys").rstrip("/")

LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

MODES = {"allow", "ask", "deny"}
SAFE_UPDATE: dict[str, Any] = {"assign": False, "statuses": set()}

logging.getLogger("httpx").setLevel(logging.WARNING)


def _split(raw: str) -> list[str]:
    return [v.strip() for v in raw.split(",") if v.strip()]


def _hosts(raw: str) -> list[str]:
    out = []
    for h in _split(raw):
        out.append(h)
        if ":" not in h:
            out.append(f"{h}:*")
    return out


def load_policy(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    policy = {}
    for tool, mode in (raw.get("tools") or {}).items():
        if mode not in MODES:
            raise SystemExit(f"policy.yaml: unknown mode for {tool}: {mode!r}")
        policy[tool] = mode
    safe = raw.get("issue_update_without_confirmation") or {}
    SAFE_UPDATE["assign"] = bool(safe.get("assign"))
    SAFE_UPDATE["statuses"] = set(safe.get("statuses") or [])
    return policy


mcp = MCPServer(name="paperclip", instructions=__doc__)
_client: httpx.AsyncClient | None = None


async def _call(method: str, path: str, **kw: Any) -> Any:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=API, timeout=TIMEOUT, headers={"Authorization": f"Bearer {TOKEN}"}
        )
    r = await _client.request(method, path, **kw)
    if r.status_code >= 400:
        raise ToolError(f"Paperclip {method} {path} -> {r.status_code} : {r.text[:300]}")
    return r.json() if r.content else {}


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    for key in ("items", "data", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    return next((v for v in data.values() if isinstance(v, list)), [])


async def _company_ids(company: str) -> list[tuple[str, str]]:
    """All companies if `company` is empty, otherwise the one whose name, issue
    prefix or id matches."""
    rows = _items(await _call("GET", "/companies"))
    if not company:
        return [(c["id"], c["name"]) for c in rows]
    key = company.strip().lower()
    match = [
        (c["id"], c["name"])
        for c in rows
        if key in (c["id"].lower(), c["name"].lower(), str(c.get("issuePrefix", "")).lower())
    ]
    if not match:
        raise ToolError(f"Unknown company: {company}. See the companies tool.")
    return match


class Confirmation(BaseModel):
    confirm: bool


async def _confirm(ctx: Context, summary: str, tool: str = "") -> str | None:
    """None if the user confirmed, otherwise the reason for refusing. A tool of
    ALLOWABLE set to allow by the policy runs without asking, logged."""
    if tool and ACTIVE.get(tool) == "allow":
        print(f"[paperclip-mcp] {tool} allowed by policy, no confirmation: {summary!r}", flush=True)
        return None
    caps = ctx.client_capabilities
    if not caps or not getattr(caps, "elicitation", None):
        print(f"[paperclip-mcp] refused, client without elicitation: {summary!r}", flush=True)
        return (
            "Refused: this action requires your confirmation and this MCP client cannot "
            "ask for it. Do it in the Paperclip UI."
        )
    result = await ctx.elicit(f"{summary}\n\nConfirm?", Confirmation)
    print(f"[paperclip-mcp] elicitation {result.action}: {summary!r}", flush=True)
    if result.action == "accept" and result.data and result.data.confirm:
        return None
    return "Cancelled: action not confirmed."


async def companies() -> str:
    """Lists Paperclip companies: name, issue prefix, id."""
    rows = _items(await _call("GET", "/companies"))
    return "\n".join(f"{c['name']} [{c.get('issuePrefix', '')}] {c['id']}" for c in rows)


async def attention(company: str = "") -> str:
    """What awaits a human: approvals, agent questions, reviews, failures.
    All companies if `company` is empty."""
    out = []
    for cid, name in await _company_ids(company):
        data = await _call("GET", f"/companies/{cid}/attention")
        items = data.get("items", [])
        if not items:
            continue
        out.append(f"## {name} ({len(items)})")
        for i in items:
            # A review or a blocker carries its issue as the subject, with no related issue.
            issue, subject = i.get("relatedIssue") or {}, i.get("subject") or {}
            ref = issue.get("identifier") or subject.get("identifier") or ""
            title = issue.get("title") or subject.get("title") or ""
            out.append(f"- [{i.get('sourceKind')}] {ref} {title} — {i.get('whyNow', '')}")
    return "\n".join(out) or "Nothing pending."


async def issues(company: str, status: str = "") -> str:
    """Issues of a company. `status`: comma-separated list
    (backlog, todo, in_progress, in_review, blocked, done)."""
    cid, _ = (await _company_ids(company))[0]
    params = {"status": status} if status else {}
    rows = _items(await _call("GET", f"/companies/{cid}/issues", params=params))
    return "\n".join(
        f"{r.get('identifier')} [{r.get('status')}] {r.get('title')}" for r in rows
    ) or "No issues."


async def issue(issue: str) -> str:
    """Issue detail (e.g. ABC-1): description, comments, documents and cards
    awaiting an answer."""
    t = await _call("GET", f"/issues/{issue}")
    out = [f"{t.get('identifier')} [{t.get('status')}] {t.get('title')}"]
    if t.get("description"):
        out.append(f"\n{t['description']}")
    docs = _items(await _call("GET", f"/issues/{issue}/documents"))
    if docs:
        out.append("\nDocuments: " + ", ".join(d.get("key", "") for d in docs))
    comments = sorted(
        _items(await _call("GET", f"/issues/{issue}/comments")), key=lambda c: c.get("createdAt", "")
    )
    for c in comments[-10:]:
        who = c.get("authorType") or "?"
        out.append(f"\n--- {who} {c.get('createdAt', '')[:16]}\n{c.get('body', '')}")
    for i in _items(await _call("GET", f"/issues/{issue}/interactions")):
        if i.get("status") == "pending":
            payload = json.dumps(i.get("payload", {}), ensure_ascii=False)
            out.append(f"\n[pending card {i['id']}] {i.get('kind')} — {i.get('title')}\n{payload}")
    return "\n".join(out)


async def document(issue: str, key: str) -> str:
    """Content of an issue document (e.g. key `plan`)."""
    d = await _call("GET", f"/issues/{issue}/documents/{key}")
    return d.get("body") or d.get("content") or json.dumps(d, ensure_ascii=False)


async def agents(company: str) -> str:
    """Agents of a company: name, title, status, model."""
    cid, _ = (await _company_ids(company))[0]
    rows = _items(await _call("GET", f"/companies/{cid}/agents"))
    return "\n".join(
        f"{a['name']} — {a.get('title') or a.get('role')} [{a.get('status')}] "
        f"{(a.get('adapterConfig') or {}).get('model') or 'default model'}"
        for a in rows
    )


async def approvals(company: str = "") -> str:
    """Pending approvals (hires in particular)."""
    out = []
    for cid, name in await _company_ids(company):
        for a in _items(await _call("GET", f"/companies/{cid}/approvals")):
            if a.get("status") != "pending":
                continue
            p = a.get("payload") or {}
            what = f"{p.get('name', '')} — {p.get('title', '')}" if a.get("type") == "hire_agent" else ""
            out.append(f"{name}: {a['id']} [{a.get('type')}] {what}")
    return "\n".join(out) or "No pending approvals."


async def action_requests(company: str = "") -> str:
    """Suspended tool actions awaiting authorization (outgoing emails...)."""
    out = []
    for cid, name in await _company_ids(company):
        for r in _items(await _call("GET", f"/companies/{cid}/tools/action-requests")):
            if r.get("status") not in (None, "pending"):
                continue
            out.append(f"{name}: {r['id']} {r.get('toolName', '')} {json.dumps(r.get('arguments') or {}, ensure_ascii=False)[:300]}")
    return "\n".join(out) or "No pending actions."


async def issue_comment(issue: str, text: str) -> str:
    """Posts a comment on an issue. The assigned agent reads it when it wakes up."""
    await _call("POST", f"/issues/{issue}/comments", json={"body": text})
    return f"Comment added to {issue}."


UPDATE_STATUSES = ("backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled")
BOARD_USER = {"me", "moi", "board"}


async def _assignee(cid: str, assignee: str) -> tuple[dict[str, Any], str]:
    if assignee.strip().lower() in BOARD_USER:
        me = await _call("GET", "/cli-auth/me")
        return {"assigneeAgentId": None, "assigneeUserId": me["userId"]}, "you"
    key = assignee.strip().lower()
    rows = _items(await _call("GET", f"/companies/{cid}/agents"))
    match = [a for a in rows if key in (a["id"].lower(), a["name"].lower())]
    if len(match) != 1:
        raise ToolError(f"agent {assignee!r}: {len(match)} match(es). Agents: " + ", ".join(a["name"] for a in rows))
    return {"assigneeAgentId": match[0]["id"], "assigneeUserId": None}, match[0]["name"]


async def issue_update(issue: str, ctx: Context, assignee: str = "", status: str = "", comment: str = "") -> str:
    """Assigns an issue and/or changes its status, with an optional comment.
    `issue`: identifier (e.g. ABC-12) or UUID. `assignee`: name or id of an agent
    in the issue's company, or `me` (or `moi`) for the board user who owns the token.
    `status`: backlog, todo, in_progress, in_review, blocked, done, cancelled;
    `blocked` requires a comment saying what is awaited.
    Assignment and the statuses listed in policy.yaml run directly; any other
    change asks for confirmation.
    A comment only wakes the assigned agent; an issue assigned to a user wakes
    nobody. To make an agent act: assign it, status `todo`, and a comment saying
    what to do."""
    if not (assignee or status):
        return "Nothing to change: give assignee and/or status."
    if status and status not in UPDATE_STATUSES:
        return "status must be one of " + ", ".join(UPDATE_STATUSES) + "."
    if status == "blocked" and not comment:
        return "Status blocked requires a comment saying what is awaited."
    t = await _call("GET", f"/issues/{issue}")
    body: dict[str, Any] = {}
    changes = []
    if assignee:
        try:
            fields, who = await _assignee(t["companyId"], assignee)
        except ToolError as exc:
            return f"{t.get('identifier')}: {exc}"
        body.update(fields)
        changes.append(f"assign to {who}")
    if status:
        body["status"] = status
        changes.append(f"status {t.get('status')} -> {status}")
        if status == "blocked":
            body["unblockDescriptor"] = {"owner": "board", "action": comment}
    if comment:
        body["comment"] = comment
        changes.append(f"comment:\n{comment}")
    safe = (not assignee or SAFE_UPDATE["assign"]) and (not status or status in SAFE_UPDATE["statuses"])
    if not safe:
        refused = await _confirm(ctx, f"{t.get('identifier')} {t.get('title')}: " + ", ".join(changes))
        if refused:
            return refused
    u = await _call("PATCH", f"/issues/{t['id']}", json=body)
    return f"{u.get('identifier', t.get('identifier'))} [{u.get('status')}] updated: " + ", ".join(changes)


async def _project_id(cid: str, project: str) -> str:
    rows = _items(await _call("GET", f"/companies/{cid}/projects"))
    match = [p for p in rows if project in (p["id"], p.get("name"))]
    if len(match) != 1:
        raise ToolError(f"project {project!r}: {len(match)} match(es). Projects: " + ", ".join(p.get("name", "") for p in rows))
    return match[0]["id"]


async def issue_create(company: str, title: str, description: str = "", project: str = "") -> str:
    """Creates an unassigned issue: it only starts once a human assigns it to an
    agent. `project`: name or id of a project in the company."""
    cid, _ = (await _company_ids(company))[0]
    body = {"title": title, "description": description}
    if project:
        body["projectId"] = await _project_id(cid, project)
    t = await _call("POST", f"/companies/{cid}/issues", json=body)
    return f"Issue created, unassigned: {t.get('identifier')} — {t.get('title')}"


async def projects(company: str) -> str:
    """Projects of a company: name, status, id."""
    cid, _ = (await _company_ids(company))[0]
    rows = _items(await _call("GET", f"/companies/{cid}/projects"))
    return "\n".join(f"{p.get('name')} [{p.get('status')}] {p['id']}" for p in rows) or "No projects."


async def project_create(company: str, name: str, description: str = "", repository_url: str = "") -> str:
    """Creates a project in a company, with no lead and no issues.
    `repository_url`: GitHub repository to attach (optional)."""
    cid, _ = (await _company_ids(company))[0]
    body: dict[str, Any] = {"name": name, "description": description}
    if repository_url:
        body["repositoryUrls"] = [repository_url]
    p = await _call("POST", f"/companies/{cid}/projects", json=body)
    return f"Project created: {p.get('name')} [{p.get('status')}] {p.get('id')}"


async def _agent(company: str, agent: str) -> dict[str, Any]:
    cid, _ = (await _company_ids(company))[0]
    key = agent.strip().lower()
    rows = _items(await _call("GET", f"/companies/{cid}/agents"))
    match = [a for a in rows if key in (a["id"].lower(), a["name"].lower())]
    if len(match) != 1:
        raise ToolError(f"agent {agent!r}: {len(match)} match(es). Agents: " + ", ".join(a["name"] for a in rows))
    return {**match[0], "companyId": cid}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _assistant_texts(log: str, keep: int = 3) -> list[str]:
    texts = []
    for line in log.splitlines():
        try:
            event = json.loads(json.loads(line).get("chunk") or "{}")
        except (ValueError, AttributeError):
            continue
        if event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                texts.append(block["text"].strip())
    return texts[-keep:]


async def runs(company: str = "", agent: str = "") -> str:
    """Agent runs. Without `agent`: runs in progress, all companies if `company` is
    empty. With `company` and `agent` (name or id): that agent's last 10 runs."""
    if agent:
        a = await _agent(company, agent)
        rows = _items(await _call("GET", f"/companies/{a['companyId']}/heartbeat-runs", params={"agentId": a["id"], "limit": 10}))
        return "\n".join(
            f"{r['id']} [{r.get('status')}] {str(r.get('startedAt') or '')[:16]} {r.get('invocationSource') or ''}"
            + (f" error: {r['error']}" if r.get("error") else "")
            for r in rows
        ) or f"No runs for {a['name']}."
    out = []
    for cid, name in await _company_ids(company):
        for r in _items(await _call("GET", f"/companies/{cid}/live-runs")):
            ref = ""
            if r.get("issueId"):
                ref = (await _call("GET", f"/issues/{r['issueId']}")).get("identifier", "")
            out.append(
                f"{name}: {r.get('agentName')} [{r.get('status')}] {ref} since {str(r.get('startedAt') or '')[11:16]} UTC"
                f" · {r.get('currentStatusMessage') or ''} · run {r['id']}"
            )
    return "\n".join(out) or "No run in progress."


async def run(run_id: str) -> str:
    """Detail of a run: status, times, error and the agent's last messages."""
    r = await _call("GET", f"/heartbeat-runs/{run_id}")
    out = [
        f"run {r['id']} [{r.get('status')}] {r.get('invocationSource') or ''}",
        f"started {r.get('startedAt')} · finished {r.get('finishedAt') or '-'}",
    ]
    if r.get("error"):
        out.append(f"error: {r['error']}")
    log = (await _call("GET", f"/heartbeat-runs/{run_id}/log")).get("content") or ""
    for text in _assistant_texts(log):
        out.append(f"\n--- agent\n{text[:800]}")
    return "\n".join(out)


async def issue_history(issue: str) -> str:
    """What happened on an issue: status changes, assignments, comments, runs."""
    rows = sorted(
        (a for a in _items(await _call("GET", f"/issues/{issue}/activity")) if not str(a.get("action", "")).startswith("tool_gateway.")),
        key=lambda a: a.get("createdAt", ""),
    )
    return "\n".join(
        f"{str(a.get('createdAt', ''))[:16]} {a.get('actorType')} {a.get('action')}"
        + (f" {json.dumps(a['details'], ensure_ascii=False)[:160]}" if a.get("details") else "")
        for a in rows[-25:]
    ) or "No activity."


async def spend(company: str = "", days: int = 7) -> str:
    """Token use per agent over the last `days` days, all companies if `company`
    is empty. Subscription runs cost 0 cents, so tokens and run counts are shown."""
    out = []
    for cid, name in await _company_ids(company):
        rows = _items(await _call("GET", f"/companies/{cid}/costs/by-agent", params={"from": _since(days)}))
        rows = [r for r in rows if r.get("inputTokens") or r.get("outputTokens")]
        if not rows:
            continue
        out.append(f"## {name}")
        for r in sorted(rows, key=lambda r: -(r.get("outputTokens") or 0)):
            out.append(
                f"- {r.get('agentName')}: {(r.get('subscriptionRunCount') or 0) + (r.get('apiRunCount') or 0)} runs, "
                f"{r.get('inputTokens', 0):,} in, {r.get('cachedInputTokens', 0):,} cached, {r.get('outputTokens', 0):,} out"
                + (f", {r['costCents'] / 100:.2f} $" if r.get("costCents") else "")
            )
    return "\n".join(out) or f"No usage over the last {days} days."


async def activity(company: str, limit: int = 20) -> str:
    """Latest events of a company: issues, comments, runs, agents."""
    cid, _ = (await _company_ids(company))[0]
    rows = _items(await _call("GET", f"/companies/{cid}/activity", params={"limit": limit}))
    return "\n".join(
        f"{str(a.get('createdAt', ''))[:16]} {a.get('actorType')} {a.get('action')} {a.get('entityType')}"
        for a in rows
    ) or "No activity."


async def routines(company: str) -> str:
    """Routines of a company: recurring tasks run by its agents."""
    cid, _ = (await _company_ids(company))[0]
    rows = _items(await _call("GET", f"/companies/{cid}/routines"))
    return "\n".join(f"{r.get('title')} [{r.get('status', '')}] {r['id']}" for r in rows) or "No routines."


async def agent_pause(company: str, agent: str) -> str:
    """Pauses an agent. Its run in progress, if any, is cancelled."""
    a = await _agent(company, agent)
    await _call("POST", f"/agents/{a['id']}/pause")
    return f"{a['name']} paused."


async def agent_resume(company: str, agent: str, ctx: Context) -> str:
    """Resumes a paused agent. The quota guard may pause it again while a quota
    limit is active."""
    a = await _agent(company, agent)
    refused = await _confirm(ctx, f"Resume {a['name']} (status {a.get('status')})", "agent_resume")
    if refused:
        return refused
    await _call("POST", f"/agents/{a['id']}/resume")
    return f"{a['name']} resumed."


async def agent_wake(company: str, agent: str, reason: str = "") -> str:
    """Wakes an agent now, without posting a comment. `reason` is shown in its run."""
    a = await _agent(company, agent)
    body: dict[str, Any] = {"source": "on_demand", "triggerDetail": "manual"}
    if reason:
        body["reason"] = reason
    await _call("POST", f"/agents/{a['id']}/wakeup", json=body)
    return f"{a['name']} woken."


async def agent_unblock(company: str, agent: str) -> str:
    """Clears an agent's error state so it can run again."""
    a = await _agent(company, agent)
    await _call("POST", f"/agents/{a['id']}/clear-error")
    return f"{a['name']}: error cleared."


async def run_stop(run_id: str) -> str:
    """Stops a run in progress. Paperclip does not wake the agent again for it."""
    await _call("POST", f"/heartbeat-runs/{run_id}/cancel")
    return f"Run {run_id} stopped."


async def routine_run(routine_id: str, ctx: Context) -> str:
    """Runs a routine now, outside its schedule. See the routines tool for ids."""
    r = await _call("GET", f"/routines/{routine_id}")
    refused = await _confirm(ctx, f"Run the routine now: {r.get('title')}")
    if refused:
        return refused
    await _call("POST", f"/routines/{routine_id}/run", json={"source": "manual"})
    return f"Routine started: {r.get('title')}."


async def approval_comment(approval_id: str, text: str) -> str:
    """Comments on an approval without deciding it."""
    await _call("POST", f"/approvals/{approval_id}/comments", json={"body": text})
    return f"Comment added to approval {approval_id}."


async def approval_decision(approval_id: str, decision: str, note: str, ctx: Context) -> str:
    """Approves or rejects an approval. `decision`: approve or reject."""
    if decision not in ("approve", "reject"):
        return "decision must be approve or reject."
    a = await _call("GET", f"/approvals/{approval_id}")
    p = a.get("payload") or {}
    label = f"{a.get('type')} {p.get('name', '')} {p.get('title', '')}".strip()
    refused = await _confirm(ctx, f"{'Approve' if decision == 'approve' else 'Reject'}: {label}")
    if refused:
        return refused
    await _call("POST", f"/approvals/{approval_id}/{decision}", json={"decisionNote": note})
    return f"Approval {approval_id}: {decision}."


async def interaction_respond(issue: str, interaction_id: str, answers_json: str, ctx: Context) -> str:
    """Answers an agent's question card. `answers_json`: list
    [{"questionId", "optionIds": [...], "otherText"}], see the issue tool."""
    answers = json.loads(answers_json)
    refused = await _confirm(
        ctx, f"Answer card {interaction_id} on {issue}:\n{json.dumps(answers, ensure_ascii=False, indent=1)}",
        "interaction_respond",
    )
    if refused:
        return refused
    await _call("POST", f"/issues/{issue}/interactions/{interaction_id}/respond", json={"answers": answers})
    return f"Answer sent on {issue}."


async def plan_confirm(issue: str, interaction_id: str, accept: bool, reason: str, ctx: Context) -> str:
    """Accepts a plan or sends it back with requested changes (`reason`)."""
    refused = await _confirm(ctx, f"{'Accept' if accept else 'Reject'} the plan of {issue}. {reason}")
    if refused:
        return refused
    action = "accept" if accept else "reject"
    await _call("POST", f"/issues/{issue}/interactions/{interaction_id}/{action}", json={"reason": reason})
    return f"Plan of {issue}: {action}."


async def action_request_decision(request_id: str, approve: bool, ctx: Context) -> str:
    """Authorizes or declines a suspended tool action (outgoing email...)."""
    refused = await _confirm(ctx, f"{'Authorize' if approve else 'Decline'} action {request_id}")
    if refused:
        return refused
    await _call("POST", f"/tool-gateway/action-requests/{request_id}/{'approve' if approve else 'decline'}")
    return f"Action {request_id}: {'authorized' if approve else 'declined'}."


# Claude Code sessions outside Paperclip, fed by a hook on each machine (hooks/qg-ping.py).
# In memory only: lost on restart, rebuilt by the next hook events.
SESSIONS: dict[str, dict[str, Any]] = {}
ACTIVE_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"}
SESSIONS_MAX = 200


def _clip(value: Any, limit: int = 80) -> str:
    return str(value or "").replace("\n", " ").strip()[:limit]


def _prune_sessions(now: float) -> None:
    for sid, s in list(SESSIONS.items()):
        if now - s["seen"] > SESSIONS_TTL or (s["status"] == "ended" and now - s["seen"] > 120):
            del SESSIONS[sid]


@mcp.custom_route("/sessions/event", methods=["POST"])
async def session_event(request: Any) -> Any:
    """One Claude Code hook event. Keeps only the session id, project folder name,
    event, tool name, host and entry point: never the prompt or the tool input."""
    if SESSIONS_TOKEN and not hmac.compare_digest(request.headers.get("x-qg-token", ""), SESSIONS_TOKEN):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    sid = _clip(body.get("session_id"), 100) if isinstance(body, dict) else ""
    if not sid:
        return JSONResponse({"error": "session_id missing"}, status_code=400)
    now = time.time()
    _prune_sessions(now)
    if sid not in SESSIONS and len(SESSIONS) >= SESSIONS_MAX:
        return JSONResponse({"error": "too many sessions"}, status_code=429)
    event = _clip(body.get("hook_event_name"), 40)
    cwd = _clip(body.get("cwd"), 300).rstrip("/")
    s = SESSIONS.setdefault(sid, {"started": now, "tool": ""})
    s.update(
        seen=now,
        project=cwd.rsplit("/", 1)[-1] or s.get("project") or "?",
        host=_clip(body.get("host"), 40) or s.get("host") or "?",
        via=_clip(body.get("entrypoint"), 30) or s.get("via") or "?",
        status="ended" if event == "SessionEnd" else "active" if event in ACTIVE_EVENTS else "idle",
    )
    if event == "PreToolUse":
        s["tool"] = _clip(body.get("tool_name"), 60)
    elif event in ("Stop", "SessionStart", "SessionEnd"):
        s["tool"] = ""
    return JSONResponse({"ok": True})


async def sessions() -> str:
    """Claude Code sessions outside Paperclip (CLI, desktop...), seen in the last
    minutes: id, status (active, idle, ended), project, current tool, host, entry point."""
    now = time.time()
    _prune_sessions(now)
    rows = sorted(SESSIONS.items(), key=lambda kv: -kv[1]["seen"])
    if not rows:
        return "No session."
    return "\n".join(
        f"{sid[:8]} [{s['status']}] {s['project']} · tool={s['tool'] or '-'} · host={s['host']} "
        f"· via={s['via']} · seen={int(now - s['seen'])}s · up={int(now - s['started'])}s"
        for sid, s in rows
    )


# State of the machine that hosts Paperclip, for a dashboard. Read-only and without a
# Paperclip call: the host's /proc and /sys as a container sees them, statvfs on the
# given paths, and one GET on Docker for the container list.
CPU_SENSORS = {"coretemp", "k10temp", "zenpower", "cpu_thermal"}
CPU_ZONES = {"x86_pkg_temp", "cpu-thermal", "cpu_thermal"}
SIZE_UNITS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _ls(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _size(n: float) -> str:
    unit = "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            break
        n /= 1024
    return f"{n:.0f}{unit}" if unit in ("B", "KiB", "MiB") else f"{n:.1f}{unit}"


def _parse_size(raw: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTP]?)i?B?", raw.strip(), re.IGNORECASE)
    return float(m.group(1)) * 1024 ** SIZE_UNITS[m.group(2).upper()] if m else 0.0


def _cpu_times() -> tuple[int, int]:
    """(total, idle) jiffies of the host since boot."""
    fields = _read(f"{NAS_PROC}/stat").split("\n", 1)[0].split()
    if len(fields) < 6 or fields[0] != "cpu":
        return 0, 0
    vals = [int(v) for v in fields[1:9]]
    return sum(vals), vals[3] + vals[4]


def _cpu_temp() -> float | None:
    temps = []
    hwmon = f"{NAS_SYS}/class/hwmon"
    for hw in _ls(hwmon):
        if _read(f"{hwmon}/{hw}/name").strip() in CPU_SENSORS:
            temps += [_read(f"{hwmon}/{hw}/{f}") for f in _ls(f"{hwmon}/{hw}") if re.fullmatch(r"temp\d+_input", f)]
    if not temps:
        thermal = f"{NAS_SYS}/class/thermal"
        temps = [_read(f"{thermal}/{z}/temp") for z in _ls(thermal) if _read(f"{thermal}/{z}/type").strip() in CPU_ZONES]
    values = [int(t) / 1000 for t in (t.strip() for t in temps) if t.isdigit()]
    return max(values) if values else None


async def _host_line() -> str:
    total0, idle0 = _cpu_times()
    await asyncio.sleep(0.25)
    total1, idle1 = _cpu_times()
    out = [f"host name={NAS_NAME}"]
    if total1 > total0:
        out.append(f"cpu={100 * (1 - (idle1 - idle0) / (total1 - total0)):.0f}%")
    cores = len(re.findall(r"^cpu\d+ ", _read(f"{NAS_PROC}/stat"), re.MULTILINE))
    if cores:
        out.append(f"cores={cores}")
    load = _read(f"{NAS_PROC}/loadavg").split()[:3]
    if len(load) == 3:
        out.append("load=" + ",".join(load))
    mem = {}
    for line in _read(f"{NAS_PROC}/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if rest.split()[:1] and rest.split()[0].isdigit():
            mem[key] = int(rest.split()[0]) * 1024
    if mem.get("MemTotal"):
        total = mem["MemTotal"]
        # The ZFS cache (ARC) gives its memory back on demand but Linux counts it as used.
        arc = next(
            (int(p[2]) for p in (ln.split() for ln in _read(f"{NAS_PROC}/spl/kstat/zfs/arcstats").splitlines())
             if len(p) == 3 and p[0] == "size" and p[2].isdigit()),
            0,
        )
        used = max(total - mem.get("MemAvailable", mem.get("MemFree", 0)) - arc, 0)
        out.append(f"mem={used / 2**30:.1f}/{total / 2**30:.1f}GiB mem_pct={100 * used / total:.0f}%")
        if arc:
            out.append(f"arc={arc / 2**30:.1f}GiB")
        if mem.get("SwapTotal"):
            out.append(f"swap={(mem['SwapTotal'] - mem.get('SwapFree', 0)) / 2**30:.1f}/{mem['SwapTotal'] / 2**30:.1f}GiB")
    temp = _cpu_temp()
    if temp is not None:
        out.append(f"temp={temp:.0f}C")
    up = _read(f"{NAS_PROC}/uptime").split()[:1]
    if up:
        s = int(float(up[0]))
        out.append(f"uptime={s // 86400}d{s % 86400 // 3600}h" if s >= 86400 else f"uptime={s // 3600}h{s % 3600 // 60}m")
    return " ".join(out)


def _pool_lines() -> list[str]:
    base = f"{NAS_PROC}/spl/kstat/zfs"
    states = ((name, _read(f"{base}/{name}/state").strip()) for name in _ls(base))
    return [f"pool {name} [{state}]" for name, state in states if state]


def _disk_lines() -> list[str]:
    out = []
    for raw in _split(NAS_DISK_PATHS):
        label, _, rest = raw.rpartition("=")
        path, _, size = rest.partition("@")
        label = re.sub(r"\s+", "_", label.strip() or path.rstrip("/").rsplit("/", 1)[-1] or "root")
        try:
            st = os.statvfs(path)
        except OSError as exc:
            out.append(f"disk {label} error={exc.strerror or 'unreadable'}")
            continue
        free = st.f_bavail * st.f_frsize
        # With "@size" the path only gives the free space: on ZFS, statvfs counts what
        # one dataset uses, not the pool.
        used = max(_parse_size(size) - free, 0) if _parse_size(size) else (st.f_blocks - st.f_bfree) * st.f_frsize
        pct = 100 * used / (used + free) if used + free else 0
        out.append(f"disk {label} used={_size(used)} free={_size(free)} total={_size(used + free)} pct={pct:.0f}%")
    return out


async def _containers() -> tuple[list[dict[str, Any]], str]:
    """Containers as Docker lists them and where the list comes from: one GET per
    Docker host, never a write."""
    path, params = "/containers/json", {"all": "1"}
    if NAS_DOCKER_URL or NAS_DOCKER_SOCKET:
        transport = None if NAS_DOCKER_URL else httpx.AsyncHTTPTransport(uds=NAS_DOCKER_SOCKET)
        async with httpx.AsyncClient(base_url=NAS_DOCKER_URL or "http://docker", transport=transport, timeout=10) as c:
            r = await c.get(path, params=params)
            r.raise_for_status()
            return r.json(), "docker"
    if PORTAINER_URL:
        async with httpx.AsyncClient(
            base_url=f"{PORTAINER_URL}/api", headers={"X-API-Key": PORTAINER_KEY}, verify=not PORTAINER_INSECURE, timeout=10
        ) as c:
            ids = _split(PORTAINER_ENDPOINT)
            if not ids:
                r = await c.get("/endpoints")
                r.raise_for_status()
                ids = [str(e["Id"]) for e in r.json() if e.get("Type") in (1, 2, 4)]
            rows: list[dict[str, Any]] = []
            for eid in ids:
                r = await c.get(f"/endpoints/{eid}/docker{path}", params=params)
                r.raise_for_status()
                rows += r.json()
            return rows, "portainer"
    return [], ""


async def _container_lines() -> list[str]:
    try:
        rows, source = await _containers()
    except Exception as exc:  # the host lines must come out even when Docker does not answer
        return [f"containers error={_clip((str(exc) or type(exc).__name__).splitlines()[0], 160)}"]
    if not source:
        return ["containers source=none (set NAS_DOCKER_URL, NAS_DOCKER_SOCKET or PORTAINER_URL)"]
    items = []
    for r in rows:
        status = _clip(r.get("Status"), 60)
        health = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
        items.append((
            _clip((r.get("Labels") or {}).get("com.docker.compose.project"), 40).replace(" ", "_") or "-",
            _clip((r.get("Names") or [str(r.get("Id", "?"))[:12]])[0].lstrip("/"), 60).replace(" ", "_"),
            _clip(r.get("State"), 20) or "unknown",
            health.group(1).replace("health: ", "") if health else "-",
            status.replace(health.group(0), "").strip() if health else status,
            _clip(r.get("Image"), 80),
        ))
    items.sort()
    running = sum(1 for i in items if i[2] == "running")
    unhealthy = sum(1 for i in items if i[3] == "unhealthy")
    return [f"containers running={running} total={len(items)} unhealthy={unhealthy} source={source}"] + [
        f"container {name} [{state}] {health} · {status} · stack={stack} · image={image}"
        for stack, name, state, health, status, image in items
    ]


async def nas() -> str:
    """State of the machine that hosts Paperclip, read-only: CPU, load, memory,
    ZFS pools, disk space and the Docker containers with their state."""
    return "\n".join([await _host_line(), *_pool_lines(), *_disk_lines(), *(await _container_lines())])


# What waits on what, for a dashboard: read-only, one issue list per company.
OPEN_STATUSES = "todo,in_progress,in_review,blocked"


def _holder(agent_id: Any, user_id: Any, names: dict[str, str]) -> str:
    if agent_id:
        return (names.get(agent_id) or "agent").replace(" ", "_")
    return "user" if user_id else "-"


async def dependencies(company: str = "") -> str:
    """What waits on what, all companies if `company` is empty: the open issues that
    are blocked, wait on other issues or are assigned to a user, and the issues they
    wait on. One line per issue: `by` who holds it, `after` the issues it waits on,
    `waits` who must unblock it, `since` the day it got blocked or last changed,
    `project` the issue's project name (spaces as `_`, `-` when it has none)."""
    out = []
    for cid, name in await _company_ids(company):
        names = {a["id"]: a.get("name") or "?" for a in _items(await _call("GET", f"/companies/{cid}/agents")) if a.get("id")}
        rows = [
            r for r in _items(await _call("GET", f"/companies/{cid}/issues", params={"status": OPEN_STATUSES, "includeBlockedBy": "true"}))
            if r.get("id")
        ]
        # A Paperclip that does not know includeBlockedBy leaves the field out of the
        # list: the blockers are then read on each blocked issue.
        if rows and not any("blockedBy" in r for r in rows):
            for r in [r for r in rows if r.get("status") == "blocked"][:40]:
                r["blockedBy"] = (await _call("GET", f"/issues/{r['id']}")).get("blockedBy") or []
        by_id = {r["id"]: r for r in rows}
        keep: dict[str, dict[str, Any]] = {}
        for r in rows:
            blockers = [b for b in r.get("blockedBy") or [] if b.get("id")]
            if blockers or r.get("status") == "blocked" or r.get("assigneeUserId"):
                keep[r["id"]] = r
            for b in blockers:
                # A blocker outside the open statuses (done, cancelled, backlog) only exists as this summary.
                keep.setdefault(b["id"], by_id.get(b["id"], b))
        if not keep:
            continue
        projects = {p["id"]: p.get("name") or "?" for p in _items(await _call("GET", f"/companies/{cid}/projects")) if p.get("id")}
        # A blocker summary carries no project: the closed blockers are read one by one.
        for i in [i for i in keep.values() if "projectId" not in i][:40]:
            try:
                i["projectId"] = (await _call("GET", f"/issues/{i['id']}")).get("projectId")
            except ToolError:
                i["projectId"] = None

        def ref(i: dict[str, Any]) -> str:
            return i.get("identifier") or str(i["id"])[:8]

        out.append(f"## {name} ({len(keep)})")
        for i in keep.values():
            unblock = i.get("unblockDescriptor") or {}
            owner = unblock.get("owner")
            waits = owner if owner == "board" else _holder(owner.get("agentId"), owner.get("userId"), names) if isinstance(owner, dict) else "-"
            after = ",".join(ref(b) for b in i.get("blockedBy") or [] if b.get("id")) or "-"
            since = str(i.get("blockedTransitionAt") or i.get("updatedAt") or "")[:10] or "-"
            project = "_".join(_clip(projects.get(i.get("projectId") or "", "-"), 60).split()) or "-"
            title = _clip(i.get("title"), 200) or ("not readable" if i.get("locked") else "")
            out.append(
                f"{ref(i)} [{i.get('status') or '?'}] by={_holder(i.get('assigneeAgentId'), i.get('assigneeUserId'), names)}"
                f" after={after} waits={waits} since={since} project={project} · {title}"
            )
            if _clip(unblock.get("action")):
                out.append(f"  awaited: {_clip(unblock.get('action'), 240)}")
    return "\n".join(out) or "Nothing is blocked or waiting."


TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        companies, attention, issues, issue, document, agents, approvals, action_requests,
        projects, issue_comment, issue_create, issue_update, project_create,
        runs, run, issue_history, spend, activity, routines,
        agent_pause, agent_resume, agent_wake, agent_unblock, run_stop, routine_run, approval_comment,
        approval_decision, interaction_respond, plan_confirm, action_request_decision,
        sessions, nas, dependencies,
    )
}
CONFIRMING = {
    "issue_update", "approval_decision", "interaction_respond", "plan_confirm", "action_request_decision",
    "agent_resume", "routine_run",
}
# Confirming tools the policy may also set to allow: the client claude.ai cannot
# ask (no elicitation), and these two are refused there otherwise (DMC-722).
ALLOWABLE = {"agent_resume", "interaction_respond"}
ACTIVE: dict[str, str] = {}


def register(policy: dict[str, str]) -> list[str]:
    exposed = []
    for name, fn in TOOLS.items():
        mode = policy.get(name, "deny")
        if mode == "deny":
            continue
        if name in CONFIRMING and mode == "allow" and name not in ALLOWABLE:
            raise SystemExit(f"policy.yaml: {name} can only be ask or deny")
        if mode == "ask" and name not in CONFIRMING:
            raise SystemExit(f"policy.yaml: {name} has no confirmation, use allow or deny")
        mcp.tool()(fn)
        ACTIVE[name] = mode
        exposed.append(f"{name}={mode}")
    return exposed


EXEMPT_PATHS = {"/healthz"}


class AccessJWT(BaseHTTPMiddleware):
    """Rejects any request without a valid Cloudflare Access JWT: a shared Docker
    network must not be enough to drive Paperclip, and Managed OAuth requires it."""

    def __init__(self, app: Any, issuer: str, audience: str) -> None:
        super().__init__(app)
        self.issuer = issuer
        self.audience = audience
        self.jwks = jwt.PyJWKClient(f"{issuer}/cdn-cgi/access/certs")

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        token = request.headers.get("cf-access-jwt-assertion")
        if not token:
            return JSONResponse({"error": "unauthenticated", "detail": "Cf-Access-Jwt-Assertion missing"}, status_code=401)
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            jwt.decode(
                token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception as exc:
            return JSONResponse({"error": "unauthenticated", "detail": f"invalid Access JWT: {exc}"}, status_code=401)
        return await call_next(request)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Any) -> Any:
    return JSONResponse({"status": "ok"})


def main() -> None:
    import uvicorn

    if not TOKEN:
        raise SystemExit("PAPERCLIP_API_KEY missing")
    if not (CF_TEAM and CF_AUD) and os.environ.get("MCP_INSECURE_NO_AUTH") != "1":
        raise SystemExit(
            "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD missing: without them, any container "
            "on the network drives Paperclip. MCP_INSECURE_NO_AUTH=1 for a local test."
        )
    print("[paperclip-mcp] tools:", ", ".join(register(load_policy(POLICY_FILE))), flush=True)

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=LOOPBACK_HOSTS + _hosts(ALLOWED_HOSTS),
        allowed_origins=LOOPBACK_ORIGINS + _split(ALLOWED_ORIGINS),
    )
    app = mcp.streamable_http_app(transport_security=security)
    if CF_TEAM and CF_AUD:
        issuer = CF_TEAM if CF_TEAM.startswith("https://") else f"https://{CF_TEAM}"
        app.add_middleware(AccessJWT, issuer=issuer, audience=CF_AUD)
        print(f"[paperclip-mcp] Cloudflare Access JWT required (iss={issuer})", flush=True)
    else:
        print("[paperclip-mcp] WARNING: authentication disabled (local test)", flush=True)

    uvicorn.run(app, host=os.environ.get("MCP_HOST", "0.0.0.0"), port=int(os.environ.get("MCP_PORT", "9200")), log_level="info")


if __name__ == "__main__":
    main()
