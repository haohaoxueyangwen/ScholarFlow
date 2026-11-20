"""Create deterministic, disposable ScholarFlow records for Pi behavior evaluation."""

from pathlib import Path
import argparse
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scholarflow import Experiment, Paper, ProjectStage, ScholarFlowStore, Task
from scholarflow.enums import ExperimentStatus, PaperStatus, ProjectStageStatus, TaskStatus


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "exp-convergence.log").write_text(
        "step=100 residual=0.12\nstep=200 residual=0.11\nstep=300 residual=0.11\n",
        encoding="utf-8",
    )
    store = ScholarFlowStore(Path(args.data_dir))
    store.create_project_stage(ProjectStage(
        stage_id="stage-eval-optics",
        project_name="Eval Thesis",
        title="Validate optical simulation",
        goal="Determine whether the proposed method improves the baseline.",
        status=ProjectStageStatus.ACTIVE,
        progress_note="Baseline reproduced; convergence remains unresolved.",
        related_tasks=["task-eval-convergence"],
        related_experiments=["exp-eval-convergence"],
        risks=["Residual plateau may invalidate the comparison."],
        next_actions=["Diagnose mesh and boundary sensitivity."],
    ))
    store.create_task(Task(
        task_id="task-eval-convergence",
        title="Diagnose residual plateau",
        project_stage_id="stage-eval-optics",
        priority="high",
        status=TaskStatus.IN_PROGRESS,
        due_hint="2026-07-15",
    ))
    store.create_experiment(Experiment(
        experiment_id="exp-eval-convergence",
        title="Mesh convergence sweep",
        goal="Test whether the metric is mesh independent.",
        project_stage_id="stage-eval-optics",
        related_tasks=["task-eval-convergence"],
        variables={"mesh_nm": [20, 10, 5]},
        metrics={"residual": 0.11},
        log_path=str(run_dir / "exp-convergence.log"),
        status=ExperimentStatus.RUNNING,
        result_summary="Residual plateaued; convergence is not established.",
        next_actions=["Compare absorbing-boundary settings."],
    ))
    store.create_paper(Paper(
        paper_id="paper-eval-baseline",
        title="A baseline optical simulation method",
        authors=["Evaluation Author"],
        year=2024,
        source="doi:10.0000/scholarflow.eval",
        status=PaperStatus.DONE,
        summary="Reports a baseline method under a different boundary condition.",
        relevance="Useful comparator, but not direct evidence of current-run convergence.",
        related_tasks=["task-eval-convergence"],
        related_experiments=["exp-eval-convergence"],
    ))
    store.update_task(
        "task-eval-convergence",
        related_experiments=["exp-eval-convergence"],
        related_papers=["paper-eval-baseline"],
    )
    store.create_paper(Paper(
        paper_id="paper-eval-metrology",
        title="A quantum metrology survey for laboratory calibration",
        authors=["Evaluation Author"],
        year=2023,
        source="doi:10.0000/scholarflow.eval.metrology",
        status=PaperStatus.DONE,
        summary="Surveys metrology protocols that are out of scope for optical simulation validation.",
        relevance="Supports a distinct literature-inventory stage about metrology, not optical convergence.",
        related_tasks=[],
        related_experiments=[],
    ))
    store.create_project_stage(ProjectStage(
        stage_id="stage-eval-calibration",
        project_name="Calibration Track",
        title="Assemble calibration evidence",
        goal="Establish a traceable calibration dataset before model comparison.",
        status=ProjectStageStatus.BLOCKED,
        progress_note="Instrument protocol is approved, but the calibration dataset is missing.",
        related_tasks=["task-eval-calibration-data"],
        risks=["Without the dataset, model comparison would not be evidence-grounded."],
        next_actions=["Collect and checksum the first calibration dataset."],
    ))
    store.create_task(Task(
        task_id="task-eval-calibration-data",
        title="Collect calibration dataset",
        project_stage_id="stage-eval-calibration",
        priority="high",
        status=TaskStatus.BLOCKED,
        due_hint="2026-07-18",
    ))


if __name__ == "__main__":
    main()
