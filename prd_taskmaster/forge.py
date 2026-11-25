"""Forgejo intake: pull approved feature requests into Discovery.

A side channel, not a task provider. The forge holds the *requests* (issues a
maintainer has labelled ``Status/Approved``); the selected task provider still
owns the tasks. Stdlib only (urllib) so the zero-dependency CLI keeps working.

Files (per project, under ``.atlas-ai/``):
  config/forge.json    where the forge is and which label means "approved"
  forge-intake.json    which issue numbers this project has already claimed

The access token is never stored: the config holds only the NAME of the
environment variable that carries it.

Pure cores raise CommandError and return dicts; the cmd_* wrappers emit/fail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from prd_taskmaster.lib import (
    REPO_ROOT, CommandError, atomic_write, emit, fail, locked_update, now_iso,
)

CONFIG_PATH = Path(".atlas-ai") / "config" / "forge.json"
CLAIMS_PATH = Path(".atlas-ai") / "forge-intake.json"
ISSUE_TEMPLATE_SRC = REPO_ROOT / "templates" / "forgejo" / "feature_request.md"
ISSUE_TEMPLATE_DEST = Path(".forgejo") / "issue_template" / "feature_request.md"

DEFAULT_LABEL = "Status/Approved"
DEFAULT_TOKEN_ENV = "FORGEJO_TOKEN"
PAGE_LIMIT = 50
MAX_PAGES = 20
MAX_BODY_CHARS = 20000
TIMEOUT_SECONDS = 20

# Forgejo's default label set. An approved issue that ALSO carries one of these
# is reported under "skipped" instead of being offered for work.
HOLD_LABELS = (
    "Status/Blocked",
    "Status/Need More Info",
    "Status/Abandoned",
    "Reviewed/Duplicate",
    "Reviewed/Invalid",
    "Reviewed/Won't Fix",
)
PRIORITY_ORDER = ("Priority/Critical", "Priority/High", "Priority/Medium", "Priority/Low")

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SSH_REMOTE = re.compile(r"(?:ssh://)?(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?[:/](?P<path>.+)\Z")


# ─── Config ───────────────────────────────────────────────────────────────────

def parse_remote(url: str) -> dict:
    """Split a git remote into base_url/owner/repo. Returns {} when unparseable.

    HTTPS remotes keep their scheme, host and port. SSH remotes give only the
    host, so https is assumed; pass --base-url when the web port differs.
    """
    url = (url or "").strip()
    if not url:
        return {}
    if url.startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if not host:
            return {}
        netloc = host + (f":{parts.port}" if parts.port else "")
        base, path = f"{parts.scheme}://{netloc}", parts.path
    else:
        match = _SSH_REMOTE.match(url)
        if not match:
            return {}
        base, path = f"https://{match.group('host')}", match.group("path")
    segments = [s for s in path.strip("/").split("/") if s]
    if len(segments) < 2:
        return {}
    repo = segments[-1][:-4] if segments[-1].endswith(".git") else segments[-1]
    return {"base_url": base, "owner": segments[-2], "repo": repo}


def _detect_remote(remote: str = "origin") -> dict:
    if not shutil.which("git"):
        return {}
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", remote],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_remote(proc.stdout) if proc.returncode == 0 else {}


def _validate_config(config: dict) -> dict:
    base = config.get("base_url")
    if not isinstance(base, str) or not base.startswith(("http://", "https://")):
        raise CommandError("base_url must start with http:// or https://")
    parts = urllib.parse.urlsplit(base)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise CommandError("base_url must be a plain forge address with no credentials or query")
    for key in ("owner", "repo"):
        value = config.get(key)
        if not isinstance(value, str) or not _SLUG.match(value):
            raise CommandError(f"{key} is missing or not a valid forge name", {"field": key})
    if not isinstance(config.get("token_env"), str) or not _ENV_NAME.match(config["token_env"]):
        raise CommandError("token_env must be the NAME of an environment variable, not a token")
    if not isinstance(config.get("intake_label"), str) or not config["intake_label"].strip():
        raise CommandError("intake_label must be a non-empty label name")
    return config


def load_config() -> dict | None:
    """Read the forge config. None when the project has not configured a forge."""
    if not CONFIG_PATH.is_file():
        return None
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise CommandError(f"{CONFIG_PATH} is not readable JSON: {exc}")
    if not isinstance(raw, dict):
        raise CommandError(f"{CONFIG_PATH} must hold a JSON object")
    return _validate_config(raw)


def run_configure(base_url: str | None = None, owner: str | None = None,
                  repo: str | None = None, token_env: str | None = None,
                  label: str | None = None, install_template: bool = False) -> dict:
    """Persist the forge config. Anything not given is kept, then auto-detected."""
    existing = {}
    if CONFIG_PATH.is_file():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
    detected = _detect_remote()
    config = {
        "forge": "forgejo",
        "base_url": (base_url or existing.get("base_url") or detected.get("base_url") or "").rstrip("/"),
        "owner": owner or existing.get("owner") or detected.get("owner") or "",
        "repo": repo or existing.get("repo") or detected.get("repo") or "",
        "token_env": token_env or existing.get("token_env") or DEFAULT_TOKEN_ENV,
        "intake_label": label or existing.get("intake_label") or DEFAULT_LABEL,
    }
    if not config["base_url"] or not config["owner"] or not config["repo"]:
        raise CommandError(
            "could not work out the forge repository; pass --base-url, --owner and --repo",
            {"detected": detected},
        )
    _validate_config(config)
    atomic_write(CONFIG_PATH, json.dumps(config, indent=2) + "\n")

    warnings = []
    host = urllib.parse.urlsplit(config["base_url"]).hostname or ""
    if config["base_url"].startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        warnings.append("base_url is plain http: the access token would travel unencrypted")
    if not os.environ.get(config["token_env"]):
        warnings.append(
            f"${config['token_env']} is not set: only public repositories can be read, "
            "and claim comments cannot be posted"
        )
    result = {"ok": True, "config_path": str(CONFIG_PATH), "config": config, "warnings": warnings}
    if install_template:
        result["issue_template"] = install_issue_template()
    return result


def install_issue_template() -> dict:
    """Copy the feature-request issue template into the project. Never overwrites."""
    if not ISSUE_TEMPLATE_SRC.is_file():
        raise CommandError(f"bundled issue template is missing: {ISSUE_TEMPLATE_SRC}")
    if ISSUE_TEMPLATE_DEST.exists():
        return {"path": str(ISSUE_TEMPLATE_DEST), "installed": False, "reason": "already exists"}
    atomic_write(ISSUE_TEMPLATE_DEST, ISSUE_TEMPLATE_SRC.read_text())
    return {"path": str(ISSUE_TEMPLATE_DEST), "installed": True}


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def _api_url(config: dict, path: str, query: dict | None = None) -> str:
    owner = urllib.parse.quote(config["owner"], safe="")
    repo = urllib.parse.quote(config["repo"], safe="")
    url = f"{config['base_url'].rstrip('/')}/api/v1/repos/{owner}/{repo}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _request(config: dict, method: str, path: str, query: dict | None = None,
             payload: dict | None = None, opener=None):
    """One forge API call. The token goes in a header, never in the URL."""
    url = _api_url(config, path, query)
    headers = {"Accept": "application/json", "User-Agent": "prd-taskmaster-forge"}
    token = os.environ.get(config["token_env"], "").strip()
    if token:
        headers["Authorization"] = f"token {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403, 404):
            hint = (
                f" Check that ${config['token_env']} holds a token with read access to issues"
                + (" and write access for comments" if method != "GET" else "")
                + f", and that {config['owner']}/{config['repo']} exists on {config['base_url']}."
            )
        raise CommandError(
            f"forge returned HTTP {exc.code} for {method} {path}.{hint}",
            {"status": exc.code, "token_present": bool(token)},
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CommandError(f"could not reach the forge at {config['base_url']}: {exc}")
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise CommandError(f"forge returned a non-JSON response for {method} {path}")


# ─── Claims (which issues this project already picked up) ─────────────────────

def load_claims() -> dict:
    if not CLAIMS_PATH.is_file():
        return {}
    try:
        raw = json.loads(CLAIMS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    claimed = raw.get("claimed") if isinstance(raw, dict) else None
    return claimed if isinstance(claimed, dict) else {}


def _update_claims(mutate) -> None:
    def transform(current: str) -> str:
        try:
            data = json.loads(current) if current.strip() else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        claimed = data.get("claimed")
        if not isinstance(claimed, dict):
            claimed = {}
        mutate(claimed)
        data["claimed"] = claimed
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    locked_update(CLAIMS_PATH, transform)


# ─── Intake ───────────────────────────────────────────────────────────────────

def _label_names(issue: dict) -> list[str]:
    names = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else None
        if isinstance(name, str):
            names.append(name)
    return names


def _scoped(names: list[str], scope: str) -> list[str]:
    return [n for n in names if n.startswith(scope + "/")]


def _priority_rank(names: list[str]) -> int:
    ranks = [PRIORITY_ORDER.index(n) for n in names if n in PRIORITY_ORDER]
    return min(ranks) if ranks else len(PRIORITY_ORDER)


def summarize_issue(issue: dict) -> dict:
    names = _label_names(issue)
    body = issue.get("body") or ""
    truncated = len(body) > MAX_BODY_CHARS
    priorities = [n for n in PRIORITY_ORDER if n in names]
    user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
    return {
        "number": issue.get("number"),
        "title": issue.get("title") or "",
        "url": issue.get("html_url") or "",
        "author": user.get("login") or "",
        "kind": _scoped(names, "Kind"),
        "priority": priorities[0] if priorities else None,
        "breaking": "Compat/Breaking" in names,
        "labels": names,
        "updated_at": issue.get("updated_at") or "",
        "body": body[:MAX_BODY_CHARS],
        "body_truncated": truncated,
    }


def fetch_labelled_issues(config: dict, label: str, opener=None) -> list[dict]:
    """Every open issue (pull requests excluded) carrying the label."""
    found = []
    for page in range(1, MAX_PAGES + 1):
        batch = _request(
            config, "GET", "/issues",
            query={"state": "open", "type": "issues", "labels": label,
                   "page": page, "limit": PAGE_LIMIT},
            opener=opener,
        )
        if not isinstance(batch, list):
            raise CommandError("forge returned an unexpected issue list")
        # Older servers ignore type=issues; the labels filter can also be loose
        # on some versions. Re-check both locally so the result is exact.
        for issue in batch:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            if label in _label_names(issue):
                found.append(issue)
        if len(batch) < PAGE_LIMIT:
            break
    return found


def run_intake(label: str | None = None, include_claimed: bool = False, opener=None) -> dict:
    """List approved requests, highest priority first.

    Not configured is NOT an error: the engine stays zero-config and the caller
    simply continues to Discovery with whatever goal the user gave.
    """
    config = load_config()
    if config is None:
        return {
            "ok": True, "configured": False, "count": 0, "issues": [],
            "hint": "run forge-configure to pull approved feature requests from Forgejo",
        }
    wanted = label or config["intake_label"]
    claims = load_claims()
    ready, skipped, already_claimed = [], [], []
    for raw in fetch_labelled_issues(config, wanted, opener=opener):
        item = summarize_issue(raw)
        holds = [n for n in item["labels"] if n in HOLD_LABELS]
        if holds:
            skipped.append({"number": item["number"], "title": item["title"],
                            "url": item["url"], "reason": ", ".join(holds)})
        elif str(item["number"]) in claims and not include_claimed:
            already_claimed.append({"number": item["number"], "title": item["title"],
                                    "url": item["url"],
                                    "claimed_at": claims[str(item["number"])].get("claimed_at")})
        else:
            ready.append(item)
    ready.sort(key=lambda i: (_priority_rank(i["labels"]), i["number"] or 0))
    return {
        "ok": True,
        "configured": True,
        "repository": f"{config['base_url']}/{config['owner']}/{config['repo']}",
        "label": wanted,
        "count": len(ready),
        "issues": ready,
        "skipped": skipped,
        "already_claimed": already_claimed,
    }


def run_claim(issue: int, comment: bool = False, note: str = "", opener=None) -> dict:
    """Record that this project picked the issue up; optionally say so on the issue."""
    config = load_config()
    if config is None:
        raise CommandError("forge is not configured; run forge-configure first")
    if issue < 1:
        raise CommandError("issue must be a positive issue number")
    raw = _request(config, "GET", f"/issues/{issue}", opener=opener)
    if not isinstance(raw, dict) or raw.get("pull_request"):
        raise CommandError(f"#{issue} is not an issue in {config['owner']}/{config['repo']}")
    item = summarize_issue(raw)
    if raw.get("state") != "open":
        raise CommandError(f"#{issue} is {raw.get('state')}, not open")
    if config["intake_label"] not in item["labels"]:
        raise CommandError(
            f"#{issue} does not carry {config['intake_label']}; only approved requests can be claimed",
            {"labels": item["labels"]},
        )
    record = {"title": item["title"], "url": item["url"], "claimed_at": now_iso()}
    _update_claims(lambda claimed: claimed.__setitem__(str(issue), record))

    commented = False
    if comment:
        text = "Picked up by prd-taskmaster: discovery and planning have started for this request."
        if note.strip():
            text += "\n\n" + note.strip()
        _request(config, "POST", f"/issues/{issue}/comments", payload={"body": text}, opener=opener)
        commented = True
    return {"ok": True, "claimed": issue, "issue": item, "commented": commented,
            "claims_path": str(CLAIMS_PATH)}


def run_release(issue: int) -> dict:
    """Forget a claim so the issue shows up in intake again."""
    released = []

    def mutate(claimed: dict) -> None:
        if claimed.pop(str(issue), None) is not None:
            released.append(issue)

    _update_claims(mutate)
    return {"ok": True, "released": bool(released), "issue": issue}


# ─── CLI wrappers ─────────────────────────────────────────────────────────────

def _run(core, *args, **kwargs) -> None:
    try:
        result = core(*args, **kwargs)
    except CommandError as exc:
        fail(exc.message, **exc.extra)
        return
    emit(result)


def cmd_forge_configure(args) -> None:
    _run(run_configure, base_url=args.base_url, owner=args.owner, repo=args.repo,
         token_env=args.token_env, label=args.label, install_template=args.install_template)


def cmd_forge_intake(args) -> None:
    _run(run_intake, label=args.label, include_claimed=args.include_claimed)


def cmd_forge_claim(args) -> None:
    _run(run_claim, args.issue, comment=args.comment, note=args.note)


def cmd_forge_release(args) -> None:
    _run(run_release, args.issue)
