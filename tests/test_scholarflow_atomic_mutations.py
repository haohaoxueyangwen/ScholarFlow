from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scholarflow import ScholarFlowStore, ProjectStage, Task
from scholarflow.event_bus import list_events
from scholarflow.mutations import MutationError, TaskMutationService
from scholarflow.proposal_state import load_proposal_state, reject_proposal
from scholarflow.research_mutations import ResearchMutationService


class AtomicMutationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ScholarFlowStore(self.root)
        self.store.create_project_stage(ProjectStage(stage_id="stage-a", project_name="A", title="A", goal="A"))
        self.store.create_task(Task(task_id="task-a", title="A"))
        self.service = ResearchMutationService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def confirmation(self, proposal):
        return {"proposal_id": proposal["proposal_id"], "confirmed": True, "confirmed_by": "interactive_user", "confirmed_at": datetime.now(timezone.utc).isoformat(), "pi_tool_call_id": "fixture"}

    def apply(self, proposal):
        return self.service.apply(proposal["proposal_id"], confirmation=self.confirmation(proposal))

    def test_concurrent_updates_have_one_winner(self):
        proposals = [self.service.propose_project_update("stage-a", {"progress_note": value}) for value in ("A", "B")]
        def run(proposal):
            try:
                return self.apply(proposal)["operation"]
            except MutationError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, proposals))
        self.assertCountEqual(results, ["project_stage.update", "stale_proposal"])

    def test_ordinary_writer_between_check_and_save_is_rejected(self):
        proposal = self.service.propose_project_update("stage-a", {"progress_note": "proposal"})
        original = self.store.apply_domain_proposal
        def concurrent_write(value):
            self.store.update_project_stage("stage-a", progress_note="ordinary writer")
            return original(value)
        with patch.object(self.store, "apply_domain_proposal", side_effect=concurrent_write):
            with self.assertRaises(MutationError) as caught:
                self.apply(proposal)
        self.assertEqual(caught.exception.code, "stale_proposal")
        self.assertEqual(self.store.get_project_stage("stage-a").progress_note, "ordinary writer")

    def test_projection_failure_recovers_without_rewriting_domain(self):
        proposal = self.service.propose_project_update("stage-a", {"progress_note": "approved"})
        with patch.object(self.service, "_commit", side_effect=OSError("projection failure")):
            with self.assertRaises(OSError):
                self.apply(proposal)
        self.assertEqual(self.store.get_project_stage("stage-a").progress_note, "approved")
        self.assertNotIn(proposal["proposal_id"], load_proposal_state(self.root)["commits"])
        # A later independent edit must not be undone by recovery.
        self.store.update_project_stage("stage-a", progress_note="newer")
        reopened = ResearchMutationService(ScholarFlowStore(self.root))
        receipt = reopened.apply(proposal["proposal_id"], confirmation=None)
        self.assertTrue(receipt["idempotent_replay"])
        self.assertEqual(receipt["record"]["progress_note"], "approved")
        self.assertEqual(self.store.get_project_stage("stage-a").progress_note, "newer")

    def test_domain_save_failure_does_not_leave_success_receipt(self):
        proposal = self.service.propose_project_update("stage-a", {"progress_note": "approved"})
        with patch.object(self.store.project_stages, "_save", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.apply(proposal)
        self.assertIsNone(self.store.get_project_stage("stage-a").progress_note)
        self.assertIsNone(self.store.domain_proposal_receipt(proposal))

    def test_rejection_cannot_reverse_durable_commit_with_missing_projection(self):
        proposal = self.service.propose_project_update("stage-a", {"progress_note": "approved"})
        with patch.object(self.service, "_commit", side_effect=OSError("projection failure")):
            with self.assertRaises(OSError):
                self.apply(proposal)
        with self.assertRaisesRegex(ValueError, "proposal_already_committed"):
            reject_proposal(self.root, proposal["proposal_id"], rejected_by="interactive_user", rejected_at="now", reason="late", request_id="fixture")

    def test_task_receipt_projection_is_recoverable(self):
        service = TaskMutationService(self.store)
        proposal = service.propose_update("task-a", {"title": "approved"})
        with patch("scholarflow.mutations.publish_domain_receipt", side_effect=OSError("projection failure")):
            with self.assertRaises(OSError):
                service.apply_update(proposal["proposal_id"], confirmation=self.confirmation(proposal))
        result = service.apply_update(proposal["proposal_id"], confirmation=None)
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(self.store.get_task("task-a").title, "approved")
        service.apply_update(proposal["proposal_id"], confirmation=None)
        commits = list_events(self.root, event_type="pi.domain.mutation.committed")["items"]
        self.assertEqual(len(commits), 1)


if __name__ == "__main__":
    unittest.main()
