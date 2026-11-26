from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest

from scholarflow.domain_queries import DomainQueryError
from scholarflow.reader_translation import ReaderTranslationService


class _ClosableProcess:
    def __init__(self, cancelled):
        self.cancelled = cancelled

    def close(self):
        self.cancelled.set()


class ReaderTranslationServiceTest(unittest.TestCase):
    def wait_status(self, service, task_id, status, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = service.get(task_id)
            if item["status"] == status:
                return item
            time.sleep(.01)
        self.fail(f"translation did not reach {status}: {service.get(task_id)['status']}")

    def test_result_is_persistent_bounded_and_never_exposes_source(self):
        with TemporaryDirectory() as directory:
            service = ReaderTranslationService(Path(directory), lambda source, register: {"translation": f"翻译:{source}"})
            task = service.create("selected paper excerpt")
            self.assertNotIn("source", task)
            replay = service.create("selected paper excerpt", task_id=task["task_id"])
            self.assertEqual(replay["task_id"], task["task_id"])
            with self.assertRaises(DomainQueryError):
                service.create("different excerpt", task_id=task["task_id"])
            completed = self.wait_status(service, task["task_id"], "completed")
            self.assertEqual(completed["translation"], "翻译:selected paper excerpt")
            again = ReaderTranslationService(Path(directory), lambda *_: self.fail("completed task reran"))
            self.assertEqual(again.get(task["task_id"]), completed)
            record_path = Path(directory) / "_reader_translations" / f"{task['task_id']}.json"
            saved = record_path.read_text(encoding="utf-8")
            self.assertIn('"source": ""', saved)
            self.assertIn('"source_sha256":', saved)

    def test_running_task_cancel_stops_its_process_and_discards_partial_result(self):
        with TemporaryDirectory() as directory:
            started = Event()
            def run(source, register):
                cancelled = Event()
                self.assertTrue(register(_ClosableProcess(cancelled)))
                started.set()
                cancelled.wait(3)
                return {"translation": "should not be retained"}
            service = ReaderTranslationService(Path(directory), run)
            task = service.create("private selected text")
            self.assertTrue(started.wait(2))
            self.assertEqual(service.cancel(task["task_id"])["status"], "cancelling")
            result = self.wait_status(service, task["task_id"], "cancelled")
            self.assertEqual(result["translation"], "")
            path = Path(directory) / "_reader_translations" / f"{task['task_id']}.json"
            self.assertNotIn("private selected text", path.read_text(encoding="utf-8"))

    def test_queue_is_bounded_and_interrupted_work_is_not_silently_replayed(self):
        with TemporaryDirectory() as directory:
            started = Event()
            release = Event()
            def runner(_source, register):
                process = _ClosableProcess(release)
                self.assertTrue(register(process))
                started.set()
                release.wait(3)
                return {"translation": "unused"}
            service = ReaderTranslationService(Path(directory), runner)
            first = service.create("one")
            self.assertTrue(started.wait(2))
            service.create("two")
            service.create("three")
            with self.assertRaises(DomainQueryError) as caught:
                service.create("over capacity")
            self.assertEqual(caught.exception.code, "reader_translation_queue_full")
            # Simulate a worker left running by an abrupt API restart.
            interrupted_id = first["task_id"]
            record = service._read(interrupted_id)
            record.update(status="running", source="", error_code="")
            service._write(record)
            reopened = ReaderTranslationService(Path(directory), lambda *_: self.fail("interrupted translation must not be retried automatically"))
            self.assertEqual(reopened.get(interrupted_id)["status"], "interrupted")
            release.set()
            service._worker.join(2)


if __name__ == "__main__":
    unittest.main()
