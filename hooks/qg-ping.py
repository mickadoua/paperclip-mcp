#!/usr/bin/env python3
"""Claude Code hook: tells the Paperclip bridge that a session is alive.

Sends only the session id, the working folder, the event, the tool name, the host
name and the Claude Code entry point. Never the prompt, the tool input or a file.
Returns immediately: the request leaves from a detached child process, so a slow
or unreachable bridge never slows Claude Code down.

Configuration, in ~/.config/qg-paperclip.env (one KEY=value per line):
  QG_URL=https://<bridge hostname>/sessions/event
  CF_ACCESS_CLIENT_ID=...       Cloudflare Access service token
  CF_ACCESS_CLIENT_SECRET=...
  QG_TOKEN=...                  optional, same value as SESSIONS_INGEST_TOKEN
"""
import json
import os
import socket
import sys
import urllib.request

KEEP = ("session_id", "hook_event_name", "cwd", "tool_name")


def config() -> dict:
    conf = {}
    try:
        with open(os.path.expanduser("~/.config/qg-paperclip.env")) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    conf[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return conf


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    conf = config()
    if not conf.get("QG_URL") or not isinstance(event, dict):
        return
    body = {k: event.get(k) for k in KEEP if event.get(k)}
    body["host"] = socket.gethostname().split(".")[0]
    body["entrypoint"] = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if hasattr(os, "fork") and os.fork():
        return  # parent: hand back to Claude Code at once
    headers = {"Content-Type": "application/json", "User-Agent": "qg-ping/1"}
    for env, header in (("CF_ACCESS_CLIENT_ID", "CF-Access-Client-Id"),
                        ("CF_ACCESS_CLIENT_SECRET", "CF-Access-Client-Secret"),
                        ("QG_TOKEN", "X-QG-Token")):
        if conf.get(env):
            headers[header] = conf[env]
    req = urllib.request.Request(conf["QG_URL"], json.dumps(body).encode(), headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
