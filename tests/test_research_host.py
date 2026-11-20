import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

import test_research_workspace as fixtures
from scholarflow import research_workspace as rw
from scholarflow.research_http import ResearchHTTPServer, open_research_host
from scholarflow.web_research import WebResearchService


class ResearchHostTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkspaceTests()
        self.fixture.setUp()
        self.data = self.fixture.data
        self.home = self.fixture.root / "shared-workspaces"
        self.service = WebResearchService(self.data, home=self.home)

    def tearDown(self):
        self.fixture.tearDown()

    def test_cli_and_service_share_one_project_directory(self):
        preview = rw.preview_project(self.data, project_name="命令行项目", title="阅读", goal="只记授权页。", home=self.home)
        self.assertIsNone(next((item for item in self.service.projects()["items"] if item["stage_id"] == preview["stage_id"]), None))
        created = rw.commit_project(self.data, preview["request_id"], preview["proposal_hash"], home=self.home)
        listed = self.service.projects()["items"]
        self.assertEqual(listed[-1]["stage_id"], created["stage_id"])
        self.assertEqual(listed[-1]["goal"], "只记授权页。")
        again = self.service.preview_project({"project_name": "接口项目", "title": "阅读", "goal": "接口创建的目标。"})
        committed = self.service.commit_project({"request_id": again["request_id"], "reviewed_hash": again["proposal_hash"]})
        names = {item["project_name"] for item in rw.projects(self.data)}
        self.assertEqual(names, {"Same name", "命令行项目", "接口项目"})
        self.assertIn(committed["stage_id"], {item["stage_id"] for item in rw.projects(self.data)})

    def test_wrong_hash_and_other_home_do_not_publish(self):
        preview = self.service.preview_project({"project_name": "未确认", "title": "阅读", "goal": "还不能写入。"})
        with self.assertRaisesRegex(Exception, "hash"):
            self.service.commit_project({"request_id": preview["request_id"], "reviewed_hash": "0" * 64})
        self.assertNotIn(preview["stage_id"], {item["stage_id"] for item in rw.projects(self.data)})
        other = WebResearchService(self.data, home=self.fixture.root / "other-home")
        self.assertFalse(any(item["project_name"] == "未确认" for item in other.projects()["items"]))

    def test_pdf_workspace_is_visible_to_the_same_home(self):
        preview = rw.preview_project(self.data, project_name="阅读项目", title="第一章", goal="阅读合成页。", home=self.home)
        created = rw.commit_project(self.data, preview["request_id"], preview["proposal_hash"], home=self.home)
        opened = self.service.open_pdf({"stage_id": created["stage_id"], "pdf_path": str(self.fixture.pdf), "pages": "1", "provider": "scholarflow-local"})
        self.assertEqual(opened["pdf"]["page_ranges"], [[1, 1]])
        self.assertEqual(opened["knowledge"]["status"], "not_queued")
        saved = self.service.list(created["stage_id"])["items"][0]
        self.assertEqual(saved["workspace_id"], opened["workspace_id"])
        self.assertEqual(saved["filename"], opened["pdf"]["filename"])
        cli_path = self.home / opened["workspace_id"] / "workspace.json"
        self.assertEqual(rw.load(cli_path)["stage_id"], created["stage_id"])
        self.assertEqual(WebResearchService(self.data, home=self.fixture.root / "other-home").list(created["stage_id"])["items"], [])

    def test_restart_marks_unmanaged_turn_and_index_interrupted(self):
        preview = rw.preview_project(self.data, project_name="恢复项目", title="阅读", goal="重启后不能假装仍在运行。", home=self.home)
        created = rw.commit_project(self.data, preview["request_id"], preview["proposal_hash"], home=self.home)
        opened = self.service.open_pdf({"stage_id": created["stage_id"], "pdf_path": str(self.fixture.pdf), "pages": "1", "provider": "scholarflow-local"})
        workspace = self.home / opened["workspace_id"]
        turn_id = "turn-" + "c" * 32
        turn_dir = workspace / "web-turns"
        turn_dir.mkdir()
        (turn_dir / f"{turn_id}.json").write_text(
            json.dumps({"turn_id": turn_id, "workspace_id": opened["workspace_id"], "stage_id": created["stage_id"], "message": "仍在跑", "mode": "chat", "status": "running"}),
            encoding="utf-8",
        )
        from scholarflow.knowledge_jobs import SyncJobStore
        path = (workspace / "workspace.json").absolute()
        SyncJobStore(self.data).save({
            "schema_version": 1, "plan_id": "ksp_" + "a" * 24, "source": "reviewed-workspace-note",
            "created_at": 1, "status": "running", "attempts": 1, "workspace": str(path), "stage_id": created["stage_id"],
            "artifact_id": "artifact-missing", "fingerprint": "sha256:none", "results": [],
        })
        fresh = WebResearchService(self.data, home=self.home)
        view = fresh.get(opened["workspace_id"], created["stage_id"])
        self.assertEqual(view["turns"][0]["status"], "interrupted")
        self.assertEqual(view["knowledge"]["status"], "interrupted")
        self.assertIn("error_code", view["knowledge"])

    def test_http_preview_commit_uses_the_service_directory(self):
        server = ResearchHTTPServer(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            self.addCleanup(connection.close)
            origin = "http://127.0.0.1:3000"
            connection.request("GET", "/session", headers={"Origin": origin})
            session = connection.getresponse()
            self.assertEqual(session.status, 200)
            cookie = session.getheader("Set-Cookie").split(";", 1)[0]
            session.read()
            common = {"Origin": origin, "Cookie": cookie, "X-ScholarFlow-Intent": "confirm"}
            connection.request("GET", "/projects", headers=common)
            before = json.loads(connection.getresponse().read())["items"]
            self.assertFalse(any(item["project_name"] == "HTTP项目" for item in before))
            body = json.dumps({"project_name": "HTTP项目", "title": "阅读", "goal": "通过本机接口创建。"}, ensure_ascii=False).encode("utf-8")
            connection.request("POST", "/projects/preview", body=body, headers={**common, "Content-Type": "application/json"})
            preview = json.loads(connection.getresponse().read())
            self.assertFalse(preview["created"])
            bad = json.dumps({"request_id": preview["request_id"], "reviewed_hash": "0" * 64}).encode("utf-8")
            connection.request("POST", "/projects/commit", body=bad, headers={**common, "Content-Type": "application/json"})
            denied = connection.getresponse()
            denied.read()
            self.assertEqual(denied.status, 400)
            good = json.dumps({"request_id": preview["request_id"], "reviewed_hash": preview["proposal_hash"]}).encode("utf-8")
            connection.request("POST", "/projects/commit", body=good, headers={**common, "Content-Type": "application/json"})
            created = json.loads(connection.getresponse().read())
            self.assertTrue(created["created"])
            names = {item["project_name"] for item in rw.projects(self.data)}
            self.assertIn("HTTP项目", names)
            connection.request(
                "POST",
                "/workspaces/upload",
                body=self.fixture.pdf.read_bytes(),
                headers={**common, "X-Stage-Id": created["stage_id"], "X-Pages": "1", "X-Filename": "sample.pdf", "X-Provider": "scholarflow-local", "Content-Type": "application/pdf"},
            )
            opened = json.loads(connection.getresponse().read())
            self.assertEqual(opened["pdf"]["page_ranges"], [[1, 1]])
            connection.request("GET", f"/imports/{opened['pdf']['import_id']}/pdf", headers={"Cookie": cookie})
            pdf = connection.getresponse()
            payload = pdf.read()
            self.assertEqual(pdf.status, 200)
            self.assertTrue(payload.startswith(b"%PDF"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_default_host_uses_cli_directories_when_present(self):
        if not Path(rw.DEFAULT_DATA).is_dir():
            self.skipTest("local research library is not present")
        host = open_research_host()
        self.assertEqual(host.data, Path(rw.DEFAULT_DATA).resolve())
        self.assertEqual(host.home, Path(rw.HOME).resolve())


if __name__ == "__main__":
    unittest.main()
