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

import json
import logging
import os
from typing import Any, Callable

import httpx
import jwt
import yaml
from mcp.server.mcpserver import Context, MCPServer
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
        raise RuntimeError(f"Paperclip {method} {path} -> {r.status_code} : {r.text[:300]}")
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
        raise ValueError(f"Unknown company: {company}. See the companies tool.")
    return match


class Confirmation(BaseModel):
    confirm: bool


async def _confirm(ctx: Context, summary: str) -> str | None:
    """None if the user confirmed, otherwise the reason for refusing."""
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
            issue = i.get("relatedIssue") or {}
            ref = issue.get("identifier") or ""
            title = issue.get("title") or (i.get("subject") or {}).get("title") or ""
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
        raise ValueError(f"agent {assignee!r}: {len(match)} match(es). Agents: " + ", ".join(a["name"] for a in rows))
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
        except ValueError as exc:
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
        raise ValueError(f"project {project!r}: {len(match)} match(es). Projects: " + ", ".join(p.get("name", "") for p in rows))
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
    refused = await _confirm(ctx, f"Answer card {interaction_id} on {issue}:\n{json.dumps(answers, ensure_ascii=False, indent=1)}")
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


TOOLS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        companies, attention, issues, issue, document, agents, approvals, action_requests,
        projects, issue_comment, issue_create, issue_update, project_create,
        approval_decision, interaction_respond, plan_confirm, action_request_decision,
    )
}
CONFIRMING = {"issue_update", "approval_decision", "interaction_respond", "plan_confirm", "action_request_decision"}


def register(policy: dict[str, str]) -> list[str]:
    exposed = []
    for name, fn in TOOLS.items():
        mode = policy.get(name, "deny")
        if mode == "deny":
            continue
        if name in CONFIRMING and mode == "allow":
            raise SystemExit(f"policy.yaml: {name} can only be ask or deny")
        if mode == "ask" and name not in CONFIRMING:
            raise SystemExit(f"policy.yaml: {name} has no confirmation, use allow or deny")
        mcp.tool()(fn)
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
