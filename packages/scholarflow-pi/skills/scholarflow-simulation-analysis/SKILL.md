---
name: scholarflow-simulation-analysis
description: Analyze simulation or computational-experiment evidence and connect conclusions to ScholarFlow experiments and projects. Use for FDTD, Lumerical, COMSOL, finite-element, numerical, parameter-sweep, convergence, failure, metric, log, checkpoint, or simulation-results analysis.
---

# ScholarFlow simulation analysis

1. First call `sf_lane_enter` with `lane: "analysis"`. This lane exposes
   experiment evidence and governed analysis artifacts without widening to
   unrelated workflows.
2. Call `sf_experiment_search`, then `sf_experiment_observe` only for logs,
   checkpoints, or result files exposed through approved roots. Treat the experiment search and
   observation payloads as the primary evidence. Use exact-object reads for
   the project, related tasks, papers, or prior artifacts only when a required
   field is missing; do not fetch every linked object by default.
3. Never claim a simulation ran or converged without an experiment record or inspectable output. Label inferred explanations as hypotheses.
4. Check configuration, baseline, parameter changes, convergence, numerical stability, metric definitions, anomalies, and reproducibility references.
5. Organize findings as evidence, interpretation, competing explanations, limitations, and the next discriminating experiment.
6. Propose experiment corrections or follow-ups separately with `sf_experiment_propose_log`.
7. Propose the durable analysis with `sf_artifact_propose_publish`, `kind: simulation_analysis`, and linked experiment/evidence ids. Apply only after confirmation.
8. Keep the normal analysis-and-follow-up path within seven tool calls. Once a
   discriminating `sf_experiment_propose_log` or analysis proposal exists,
   stop instead of widening the context.
