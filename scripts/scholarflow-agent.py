"""Portable operator entry point; all conversations still run in native Pi."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scholarflow.nature_skill_package import NatureSkillPackage
from scholarflow.source_inventory import SourceInventory


def default_home() -> Path:
    if os.environ.get("SCHOLARFLOW_HOME"): return Path(os.environ["SCHOLARFLOW_HOME"]).absolute()
    return (Path(os.environ["LOCALAPPDATA"]) / "ScholarFlow").absolute() if os.environ.get("LOCALAPPDATA") else (Path.home() / ".scholarflow").absolute()


def node_command() -> str:
    override = os.environ.get("SCHOLARFLOW_NODE")
    candidates = [override] if override else [str(Path(part) / ("node.exe" if os.name == "nt" else "node")) for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    for candidate in dict.fromkeys(candidates):
        if not candidate or not Path(candidate).is_file(): continue
        try:
            version = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip().lstrip("v")
            if re.fullmatch(r"\d+\.\d+\.\d+", version) and tuple(map(int, version.split("."))) >= (22, 19, 0): return candidate
        except (OSError, subprocess.SubprocessError): pass
    raise ValueError("Node.js >= 22.19.0 is required; set SCHOLARFLOW_NODE to its executable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ScholarFlow Pi Agent — local operator entry point")
    parser.add_argument("--home", type=Path, default=default_home(), help="user data directory (default: LOCALAPPDATA/ScholarFlow)")
    parser.add_argument("--library-home", type=Path, help="explicit existing library home; never copied or migrated")
    parser.add_argument("--workspace-home", type=Path, help="explicit existing workspace directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check prerequisites without starting services")
    auth = commands.add_parser("auth", help="configure a user-local Pi provider credential"); a = auth.add_subparsers(dest="action", required=True)
    a.add_parser("status")
    set_key = a.add_parser("set-deepseek"); set_key.add_argument("--replace", action="store_true")
    projects = commands.add_parser("projects"); p = projects.add_subparsers(dest="action", required=True)
    p.add_parser("list")
    preview = p.add_parser("preview"); preview.add_argument("--name", required=True); preview.add_argument("--title", required=True); preview.add_argument("--goal", required=True)
    commit = p.add_parser("commit"); commit.add_argument("--request-id", required=True); commit.add_argument("--reviewed-hash", required=True)
    workspaces = commands.add_parser("workspaces"); w = workspaces.add_subparsers(dest="action", required=True)
    w.add_parser("list")
    prepare = w.add_parser("prepare"); prepare.add_argument("--project", required=True); prepare.add_argument("--provider", choices=["deepseek", "scholarflow-local"], required=True); prepare.add_argument("--pdf", type=Path); prepare.add_argument("--pages", default="1")
    proposals = w.add_parser("proposals"); proposals.add_argument("--workspace", type=Path, required=True)
    decide = w.add_parser("approve"); decide.add_argument("--workspace", type=Path, required=True); decide.add_argument("--proposal-id", required=True); decide.add_argument("--reviewed-hash", required=True)
    sources = commands.add_parser("sources"); s = sources.add_subparsers(dest="action", required=True)
    s.add_parser("list")
    add = s.add_parser("add"); add.add_argument("--project", required=True); add.add_argument("--id", required=True); add.add_argument("--path", type=Path, required=True); add.add_argument("--kind", action="append", choices=["md", "pdf"], required=True); add.add_argument("--auto", action="store_true", help="refresh this root when starting Pi")
    scan = s.add_parser("scan"); scan.add_argument("--auto-only", action="store_true")
    nature = commands.add_parser("nature"); n = nature.add_subparsers(dest="action", required=True)
    n.add_parser("status")
    n.add_parser("check", help="compare the fixed revision with upstream without updating")
    install = n.add_parser("install"); group = install.add_mutually_exclusive_group(required=True); group.add_argument("--checkout", type=Path); group.add_argument("--fetch", action="store_true")
    activate = n.add_parser("activate"); activate.add_argument("--version", required=True); activate.add_argument("--replace", action="store_true")
    knowledge = commands.add_parser("knowledge", help="inspect or explicitly run a reviewed note's index job"); k = knowledge.add_subparsers(dest="action", required=True)
    for action in ("status", "enqueue", "run"):
        operation = k.add_parser(action); operation.add_argument("--workspace", type=Path, required=True)
    k.add_parser("embedding-status")
    config = k.add_parser("configure-embedding"); config.add_argument("--endpoint", required=True); config.add_argument("--model-sha256", required=True); config.add_argument("--backend", choices=["cpu", "gpu"], default="cpu"); config.add_argument("--parity-verified", action="store_true"); config.add_argument("--replace", action="store_true")
    pi = commands.add_parser("pi", help="open an existing, explicitly scoped Pi research workspace"); pi.add_argument("--workspace", type=Path, required=True); pi.add_argument("--status", action="store_true")
    web = commands.add_parser("web", help="start the optional local-only reading desk"); web.add_argument("--no-browser", action="store_true"); web.add_argument("--probe-only", action="store_true"); web.add_argument("--api-port", type=int, default=8787); web.add_argument("--web-port", type=int, default=3000)
    return parser


def run(args) -> object:
    home = Path(args.home).absolute()
    library = args.library_home.absolute() if args.library_home else home / "library"
    facts = library / "facts"
    workspace_home = args.workspace_home.absolute() if args.workspace_home else home / "workspaces"
    # Propagated only to this process and its children, never to machine settings.
    os.environ.update(SCHOLARFLOW_HOME=str(home), SCHOLARFLOW_LIBRARY_HOME=str(library), SCHOLARFLOW_LIBRARY_FACTS_DIR=str(facts), SCHOLARFLOW_WORKSPACE_HOME=str(workspace_home), SCHOLARFLOW_RUNTIME_DIR=str(home / "runtime-state"), SCHOLARFLOW_PORTABLE_KNOWLEDGE="1", SCHOLARFLOW_INVENTORY_MODE="configured", SCHOLARFLOW_PYTHON=sys.executable, PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    if args.command == "doctor":
        try: node = node_command(); node_ok = True
        except ValueError: node = None; node_ok = False
        pi_cli = ROOT / "packages/scholarflow-pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
        return {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 11), "node": node, "node_ok": node_ok, "pi_installed": pi_cli.is_file(), "data_home": str(home), "starts_services": False}
    if args.command == "auth":
        from scholarflow import portable_auth
        if args.action == "status": return portable_auth.status(home)
        import getpass
        if not sys.stdin.isatty(): raise ValueError("An interactive terminal is required to enter a credential safely")
        print("The key will be saved as plaintext in your user-local Pi auth file. Do not paste it into chat.", file=sys.stderr)
        return portable_auth.set_deepseek(home, getpass.getpass("DeepSeek API key: "), replace=args.replace)
    if args.command == "projects":
        from scholarflow import research_workspace as rw
        if args.action == "list": return rw.projects(facts) if facts.is_dir() else []
        if args.action == "preview":
            facts.mkdir(parents=True, exist_ok=True)
            return rw.preview_project(facts, project_name=args.name, title=args.title, goal=args.goal)
        return rw.commit_project(facts, args.request_id, args.reviewed_hash)
    if args.command == "workspaces":
        from scholarflow import research_workspace as rw
        if args.action == "list":
            result = []
            for path in sorted(workspace_home.glob("workspace-*/workspace.json")) if workspace_home.is_dir() else []:
                try:
                    value = rw.load(path)
                    if Path(value["data_dir"]) == facts: result.append({"workspace": str(path), "stage_id": value["stage_id"], "title": value["project_title"], "provider": value["provider"]})
                except (ValueError, OSError, KeyError): continue
            return result
        if args.action == "prepare": return {"workspace": str(rw.prepare(facts, args.project, pdf=args.pdf, pages=args.pages, provider=args.provider)), "model_calls": 0}
        if args.action == "proposals": return rw.proposals(args.workspace)
        result = rw.decide(args.workspace, args.proposal_id, args.reviewed_hash)
        if result["phase"] == "committed":
            from scholarflow import workspace_knowledge as wk
            try: result["knowledge"] = {"status": wk.enqueue(args.workspace)["status"], "requires_worker": True}
            except (ValueError, OSError) as error: result["knowledge"] = {"status": "not_queued", "error": str(error), "note_saved": True}
        return result
    if args.command == "sources":
        registry = SourceInventory(library)
        if args.action == "list": return registry._config()
        if args.action == "add": return registry.register(args.path, project=args.project, source_id=args.id, kinds=args.kind, auto_scan=args.auto)
        result = registry.scan(auto_only=args.auto_only)
        return {"status": result.get("status", "complete"), "counts": result["counts"], "model_calls": 0, "source_files_modified": False}
    if args.command == "nature":
        manager = NatureSkillPackage(home)
        if args.action == "status": return manager.status()
        if args.action == "check": return manager.check_upstream()
        if args.action == "activate": return manager.activate(args.version, replace=args.replace)
        return {"installed": manager.install(manager.fetch() if args.fetch else args.checkout), "activated": False}
    if args.command == "knowledge":
        from scholarflow import portable_knowledge
        if args.action == "embedding-status": return portable_knowledge.status(library)
        if args.action == "configure-embedding": return portable_knowledge.configure(library, args.endpoint, model_sha256=args.model_sha256, backend=args.backend, parity_verified=args.parity_verified, replace=args.replace)
        command = [sys.executable, "-s", "-B", str(ROOT / "scripts/workspace-knowledge.py"), args.action, "--workspace", str(args.workspace.absolute())]
        return subprocess.call(command, cwd=ROOT)
    if args.command in {"pi", "web"}:
        from scholarflow import portable_auth
        local_auth = portable_auth.auth_file(home)
        if local_auth.exists() and not os.environ.get("SCHOLARFLOW_PI_AUTH_FILE"):
            portable_auth.status(home)  # Validate before handing the location to Pi.
            os.environ["SCHOLARFLOW_PI_AUTH_FILE"] = str(local_auth)
        if (home / "skills/nature-skills/active.json").exists():
            NatureSkillPackage(home).status()  # Fail closed before child process starts.
            os.environ["SCHOLARFLOW_NATURE_HOME"] = str(home)
        else:
            os.environ.pop("SCHOLARFLOW_NATURE_HOME", None)
        registry = SourceInventory(library)
        read_only_probe = (args.command == "pi" and args.status) or (args.command == "web" and args.probe_only)
        if registry.config_path.exists() and not read_only_probe:
            refresh = registry.scan(auto_only=True)
            if refresh.get("status") != "no_automatic_roots": print("Configured automatic source inventory refreshed; no model calls or indexing.", file=sys.stderr)
        if args.command == "pi":
            command = [node_command(), str(ROOT / "packages/scholarflow-pi/scripts/research-chat.mjs"), "--workspace", str(args.workspace.absolute())]
            if args.status: command.append("--status")
        else:
            if os.name != "nt": raise ValueError("The optional desktop launcher currently requires Windows")
            command = ["powershell", "-NoProfile", "-File", str(ROOT / "scripts/research-desk-runtime.ps1"), "-DataDir", str(facts), "-WorkspaceHome", str(workspace_home), "-ApiPort", str(args.api_port), "-WebPort", str(args.web_port)]
            if args.no_browser: command.append("-NoBrowser")
            if args.probe_only: command.append("-ProbeOnly")
        return subprocess.call(command, cwd=ROOT)
    raise ValueError("Unknown command")


def main() -> int:
    try:
        result = run(build_parser().parse_args())
        if isinstance(result, int): return result
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
