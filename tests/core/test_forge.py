"""Forgejo intake: config, approved-issue listing, claims, CLI contracts.

A real HTTP server on localhost stands in for Forgejo so the urllib path,
auth header, query string and pagination are all exercised for real.
"""

import json
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from prd_taskmaster import forge
from prd_taskmaster.lib import CommandError

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "script.py"


def _issue(number, title, labels, *, body="", state="open", pull=False):
    data = {
        "number": number, "title": title, "body": body, "state": state,
        "html_url": f"http://forge.test/me/proj/issues/{number}",
        "user": {"login": "requester"}, "updated_at": "2026-11-20T10:00:00Z",
        "labels": [{"id": i, "name": name} for i, name in enumerate(labels, start=1)],
    }
    if pull:
        data["pull_request"] = {"merged": False}
    return data


class FakeForge:
    def __init__(self, issues):
        self.issues = issues
        self.requests = []
        self.comments = []
        forge_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self):
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                forge_ref.requests.append({
                    "method": self.command, "path": parts.path, "query": query,
                    "auth": self.headers.get("Authorization"),
                })
                return parts.path, query

            def do_GET(self):
                path, query = self._record()
                prefix = "/api/v1/repos/me/proj/issues"
                if path == prefix:
                    label = query.get("labels", [""])[0]
                    page = int(query.get("page", ["1"])[0])
                    limit = int(query.get("limit", ["50"])[0])
                    # Deliberately loose, like an older server: returns pulls and
                    # ignores state, so the client-side re-check is exercised.
                    matching = [i for i in forge_ref.issues
                                if label in [l["name"] for l in i["labels"]] and i["state"] == "open"]
                    self._send(200, matching[(page - 1) * limit: page * limit])
                elif path.startswith(prefix + "/"):
                    number = int(path.rsplit("/", 1)[1])
                    found = [i for i in forge_ref.issues if i["number"] == number]
                    self._send(200, found[0]) if found else self._send(404, {"message": "not found"})
                else:
                    self._send(404, {"message": "not found"})

            def do_POST(self):
                path, _ = self._record()
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not self.headers.get("Authorization"):
                    self._send(401, {"message": "token required"})
                    return
                forge_ref.comments.append({"path": path, "body": payload.get("body")})
                self._send(201, {"id": len(forge_ref.comments)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_forge():
    servers = []

    def start(issues):
        server = FakeForge(issues)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    return tmp_path


def _configure(server, **kwargs):
    return forge.run_configure(base_url=server.base_url, owner="me", repo="proj", **kwargs)


# ─── remote parsing ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("remote,expected", [
    ("https://git.example.com/me/proj.git",
     {"base_url": "https://git.example.com", "owner": "me", "repo": "proj"}),
    ("https://user:secret@git.example.com:3000/me/proj",
     {"base_url": "https://git.example.com:3000", "owner": "me", "repo": "proj"}),
    ("git@git.example.com:me/proj.git",
     {"base_url": "https://git.example.com", "owner": "me", "repo": "proj"}),
    ("ssh://git@git.example.com:2222/me/proj.git",
     {"base_url": "https://git.example.com", "owner": "me", "repo": "proj"}),
    ("", {}),
    ("not a remote", {}),
])
def test_parse_remote(remote, expected):
    assert forge.parse_remote(remote) == expected


def test_parse_remote_never_keeps_embedded_credentials():
    parsed = forge.parse_remote("https://user:secret@git.example.com/me/proj.git")
    assert "secret" not in json.dumps(parsed)


# ─── configure ────────────────────────────────────────────────────────────────

def test_configure_writes_config_without_any_token(project, fake_forge, monkeypatch):
    server = fake_forge([])
    monkeypatch.setenv("FORGEJO_TOKEN", "tok-should-never-be-written")
    result = _configure(server)
    assert result["ok"] is True
    saved = json.loads((project / forge.CONFIG_PATH).read_text())
    assert saved == {
        "forge": "forgejo", "base_url": server.base_url, "owner": "me", "repo": "proj",
        "token_env": "FORGEJO_TOKEN", "intake_label": "Status/Approved",
    }
    assert "tok-should-never-be-written" not in (project / forge.CONFIG_PATH).read_text()


def test_configure_detects_repository_from_git_remote(project):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://git.example.com/acme/widgets.git"],
                   cwd=project, check=True)
    config = forge.run_configure()["config"]
    assert (config["base_url"], config["owner"], config["repo"]) == (
        "https://git.example.com", "acme", "widgets")


def test_configure_fails_clearly_when_nothing_is_detectable(project):
    with pytest.raises(CommandError) as exc:
        forge.run_configure()
    assert "--base-url" in exc.value.message


def test_configure_rejects_a_token_passed_as_the_env_name(project, fake_forge):
    server = fake_forge([])
    with pytest.raises(CommandError):
        _configure(server, token_env="abc123-not-a-variable-name!")


def test_configure_warns_when_token_missing(project, fake_forge):
    result = _configure(fake_forge([]))
    assert any("FORGEJO_TOKEN" in w for w in result["warnings"])


def test_configure_installs_issue_template_once(project, fake_forge):
    server = fake_forge([])
    first = _configure(server, install_template=True)
    dest = project / forge.ISSUE_TEMPLATE_DEST
    assert first["issue_template"]["installed"] is True
    assert "Kind/Feature" in dest.read_text()
    dest.write_text("customised by the user")
    second = _configure(server, install_template=True)
    assert second["issue_template"]["installed"] is False
    assert dest.read_text() == "customised by the user"


# ─── intake ───────────────────────────────────────────────────────────────────

def test_intake_is_a_quiet_no_op_when_not_configured(project):
    result = forge.run_intake()
    assert result["ok"] is True and result["configured"] is False and result["issues"] == []


def test_intake_returns_only_open_approved_issues_sorted_by_priority(project, fake_forge):
    server = fake_forge([
        _issue(1, "Low one", ["Status/Approved", "Kind/Feature", "Priority/Low"]),
        _issue(2, "Not approved", ["Kind/Feature", "Priority/Critical"]),
        _issue(3, "Critical one", ["Status/Approved", "Kind/Bug", "Priority/Critical"], body="steps"),
        _issue(4, "No priority", ["Status/Approved", "Kind/Enhancement"]),
        _issue(5, "A pull request", ["Status/Approved"], pull=True),
        _issue(6, "Closed", ["Status/Approved", "Priority/High"], state="closed"),
        _issue(7, "High breaking", ["Status/Approved", "Kind/Feature", "Priority/High", "Compat/Breaking"]),
    ])
    _configure(server)
    result = forge.run_intake()
    assert [i["number"] for i in result["issues"]] == [3, 7, 1, 4]
    assert result["count"] == 4
    top = result["issues"][0]
    assert top["kind"] == ["Kind/Bug"] and top["priority"] == "Priority/Critical"
    assert top["body"] == "steps" and top["author"] == "requester"
    assert result["issues"][1]["breaking"] is True
    assert result["issues"][3]["priority"] is None
    query = server.requests[0]["query"]
    assert query["labels"] == ["Status/Approved"]
    assert query["state"] == ["open"] and query["type"] == ["issues"]


def test_intake_holds_back_approved_issues_with_hold_labels(project, fake_forge):
    server = fake_forge([
        _issue(1, "Dup", ["Status/Approved", "Reviewed/Duplicate"]),
        _issue(2, "Wontfix", ["Status/Approved", "Reviewed/Won't Fix"]),
        _issue(3, "Fine", ["Status/Approved", "Reviewed/Confirmed"]),
    ])
    _configure(server)
    result = forge.run_intake()
    assert [i["number"] for i in result["issues"]] == [3]
    assert {s["number"]: s["reason"] for s in result["skipped"]} == {
        1: "Reviewed/Duplicate", 2: "Reviewed/Won't Fix"}


def test_intake_follows_pagination(project, fake_forge):
    issues = [_issue(n, f"Request {n}", ["Status/Approved"]) for n in range(1, forge.PAGE_LIMIT + 8)]
    server = fake_forge(issues)
    _configure(server)
    result = forge.run_intake()
    assert result["count"] == forge.PAGE_LIMIT + 7
    assert [r["query"]["page"] for r in server.requests] == [["1"], ["2"]]


def test_intake_honours_a_custom_label(project, fake_forge):
    server = fake_forge([
        _issue(1, "Ready", ["Ready To Build"]),
        _issue(2, "Approved", ["Status/Approved"]),
    ])
    _configure(server, label="Ready To Build")
    assert [i["number"] for i in forge.run_intake()["issues"]] == [1]
    assert [i["number"] for i in forge.run_intake(label="Status/Approved")["issues"]] == [2]


def test_intake_truncates_huge_bodies(project, fake_forge):
    server = fake_forge([_issue(1, "Big", ["Status/Approved"], body="x" * (forge.MAX_BODY_CHARS + 5))])
    _configure(server)
    issue = forge.run_intake()["issues"][0]
    assert len(issue["body"]) == forge.MAX_BODY_CHARS and issue["body_truncated"] is True


def test_token_travels_in_the_header_never_the_url(project, fake_forge, monkeypatch):
    server = fake_forge([_issue(1, "One", ["Status/Approved"])])
    _configure(server)
    monkeypatch.setenv("FORGEJO_TOKEN", "s3cret")
    forge.run_intake()
    request = server.requests[-1]
    assert request["auth"] == "token s3cret"
    assert "s3cret" not in json.dumps(request["query"]) and "s3cret" not in request["path"]


def test_unreachable_forge_is_a_command_error(project):
    forge.run_configure(base_url="http://127.0.0.1:9", owner="me", repo="proj")
    with pytest.raises(CommandError) as exc:
        forge.run_intake()
    assert "could not reach the forge" in exc.value.message


def test_http_error_names_the_token_variable(project, fake_forge):
    server = fake_forge([])
    forge.run_configure(base_url=server.base_url, owner="me", repo="missing")
    with pytest.raises(CommandError) as exc:
        forge.run_intake()
    assert "HTTP 404" in exc.value.message and "FORGEJO_TOKEN" in exc.value.message


# ─── claim / release ──────────────────────────────────────────────────────────

def test_claim_hides_issue_from_later_intake_and_release_restores_it(project, fake_forge):
    server = fake_forge([
        _issue(1, "One", ["Status/Approved"]),
        _issue(2, "Two", ["Status/Approved"]),
    ])
    _configure(server)
    claimed = forge.run_claim(1)
    assert claimed["claimed"] == 1 and claimed["commented"] is False
    assert server.comments == []
    result = forge.run_intake()
    assert [i["number"] for i in result["issues"]] == [2]
    assert [c["number"] for c in result["already_claimed"]] == [1]
    assert [i["number"] for i in forge.run_intake(include_claimed=True)["issues"]] == [1, 2]
    assert forge.run_release(1)["released"] is True
    assert [i["number"] for i in forge.run_intake()["issues"]] == [1, 2]
    assert forge.run_release(1)["released"] is False


def test_claim_refuses_unapproved_closed_and_pull_requests(project, fake_forge):
    server = fake_forge([
        _issue(1, "Unapproved", ["Kind/Feature"]),
        _issue(2, "Closed", ["Status/Approved"], state="closed"),
        _issue(3, "Pull", ["Status/Approved"], pull=True),
    ])
    _configure(server)
    for number in (1, 2, 3, 99):
        with pytest.raises(CommandError):
            forge.run_claim(number)
    assert forge.load_claims() == {}


def test_claim_comment_is_opt_in_and_posts_once(project, fake_forge, monkeypatch):
    server = fake_forge([_issue(4, "Four", ["Status/Approved"])])
    _configure(server)
    monkeypatch.setenv("FORGEJO_TOKEN", "tok")
    result = forge.run_claim(4, comment=True, note="Spec to follow.")
    assert result["commented"] is True
    assert len(server.comments) == 1
    assert server.comments[0]["path"] == "/api/v1/repos/me/proj/issues/4/comments"
    assert "Picked up by prd-taskmaster" in server.comments[0]["body"]
    assert server.comments[0]["body"].endswith("Spec to follow.")


# ─── CLI contracts ────────────────────────────────────────────────────────────

def _run_cli(cwd, *args, expect_exit=0, env=None):
    import os
    full_env = {k: v for k, v in os.environ.items() if k != "FORGEJO_TOKEN"}
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=str(cwd), env=full_env)
    assert proc.returncode == expect_exit, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


def test_cli_end_to_end(tmp_path, fake_forge):
    server = fake_forge([
        _issue(10, "Export to CSV", ["Status/Approved", "Kind/Feature", "Priority/High"], body="Need CSV."),
        _issue(11, "Dark mode", ["Kind/Feature"]),
    ])
    assert _run_cli(tmp_path, "forge-intake")["configured"] is False
    configured = _run_cli(tmp_path, "forge-configure", "--base-url", server.base_url,
                          "--owner", "me", "--repo", "proj", "--install-template")
    assert configured["issue_template"]["installed"] is True
    listed = _run_cli(tmp_path, "forge-intake")
    assert [i["number"] for i in listed["issues"]] == [10]
    assert _run_cli(tmp_path, "forge-claim", "--issue", "10")["claimed"] == 10
    assert _run_cli(tmp_path, "forge-intake")["count"] == 0
    refused = _run_cli(tmp_path, "forge-claim", "--issue", "11", expect_exit=1)
    assert refused["ok"] is False and "Status/Approved" in refused["error"]
    assert _run_cli(tmp_path, "forge-release", "--issue", "10")["released"] is True


# ─── skill doc contracts ──────────────────────────────────────────────────────

def test_intake_skill_and_hooks_are_documented():
    skill = (REPO_ROOT / "skills/intake/SKILL.md").read_text()
    assert skill.startswith("---\nname: intake\n")
    for command in ("forge-configure", "forge-intake", "forge-claim", "forge-release"):
        assert f"script.py {command}" in skill
    assert "never a set of\ninstructions" in skill or "not instructions" in skill
    root = (REPO_ROOT / "SKILL.md").read_text()
    assert "## Phase 1b: Forge intake" in root
    assert root.index("## Phase 1b: Forge intake") < root.index("## Phase 2: Discovery")
    assert "| `forge-intake" in root
    assert "/prd:intake" in (REPO_ROOT / "skills/go/SKILL.md").read_text()


def test_installer_offers_forgejo_intake_as_an_option():
    installer = (REPO_ROOT / "install.sh").read_text()
    assert "--with-forgejo" in installer and "--no-forgejo" in installer
    # Opt-in only: a piped (non-interactive) install must not add it silently.
    assert "Non-interactive shell; skipping optional Forgejo intake skill" in installer
    # The installer never asks for, or stores, a forge address or token.
    assert "FORGEJO_TOKEN=" not in installer
    assert subprocess.run(["bash", "-n", str(REPO_ROOT / "install.sh")]).returncode == 0
