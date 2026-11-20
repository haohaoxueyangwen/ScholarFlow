"""Governed local LaTeX manuscript workspaces.

Local files and Git own manuscript revisions. ScholarFlow owns the controlled
workspace catalog, immutable proposals, stale checks and audit receipts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .event_bus import append_event
from .mutations import MutationError, required_text
from .persistence import atomic_write_json, file_lock
from .proposal_state import (
    load_proposal_state,
    raise_if_rejected,
    save_proposal_state,
    valid_confirmation,
)
from .storage import ScholarFlowStore

MANUSCRIPT_SCHEMA_VERSION = 1
PROPOSAL_TTL = timedelta(hours=24)
SECTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MANUSCRIPT_ID_RE = re.compile(r"^manuscript-[a-z0-9][a-z0-9-]{0,62}$")
SECTION_BEGIN_RE = re.compile(r"^\s*%\s*sf-section:([a-z][a-z0-9_-]{0,63}):begin\s*$")
SECTION_END_RE = re.compile(r"^\s*%\s*sf-section:([a-z][a-z0-9_-]{0,63}):end\s*$")
FORBIDDEN_LATEX = (
    (re.compile(r"\\(?:write18|openout|openin|read|catcode|csname|usepackage)\b", re.I), "unsafe_command"),
    (re.compile(r"\\(?:input|include|includegraphics|bibliography|addbibresource)\s*\{", re.I), "external_file_reference"),
    (re.compile(r"\\(?:directlua|luaexec|ShellEscape)\b", re.I), "shell_or_lua_escape"),
)
EXPORT_EXTENSIONS = {".tex", ".bib", ".bst", ".cls", ".sty", ".png", ".jpg", ".jpeg", ".pdf", ".eps"}
COMPILER_CANDIDATES = (
    ("latexmk", ("latexmk", "-pdf", "-halt-on-error", "-interaction=nonstopmode", "-no-shell-escape")),
    ("tectonic", ("tectonic", "--untrusted", "--keep-logs")),
    ("pdflatex", ("pdflatex", "-halt-on-error", "-interaction=nonstopmode", "-no-shell-escape")),
)


class ManuscriptService:
    def __init__(self, store: ScholarFlowStore, *, compiler_path: Path | None = None):
        self.store = store
        self.root_dir = store.root_dir
        self.catalog_path = self.root_dir / "_manuscripts" / "catalog.json"
        self.workspace_root = self.root_dir / "_manuscripts" / "workspaces"
        self.build_root = self.root_dir / "_manuscripts" / "builds"
        self.vscode_root = self.root_dir / "_manuscripts" / "vscode"
        self.compiler_path = compiler_path

    def list(self, *, project_stage_id: str | None = None) -> dict[str, Any]:
        records = list(self._catalog()["items"].values())
        if project_stage_id:
            records = [item for item in records if item.get("project_stage_id") == project_stage_id]
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"schema_version": 1, "count": len(records), "items": records, "read_only": True}

    def get(self, manuscript_id: str) -> dict[str, Any]:
        normalized = _manuscript_id(manuscript_id)
        record = self._catalog()["items"].get(normalized)
        if not isinstance(record, Mapping):
            raise MutationError("not_found", f"manuscript '{normalized}' was not found")
        current = dict(record)
        main_path = self._main_path(current)
        current["current_file_hash"] = _file_hash(main_path)
        current["git"] = self._git_status(self._workspace(normalized))
        current["sections"] = _section_outline(main_path.read_text(encoding="utf-8"))
        current["read_only"] = True
        return current

    def read_section(self, manuscript_id: str, section: str) -> dict[str, Any]:
        record = self.get(manuscript_id)
        normalized_section = _section_name(section)
        content = self._main_path(record).read_text(encoding="utf-8")
        body, start, end = _extract_section(content, normalized_section)
        return {
            "manuscript_id": record["manuscript_id"],
            "section": normalized_section,
            "latex": body,
            "file_hash": record["current_file_hash"],
            "line_start": start,
            "line_end": end,
            "read_only": True,
        }

    def validate(self, manuscript_id: str) -> dict[str, Any]:
        record = self.get(manuscript_id)
        main_path = self._main_path(record)
        content = main_path.read_text(encoding="utf-8")
        _validate_document(content)
        diagnostics: list[dict[str, Any]] = []
        for pattern, reason in FORBIDDEN_LATEX:
            if reason == "unsafe_command":
                # The generated preamble has a fixed package allowlist. Only
                # reject dangerous commands that are never valid there.
                dangerous = re.compile(r"\\(?:write18|openout|openin|read|catcode|csname)\b", re.I)
                match = dangerous.search(content)
            else:
                match = pattern.search(content)
            if match:
                diagnostics.append({"severity": "error", "code": reason, "offset": match.start()})
        compiler = _compiler_status(self.compiler_path)
        return {
            "manuscript_id": record["manuscript_id"],
            "valid": not diagnostics,
            "file_hash": record["current_file_hash"],
            "section_count": len(record["sections"]),
            "diagnostics": diagnostics,
            "compiler": compiler,
            "can_compile": bool(compiler["available"] and compiler["name"] == "tectonic" and not diagnostics),
            "can_export": not diagnostics,
            "read_only": True,
        }

    def export_upload_bundle(self, manuscript_id: str) -> tuple[str, bytes, dict[str, Any]]:
        record = self.get(manuscript_id)
        validation = self.validate(manuscript_id)
        if not validation["can_export"]:
            raise MutationError("manuscript_export_blocked", "LaTeX validation failed; upload bundle was not created")
        workspace = self._workspace(record["manuscript_id"])
        files = _export_files(workspace)
        from io import BytesIO

        buffer = BytesIO()
        with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, path in files:
                info = ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        payload = buffer.getvalue()
        if len(payload) > 100_000_000:
            raise MutationError("manuscript_export_blocked", "upload bundle exceeds 100 MB")
        metadata = {
            "manuscript_id": record["manuscript_id"],
            "file_count": len(files),
            "size_bytes": len(payload),
            "source_commit": record["git"]["commit"],
            "source_file_hash": record["current_file_hash"],
            "main_file": record["main_file"],
        }
        return f"{record['manuscript_id']}-overleaf.zip", payload, metadata

    def compile(self, manuscript_id: str) -> dict[str, Any]:
        record = self.get(manuscript_id)
        validation = self.validate(manuscript_id)
        if not validation["can_compile"]:
            raise MutationError("compiler_unavailable", str(validation["compiler"].get("reason") or "a supported compiler is required"))
        compiler = dict(validation["compiler"])
        if compiler["name"] != "tectonic":
            raise MutationError("compiler_unsupported", "the accepted compile path currently requires Tectonic")
        source_hash = str(record["current_file_hash"])
        build_dir = self._build_dir(str(record["manuscript_id"]), source_hash)
        build_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = build_dir / f"{Path(str(record['main_file'])).stem}.pdf"
        report_path = build_dir / "compile.json"
        if pdf_path.is_file() and report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["cached"] = True
            return report
        executable = str(compiler["executable"])
        command = [
            executable,
            "--untrusted",
            "--keep-logs",
            "--synctex",
            "--color=never",
            "--outdir",
            str(build_dir),
            str(self._main_path(record)),
        ]
        environment = dict(os.environ)
        environment["TECTONIC_UNTRUSTED_MODE"] = "1"
        environment["SOURCE_DATE_EPOCH"] = "1"
        started = datetime.now(timezone.utc)
        try:
            result = subprocess.run(
                command,
                cwd=self._workspace(str(record["manuscript_id"])),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
        except subprocess.TimeoutExpired as exc:
            raise MutationError("compile_timeout", "LaTeX compilation exceeded 90 seconds") from exc
        log = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-40_000:]
        if result.returncode != 0 or not pdf_path.is_file():
            raise MutationError("compile_failed", "LaTeX compilation failed", details={"log": log})
        report = {
            "manuscript_id": record["manuscript_id"],
            "status": "succeeded",
            "compiler": {"name": compiler["name"], "version": compiler.get("version")},
            "source_file_hash": source_hash,
            "source_commit": record["git"]["commit"],
            "pdf_size_bytes": pdf_path.stat().st_size,
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "shell_escape": False,
            "cached": False,
            "log_tail": log,
        }
        atomic_write_json(report_path, report)
        return report

    def compiled_pdf(self, manuscript_id: str) -> tuple[str, bytes, dict[str, Any]]:
        record = self.get(manuscript_id)
        build_dir = self._build_dir(str(record["manuscript_id"]), str(record["current_file_hash"]))
        pdf_path = build_dir / f"{Path(str(record['main_file'])).stem}.pdf"
        report_path = build_dir / "compile.json"
        if not pdf_path.is_file() or not report_path.is_file():
            raise MutationError("not_found", "the current manuscript revision has not been compiled")
        return f"{record['manuscript_id']}.pdf", pdf_path.read_bytes(), json.loads(report_path.read_text(encoding="utf-8"))

    def open_in_vscode(self, manuscript_id: str) -> dict[str, Any]:
        record = self.get(manuscript_id)
        code = shutil.which("code") or shutil.which("code.cmd")
        if not code:
            raise MutationError("vscode_unavailable", "VS Code command-line launcher was not found")
        compiler = _compiler_status(self.compiler_path)
        if not compiler["available"] or compiler["name"] != "tectonic":
            raise MutationError("compiler_unavailable", "Tectonic is required for the ScholarFlow VS Code recipe")
        self.vscode_root.mkdir(parents=True, exist_ok=True)
        workspace_file = self.vscode_root / f"{record['manuscript_id']}.code-workspace"
        vscode_build = (self.build_root / str(record["manuscript_id"]) / "vscode").resolve()
        vscode_build.mkdir(parents=True, exist_ok=True)
        atomic_write_json(workspace_file, {
            "folders": [{"path": str(self._workspace(str(record["manuscript_id"]))) }],
            "settings": {
                "latex-workshop.latex.outDir": str(vscode_build),
                "latex-workshop.latex.tools": [{
                    "name": "scholarflow-tectonic",
                    "command": str(compiler["executable"]),
                    "args": ["--untrusted", "--synctex", "--keep-logs", "--outdir", "%OUTDIR%", "%DOC_EXT%"],
                    "env": {"TECTONIC_UNTRUSTED_MODE": "1"},
                }],
                "latex-workshop.latex.recipes": [{"name": "ScholarFlow Tectonic", "tools": ["scholarflow-tectonic"]}],
                "latex-workshop.latex.recipe.default": "first",
                "latex-workshop.latex.autoBuild.run": "never",
            },
        })
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        launcher = Path(code)
        if launcher.suffix.casefold() in {".cmd", ".bat"} and (launcher.parent.parent / "Code.exe").is_file():
            launcher = launcher.parent.parent / "Code.exe"
        subprocess.Popen([str(launcher), str(workspace_file)], cwd=self._workspace(str(record["manuscript_id"])), creationflags=creation_flags)
        return {"opened": True, "manuscript_id": record["manuscript_id"], "editor": "vscode", "compiler": compiler["name"]}

    def propose_create(
        self,
        manuscript_id: str,
        *,
        project_stage_id: str,
        title: str,
        target_journal: str = "generic",
        language: str = "en",
        main_file: str = "main.tex",
        sections: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_id = _manuscript_id(manuscript_id)
        stage_id = required_text(project_stage_id, "project_stage_id")
        if self.store.get_project_stage(stage_id) is None:
            raise MutationError("invalid_reference", f"project_stage '{stage_id}' was not found")
        if normalized_id in self._catalog()["items"]:
            raise MutationError("already_exists", f"manuscript '{normalized_id}' already exists")
        normalized_sections = _normalize_sections(sections or ["abstract", "introduction", "methods", "results", "discussion"])
        normalized_evidence = _evidence_refs(self.store, evidence_refs or [])
        changes = {
            "manuscript_id": normalized_id,
            "project_stage_id": stage_id,
            "title": required_text(title, "title"),
            "target_journal": required_text(target_journal, "target_journal"),
            "language": required_text(language, "language"),
            "main_file": _relative_tex_path(main_file),
            "sections": normalized_sections,
            "evidence_refs": normalized_evidence,
        }
        return self._store_proposal(
            operation="manuscript.create",
            target={"manuscript_id": normalized_id},
            changes=changes,
            base_fingerprint="sha256:absent",
            request_id=request_id,
            now=now,
        )

    def propose_patch(
        self,
        manuscript_id: str,
        *,
        section: str,
        replacement_latex: str,
        evidence_refs: list[str],
        base_file_hash: str,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        record = self.get(manuscript_id)
        normalized_section = _section_name(section)
        current = self.read_section(manuscript_id, normalized_section)
        if required_text(base_file_hash, "base_file_hash") != current["file_hash"]:
            raise MutationError("stale_base", "the manuscript file changed before proposal creation")
        replacement = _validate_latex_fragment(replacement_latex)
        if replacement.strip() == str(current["latex"]).strip():
            raise MutationError("no_changes", "the manuscript patch would not change the section")
        normalized_evidence = _evidence_refs(self.store, evidence_refs)
        if not normalized_evidence:
            raise MutationError("invalid_argument", "manuscript patches require at least one typed evidence reference")
        changes = {
            "section": normalized_section,
            "replacement_latex": replacement,
            "evidence_refs": normalized_evidence,
            "before_latex": current["latex"],
            "base_file_hash": current["file_hash"],
            "main_file": record["main_file"],
        }
        return self._store_proposal(
            operation="manuscript.patch",
            target={"manuscript_id": record["manuscript_id"], "section": normalized_section},
            changes=changes,
            base_fingerprint=current["file_hash"],
            request_id=request_id,
            now=now,
        )

    def apply(
        self,
        proposal_id: str,
        *,
        confirmation: Mapping[str, Any] | None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_id = required_text(proposal_id, "proposal_id")
        state = load_proposal_state(self.root_dir)
        committed = state["commits"].get(normalized_id)
        if committed is not None:
            response = dict(committed["response"])
            response["idempotent_replay"] = True
            return response
        raise_if_rejected(state, normalized_id)
        proposal = state["proposals"].get(normalized_id)
        if not isinstance(proposal, Mapping):
            raise MutationError("not_found", f"manuscript proposal '{normalized_id}' was not found")
        if proposal.get("operation") not in {"manuscript.create", "manuscript.patch"}:
            raise MutationError("invalid_proposal", "the proposal is not a manuscript mutation")
        effective_now = _now(now)
        if effective_now > datetime.fromisoformat(str(proposal["expires_at"])):
            raise MutationError("expired_proposal", "the manuscript proposal has expired")
        if not valid_confirmation(confirmation, normalized_id):
            raise MutationError("confirmation_required", "interactive user confirmation is required")

        operation = str(proposal["operation"])
        if operation == "manuscript.create":
            record, commit = self._apply_create(proposal, effective_now)
        else:
            record, commit = self._apply_patch(proposal, effective_now)
        event = append_event(
            self.root_dir,
            event_type="manuscript.mutation.committed",
            payload={
                "proposal_id": normalized_id,
                "operation": operation,
                "manuscript_id": record["manuscript_id"],
                "git_commit": commit,
                "pi_tool_call_id": request_id,
            },
        )
        response = {
            "proposal_id": normalized_id,
            "operation": operation,
            "record": record,
            "git_commit": commit,
            "audit_event_id": event["event_id"],
            "idempotent_replay": False,
        }
        state["commits"][normalized_id] = {
            "proposal_id": normalized_id,
            "committed_at": effective_now.isoformat(),
            "event_id": event["event_id"],
            "response": response,
        }
        save_proposal_state(self.root_dir, state)
        return response

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        normalized_id = required_text(proposal_id, "proposal_id")
        state = load_proposal_state(self.root_dir)
        proposal = state["proposals"].get(normalized_id)
        if not isinstance(proposal, Mapping) or proposal.get("operation") not in {"manuscript.create", "manuscript.patch"}:
            raise MutationError("not_found", f"manuscript proposal '{normalized_id}' was not found")
        result = dict(proposal)
        if normalized_id in state["commits"]:
            result["status"] = "applied"
        elif normalized_id in state["rejections"]:
            result["status"] = "rejected"
        return result

    def _apply_create(self, proposal: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], str]:
        changes = dict(proposal["normalized_changes"])
        manuscript_id = str(changes["manuscript_id"])
        if manuscript_id in self._catalog()["items"]:
            raise MutationError("stale_proposal", "the manuscript was created after this proposal")
        workspace = self._workspace(manuscript_id)
        if workspace.exists():
            raise MutationError("workspace_conflict", "the controlled manuscript workspace already exists")
        workspace.mkdir(parents=True)
        main_path = self._main_path(changes)
        main_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(workspace / ".scholarflow-manuscript.json", {
            "schema_version": MANUSCRIPT_SCHEMA_VERSION,
            "manuscript_id": manuscript_id,
            "main_file": changes["main_file"],
        })
        _atomic_write_text(main_path, _initial_document(changes))
        _run_git(workspace, "init", "--initial-branch=master")
        _run_git(workspace, "config", "user.name", "ScholarFlow")
        _run_git(workspace, "config", "user.email", "scholarflow@localhost")
        _run_git(workspace, "add", "--", ".scholarflow-manuscript.json", str(changes["main_file"]))
        _run_git(workspace, "commit", "-m", "Initialize governed manuscript")
        commit = _run_git(workspace, "rev-parse", "HEAD").strip()
        record = {
            **changes,
            "schema_version": MANUSCRIPT_SCHEMA_VERSION,
            "status": "draft",
            "workspace_ref": f"_manuscripts/workspaces/{manuscript_id}",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "current_file_hash": _file_hash(main_path),
            "local_commit": commit,
            "overleaf": {"configured": False},
        }
        self._save_catalog_record(record)
        return record, commit

    def _apply_patch(self, proposal: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], str]:
        manuscript_id = str(proposal["target"]["manuscript_id"])
        record = self.get(manuscript_id)
        changes = dict(proposal["normalized_changes"])
        main_path = self._main_path(record)
        current_hash = _file_hash(main_path)
        if current_hash != proposal["base_fingerprint"] or current_hash != changes["base_file_hash"]:
            raise MutationError("stale_proposal", "the manuscript changed after this patch was proposed")
        content = main_path.read_text(encoding="utf-8")
        updated = _replace_section(content, str(changes["section"]), str(changes["replacement_latex"]))
        _validate_document(updated)
        _atomic_write_text(main_path, updated)
        workspace = self._workspace(manuscript_id)
        _run_git(workspace, "add", "--", str(record["main_file"]))
        _run_git(workspace, "commit", "-m", f"Update {changes['section']} section")
        commit = _run_git(workspace, "rev-parse", "HEAD").strip()
        next_record = {
            **{key: value for key, value in record.items() if key not in {"git", "sections", "read_only"}},
            "evidence_refs": list(dict.fromkeys([*(record.get("evidence_refs") or []), *changes["evidence_refs"]])),
            "updated_at": now.isoformat(),
            "current_file_hash": _file_hash(main_path),
            "local_commit": commit,
            "latest_patch_proposal_id": proposal["proposal_id"],
        }
        self._save_catalog_record(next_record)
        return next_record, commit

    def _store_proposal(
        self,
        *,
        operation: str,
        target: Mapping[str, Any],
        changes: Mapping[str, Any],
        base_fingerprint: str,
        request_id: str | None,
        now: datetime | None,
    ) -> dict[str, Any]:
        created_at = _now(now)
        material = json.dumps(
            {"operation": operation, "target": target, "changes": changes, "base": base_fingerprint},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        proposal_id = "sha256:" + sha256(material.encode("utf-8")).hexdigest()
        state = load_proposal_state(self.root_dir)
        terminal = set(state["commits"]) | set(state["rejections"])
        for candidate in state["proposals"].values():
            if candidate.get("proposal_id") in terminal or candidate.get("proposal_id") == proposal_id:
                continue
            if candidate.get("operation") == operation and candidate.get("target") == dict(target):
                raise MutationError("id_reserved", "a pending manuscript proposal already targets this section")
        proposal = {
            "proposal_id": proposal_id,
            "operation": operation,
            "target": dict(target),
            "base_fingerprint": base_fingerprint,
            "normalized_changes": dict(changes),
            "diff": _proposal_diff(operation, changes),
            "policy": {"requires_confirmation": True, "reasons": ["local manuscript file mutation"]},
            "lane": "manuscript",
            "status": "pending",
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + PROPOSAL_TTL).isoformat(),
        }
        state["proposals"][proposal_id] = proposal
        save_proposal_state(self.root_dir, state)
        append_event(
            self.root_dir,
            event_type="manuscript.mutation.proposed",
            payload={"proposal_id": proposal_id, "operation": operation, **dict(target), "pi_tool_call_id": request_id},
        )
        return proposal

    def _catalog(self) -> dict[str, Any]:
        if not self.catalog_path.exists():
            return {"schema_version": MANUSCRIPT_SCHEMA_VERSION, "revision": 0, "items": {}}
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MutationError("manuscript_catalog_invalid", "the manuscript catalog is unreadable") from exc
        if payload.get("schema_version") != MANUSCRIPT_SCHEMA_VERSION or not isinstance(payload.get("items"), dict):
            raise MutationError("manuscript_catalog_invalid", "the manuscript catalog schema is unsupported")
        return payload

    def _save_catalog_record(self, record: Mapping[str, Any]) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.catalog_path):
            payload = self._catalog()
            payload["items"][record["manuscript_id"]] = dict(record)
            payload["revision"] = int(payload.get("revision", 0)) + 1
            atomic_write_json(self.catalog_path, payload)

    def _workspace(self, manuscript_id: str) -> Path:
        normalized = _manuscript_id(manuscript_id)
        root = self.workspace_root.resolve()
        target = (root / normalized).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise MutationError("scope_escape", "manuscript workspace escaped the controlled root") from exc
        return target

    def _build_dir(self, manuscript_id: str, source_hash: str) -> Path:
        digest = source_hash.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MutationError("invalid_argument", "source hash is invalid")
        root = self.build_root.resolve()
        target = (root / _manuscript_id(manuscript_id) / digest).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise MutationError("scope_escape", "compile directory escaped the controlled root") from exc
        return target

    def _main_path(self, record: Mapping[str, Any]) -> Path:
        workspace = self._workspace(str(record["manuscript_id"]))
        target = (workspace / _relative_tex_path(str(record["main_file"]))).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError as exc:
            raise MutationError("scope_escape", "main TeX file escaped the manuscript workspace") from exc
        return target

    @staticmethod
    def _git_status(workspace: Path) -> dict[str, Any]:
        commit = _run_git(workspace, "rev-parse", "HEAD").strip()
        dirty = bool(_run_git(workspace, "status", "--porcelain").strip())
        return {"commit": commit, "dirty": dirty, "branch": _run_git(workspace, "branch", "--show-current").strip()}


def _initial_document(changes: Mapping[str, Any]) -> str:
    lines = [
        r"\documentclass{article}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage{amsmath,amssymb}",
        f"\\title{{{_escape_latex(str(changes['title']))}}}",
        r"\date{}",
        r"\begin{document}",
        r"\maketitle",
        "",
    ]
    for section in changes["sections"]:
        label = str(section).replace("_", " ").title()
        lines.extend([
            f"\\section{{{_escape_latex(label)}}}",
            f"% sf-section:{section}:begin",
            f"% TODO: draft {section} from approved ScholarFlow evidence.",
            f"% sf-section:{section}:end",
            "",
        ])
    lines.extend([r"\end{document}", ""])
    return "\n".join(lines)


def _section_outline(content: str) -> list[dict[str, Any]]:
    lines = content.splitlines()
    begins: dict[str, int] = {}
    outline: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        begin = SECTION_BEGIN_RE.match(line)
        if begin:
            begins[begin.group(1)] = index
            continue
        end = SECTION_END_RE.match(line)
        if end and end.group(1) in begins:
            section = end.group(1)
            outline.append({"section": section, "line_start": begins.pop(section) + 1, "line_end": index - 1})
    if begins:
        raise MutationError("manuscript_structure_invalid", "one or more section end anchors are missing")
    return outline


def _extract_section(content: str, section: str) -> tuple[str, int, int]:
    normalized = _section_name(section)
    lines = content.splitlines()
    outline = {item["section"]: item for item in _section_outline(content)}
    target = outline.get(normalized)
    if target is None:
        raise MutationError("section_not_found", f"manuscript section '{normalized}' was not found")
    start = int(target["line_start"])
    end = int(target["line_end"])
    return "\n".join(lines[start - 1:end]), start, end


def _replace_section(content: str, section: str, replacement: str) -> str:
    normalized = _section_name(section)
    lines = content.splitlines()
    outline = {item["section"]: item for item in _section_outline(content)}
    target = outline.get(normalized)
    if target is None:
        raise MutationError("section_not_found", f"manuscript section '{normalized}' was not found")
    start = int(target["line_start"]) - 1
    end = int(target["line_end"])
    result = [*lines[:start], *replacement.splitlines(), *lines[end:]]
    return "\n".join(result) + "\n"


def _validate_latex_fragment(value: Any) -> str:
    fragment = required_text(value, "replacement_latex").replace("\r\n", "\n").replace("\r", "\n")
    if len(fragment.encode("utf-8")) > 500_000:
        raise MutationError("invalid_argument", "LaTeX patch exceeds 500 KB")
    if any(SECTION_BEGIN_RE.match(line) or SECTION_END_RE.match(line) for line in fragment.splitlines()):
        raise MutationError("invalid_latex", "a patch cannot create or replace section anchors")
    for pattern, reason in FORBIDDEN_LATEX:
        if pattern.search(fragment):
            raise MutationError("unsafe_latex", f"LaTeX patch contains {reason}")
    _balanced_braces(fragment)
    return fragment.strip()


def _validate_document(content: str) -> None:
    _section_outline(content)
    _balanced_braces(content)
    if content.count(r"\begin{document}") != 1 or content.count(r"\end{document}") != 1:
        raise MutationError("invalid_latex", "manuscript must contain one document environment")


def _balanced_braces(value: str) -> None:
    depth = 0
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise MutationError("invalid_latex", "LaTeX braces are unbalanced")
    if depth:
        raise MutationError("invalid_latex", "LaTeX braces are unbalanced")


def _run_git(workspace: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MutationError("git_unavailable", "local Git is unavailable for the manuscript workspace") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["unknown Git error"]
        raise MutationError("git_failed", f"local manuscript Git operation failed: {detail[0][:300]}")
    return result.stdout


def _proposal_diff(operation: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    if operation == "manuscript.create":
        return {key: {"before": None, "after": value} for key, value in changes.items()}
    return {
        "section": {"before": changes["before_latex"], "after": changes["replacement_latex"]},
        "evidence_refs": {"before": [], "after": changes["evidence_refs"]},
    }


def _evidence_refs(store: ScholarFlowStore, values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        ref = required_text(value, "evidence_ref")
        if ":" not in ref:
            raise MutationError("invalid_reference", f"evidence reference '{ref}' is not typed")
        kind, object_id = ref.split(":", 1)
        exists = (
            (kind == "paper" and store.get_paper(object_id) is not None)
            or (kind == "research_artifact" and store.get_research_artifact(object_id) is not None)
            or (kind == "experiment" and store.get_experiment(object_id) is not None)
        )
        if not exists:
            raise MutationError("invalid_reference", f"evidence reference '{ref}' was not found")
        normalized.append(ref)
    return list(dict.fromkeys(normalized))


def _relative_tex_path(value: str) -> str:
    normalized = required_text(value, "main_file").replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".tex":
        raise MutationError("invalid_argument", "main_file must be a relative .tex path inside the workspace")
    if any(part in {"", ".", ".git"} or part.startswith(".") for part in path.parts):
        raise MutationError("invalid_argument", "main_file contains a reserved path segment")
    return path.as_posix()


def _manuscript_id(value: str) -> str:
    normalized = required_text(value, "manuscript_id").casefold()
    if not MANUSCRIPT_ID_RE.fullmatch(normalized):
        raise MutationError("invalid_argument", "manuscript_id must match manuscript-[a-z0-9-]")
    return normalized


def _section_name(value: str) -> str:
    normalized = required_text(value, "section").casefold()
    if not SECTION_NAME_RE.fullmatch(normalized):
        raise MutationError("invalid_argument", "section must be a stable lowercase identifier")
    return normalized


def _normalize_sections(values: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(_section_name(value) for value in values))
    if not normalized or len(normalized) > 30:
        raise MutationError("invalid_argument", "a manuscript requires 1 to 30 sections")
    return normalized


def _file_hash(path: Path) -> str:
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MutationError("manuscript_file_unavailable", "the manuscript main file is unavailable") from exc


def _compiler_status(configured_path: Path | None = None) -> dict[str, Any]:
    configured = configured_path or (Path(os.environ["SCHOLARFLOW_TECTONIC"]) if str(os.environ.get("SCHOLARFLOW_TECTONIC") or "").strip() else None)
    if configured and configured.expanduser().is_file():
        executable = str(configured.expanduser().resolve())
        return {
            "available": True,
            "name": "tectonic",
            "version": _compiler_version(executable),
            "executable": executable,
            "command_profile": ["--untrusted", "--keep-logs", "--synctex"],
            "shell_escape": False,
        }
    for name, command in COMPILER_CANDIDATES:
        executable = shutil.which(name)
        if executable:
            return {
                "available": True,
                "name": name,
                "version": _compiler_version(executable),
                "executable": executable,
                "command_profile": list(command[1:]),
                "shell_escape": False,
            }
    return {
        "available": False,
        "name": None,
        "executable": None,
        "command_profile": [],
        "shell_escape": False,
        "reason": "No supported TeX compiler was found (latexmk, tectonic, or pdflatex).",
    }


def _compiler_version(executable: str) -> str | None:
    try:
        result = subprocess.run([executable, "--version"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = (result.stdout or result.stderr).strip().splitlines()
    return first[0][:120] if result.returncode == 0 and first else None


def _export_files(workspace: Path) -> list[tuple[PurePosixPath, Path]]:
    files: list[tuple[PurePosixPath, Path]] = []
    total_size = 0
    for path in sorted(workspace.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink():
            raise MutationError("manuscript_export_blocked", "symbolic links are not allowed in upload bundles")
        if not path.is_file():
            continue
        relative_path = path.relative_to(workspace)
        relative = PurePosixPath(*relative_path.parts)
        if ".git" in relative.parts or any(part.startswith(".scholarflow") for part in relative.parts):
            continue
        if path.suffix.casefold() not in EXPORT_EXTENSIONS:
            continue
        size = path.stat().st_size
        if size > 50_000_000:
            raise MutationError("manuscript_export_blocked", f"'{relative.as_posix()}' exceeds 50 MB")
        total_size += size
        if total_size > 150_000_000 or len(files) >= 2_000:
            raise MutationError("manuscript_export_blocked", "manuscript source exceeds bounded export limits")
        files.append((relative, path))
    if not files:
        raise MutationError("manuscript_export_blocked", "no supported manuscript files were found")
    return files


def _escape_latex(value: str) -> str:
    return "".join({"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}"}.get(char, char) for char in value)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
