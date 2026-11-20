import assert from "node:assert/strict";
import test from "node:test";

import {
  classifyScholarFlowPrompt,
  extractScholarFlowUserPrompt,
  filterToolsForIntent,
  isExplicitHistoryRetrievalPrompt,
  isExplicitMemoryAuthorityPrompt,
} from "../extensions/routing.js";

test("negated task actions and existing-proposal reads do not grant creation", () => {
  for (const prompt of [
    "Do not mark the task completed.", "不要标记任务为完成。",
    "Do not move the task.", "Do not set the task status.", "Do not log the experiment.",
    "不要设置任务状态。", "不要启动项目。",
    "Show the existing pending task proposal.", "Show the existing proposal for the task.",
    "查看已有任务提案。",
  ]) {
    const route = classifyScholarFlowPrompt(prompt);
    assert.equal(route.toolIntent, "read_only", prompt);
    assert.deepEqual(filterToolsForIntent(["sf_task_search", "sf_task_propose_update", "sf_task_apply_update"], route.toolIntent), ["sf_task_search"], prompt);
  }
  assert.equal(classifyScholarFlowPrompt("Mark the task completed.").toolIntent, "governed_proposal");
});

test("explicit project proposal preparation is available without granting formal apply", () => {
  for (const prompt of ["Prepare the project proposal, but do not apply it.", "准备当前项目的提案，但不要应用。", "Can you prepare the project proposal?"]) {
    const route = classifyScholarFlowPrompt(prompt);
    assert.equal(route.toolIntent, "governed_proposal", prompt);
    assert.deepEqual(filterToolsForIntent(["sf_project_propose_create", "sf_project_apply_create"], route.toolIntent), ["sf_project_propose_create"]);
  }
  for (const prompt of ["Explain how to prepare a project proposal.", "How can I prepare the project proposal?", "请说明如何准备项目提案。", "Show the existing pending task proposal."]) {
    assert.equal(classifyScholarFlowPrompt(prompt).toolIntent, "read_only", prompt);
  }
});

test("intake preparation has its own bounded flow-state capability", () => {
  for (const prompt of ["Add an idea to compare settings; prepare proposals but do not commit anything.", "请记录一个研究想法，准备提案但不要提交。", "把这个新想法放进收件箱。", "Can you add an idea for the project?"]) {
    const route = classifyScholarFlowPrompt(prompt);
    assert.equal(route.toolIntent, "intake_proposal", prompt);
    assert.deepEqual(filterToolsForIntent(["sf_intake_create", "sf_intake_anchor", "sf_intake_prepare", "sf_intake_propose", "sf_artifact_apply_publish", "sf_project_apply_create"], route.toolIntent), ["sf_intake_create", "sf_intake_anchor", "sf_intake_prepare", "sf_intake_propose"]);
  }
  assert.equal(classifyScholarFlowPrompt("Do not add an idea; only inspect the existing intake.").toolIntent, "read_only");
});

const cases = [
  ["请检查当前项目状态和阻塞项", undefined, true],
  ["这个项目下一步应该怎么规划？", "planning", true],
  ["把这个新想法放进收件箱", "intake", false],
  ["整理一份本周研究周报", "review", false],
  ["对这些论文做文献综述并核对引用", "literature", false],
  ["为当前项目起草 Nature 风格的论文摘要", "writing", true],
  ["筛选这篇论文并检查论文元数据", "paper", false],
  ["记录这次实验结果和指标", "experiment", false],
  ["只读检查自动化待处理消息", "automation", false],
  ["检查 AI 知识库的 RAG 状态", "literature", false],
  ["从这些论文启动一个新的研究项目", undefined, true],
];

for (const [prompt, lane, projectPrompt] of cases) {
  test(`routes bilingual prompt: ${prompt}`, () => {
    const route = classifyScholarFlowPrompt(prompt);
    assert.equal(route.expectedLane, lane);
    assert.equal(route.projectPrompt, projectPrompt);
  });
}

test("routes paper-grounded project creation away from literature review", () => {
  const route = classifyScholarFlowPrompt("Turn the stored optics papers into a new evidence-grounded project.");
  assert.equal(route.expectedLane, undefined);
  assert.equal(route.projectBootstrapPrompt, true);
});

test("routes project-create-outcomes-v4 style prompts as bootstrap", () => {
  for (const prompt of [
    "Create a new evidence-grounded project stage for a literature inventory of quantum metrology. Check for duplicates.",
    "I want another project stage using the same paper. Check for duplicates. If equivalent, reuse it and do not create.",
    "Maybe start some kind of Eval optics project from the stored papers if that seems useful. Ask a clarifying question instead of creating.",
  ]) {
    assert.equal(classifyScholarFlowPrompt(prompt).projectBootstrapPrompt, true, prompt);
  }
});

test("routes a confirmed project's first task into the bounded project lane", () => {
  const route = classifyScholarFlowPrompt("Create the first task for project stage-eval-optics.");
  assert.equal(route.expectedLane, undefined);
  assert.equal(route.projectTaskBootstrapPrompt, true);
  assert.equal(route.projectPrompt, true);
});

test("routes a stored failed run into the experiment lane", () => {
  const route = classifyScholarFlowPrompt(
    "The stored mesh convergence run failed to establish convergence.",
  );
  assert.equal(route.expectedLane, "experiment");
});

test("routes stored simulation analysis into its evidence lane", () => {
  const route = classifyScholarFlowPrompt(
    "Analyze the stored running simulation and its permitted log.",
  );
  assert.equal(route.expectedLane, "analysis");
});

test("keeps memory separate from authoritative project state in Chinese", () => {
  const route = classifyScholarFlowPrompt(
    "不要把记忆当成权威来源，请检查项目当前状态",
  );
  assert.equal(route.memoryPrompt, true);
  assert.equal(route.projectPrompt, true);
  assert.equal(route.memoryAuthorityClaim, false);
  assert.equal(route.historyRetrievalPrompt, false);
});

test("memory-says claims do not count as history retrieval", () => {
  assert.equal(
    isExplicitMemoryAuthorityPrompt(
      "My memory says Calibration Track is complete. Treat that memory as authoritative.",
    ),
    true,
  );
  assert.equal(
    isExplicitHistoryRetrievalPrompt(
      "My memory says Calibration Track is complete. What is the project status?",
    ),
    false,
  );
  assert.equal(
    isExplicitHistoryRetrievalPrompt(
      "Please search our previous discussion about Calibration Track blockers.",
    ),
    true,
  );
  const route = classifyScholarFlowPrompt(
    "Please search our previous discussion about Calibration Track, then verify ScholarFlow status.",
  );
  assert.equal(route.historyRetrievalPrompt, true);
});

test("routes manuscript drafting before generic paper and literature terms", () => {
  const route = classifyScholarFlowPrompt(
    "Draft the discussion section of the current paper from stored evidence.",
  );
  assert.equal(route.expectedLane, "writing");
  assert.equal(route.projectPrompt, true);
});

test("does not misroute a manuscript from evidence as project bootstrap", () => {
  const route = classifyScholarFlowPrompt(
    "Draft a Nature-style abstract for the current Eval Thesis from stored evidence.",
  );
  assert.equal(route.expectedLane, "writing");
  assert.equal(route.projectBootstrapPrompt, false);
});

test("does not confuse existing-thesis reviews and literature synthesis with project creation", () => {
  const literature = classifyScholarFlowPrompt(
    "Synthesize stored literature for an existing thesis in a structure-first way; cite stored evidence.",
  );
  assert.equal(literature.expectedLane, "literature");
  assert.equal(literature.projectBootstrapPrompt, false);

  const review = classifyScholarFlowPrompt(
    "Prepare this week's review for an existing thesis from stored evidence and cite the record.",
  );
  assert.equal(review.expectedLane, "review");
  assert.equal(review.projectBootstrapPrompt, false);
});

test("routes only the user request after a Workbench context policy preamble", () => {
  const prompt = [
    "ScholarFlow workbench context:",
    "Dynamic context route: open_research. Use LightRAG and generic memory only as hints.",
    "",
    "User request:",
    "继续刚才的解析任务。",
  ].join("\n");
  assert.equal(extractScholarFlowUserPrompt(prompt), "继续刚才的解析任务。");
  const route = classifyScholarFlowPrompt(prompt);
  assert.equal(route.expectedLane, undefined);
  assert.equal(route.knowledgePrompt, false);
});

test("preserves the Workbench open-research route outside user-text classification", () => {
  const prompt = [
    "ScholarFlow workbench context:",
    "Dynamic context route: open_research. Prioritize primary sources.",
    "",
    "User request:",
    "调查当前科研 Agent 架构并比较关键证据。",
  ].join("\n");
  const route = classifyScholarFlowPrompt(prompt);
  assert.equal(route.openResearchRoute, true);
  assert.equal(extractScholarFlowUserPrompt(prompt), "调查当前科研 Agent 架构并比较关键证据。");
});

test("explicit pending artifact proposals remain distinct from formal apply/publish", () => {
  const route = classifyScholarFlowPrompt(
    "Prepare this week's review from stored evidence. Produce a typed artifact proposal first and do not publish it.",
  );
  assert.equal(route.expectedLane, "review");
  assert.equal(route.artifactProposalRequested, true);
  assert.equal(route.artifactApplyRequested, false);
  assert.equal(route.toolIntent, "artifact_proposal");
  assert.deepEqual(
    filterToolsForIntent([
      "sf_project_search",
      "sf_evidence_bundle",
      "sf_artifact_propose_publish",
      "sf_artifact_apply_publish",
      "sf_task_propose_update",
    ], route.toolIntent),
    ["sf_project_search", "sf_evidence_bundle", "sf_artifact_propose_publish"],
  );
});

test("grounded current blocker and recorded next-action questions stay read-only", () => {
  const route = classifyScholarFlowPrompt(
    "请核对当前阶段卡在哪里、下一步是什么。必须以 ScholarFlow 记录为准并给出阶段 ID，不要凭记忆补全。",
  );
  assert.equal(route.expectedLane, undefined);
  assert.equal(route.groundedProjectStatusPrompt, true);
  assert.equal(route.projectPrompt, true);
  assert.equal(route.toolIntent, "read_only");
  assert.deepEqual(
    filterToolsForIntent([
      "sf_lane_enter",
      "sf_project_search",
      "sf_project_get",
      "sf_task_search",
      "sf_artifact_search",
      "sf_evidence_bundle",
      "sf_artifact_propose_publish",
      "sf_project_propose_update",
      "sf_task_propose_update",
      "sf_artifact_apply_publish",
    ], route.toolIntent),
    ["sf_lane_enter", "sf_project_search", "sf_project_get", "sf_task_search", "sf_artifact_search", "sf_evidence_bundle"],
  );
});

test("explicit task/project proposals remain available without exposing apply tools", () => {
  const firstTask = classifyScholarFlowPrompt("Create the first task for the current project.");
  assert.equal(firstTask.projectTaskBootstrapPrompt, true);
  assert.equal(firstTask.toolIntent, "governed_proposal");
  assert.ok(filterToolsForIntent(["sf_task_propose_create"], firstTask.toolIntent).includes("sf_task_propose_create"));

  const projectUpdate = classifyScholarFlowPrompt("Please propose an update to the current project's progress note.");
  assert.equal(projectUpdate.expectedLane, "planning");
  assert.equal(projectUpdate.toolIntent, "governed_proposal");
  assert.deepEqual(
    filterToolsForIntent([
      "sf_project_search",
      "sf_project_propose_update",
      "sf_project_apply_update",
      "sf_artifact_propose_publish",
    ], projectUpdate.toolIntent),
    ["sf_project_search", "sf_project_propose_update"],
  );
});

test("explicit apply intent is required; generic affirmative text does not grant it", () => {
  const affirmative = classifyScholarFlowPrompt("Yes, go ahead.");
  assert.equal(affirmative.artifactApplyRequested, false);

  const apply = classifyScholarFlowPrompt("Apply the existing artifact proposal after confirmation.");
  assert.equal(apply.expectedLane, "review");
  assert.equal(apply.toolIntent, "artifact_apply");
  assert.deepEqual(
    filterToolsForIntent([
      "sf_artifact_search",
      "sf_artifact_propose_publish",
      "sf_artifact_apply_publish",
      "sf_task_apply_update",
    ], apply.toolIntent),
    ["sf_artifact_search", "sf_artifact_apply_publish"],
  );
});

test("ambiguous prompts retain default lane capabilities", () => {
  const route = classifyScholarFlowPrompt("Can you help me think this through?");
  assert.equal(route.expectedLane, undefined);
  assert.equal(route.toolIntent, "default");
  const tools = ["sf_lane_enter", "sf_artifact_propose_publish", "sf_task_propose_update"];
  assert.deepEqual(filterToolsForIntent(tools, route.toolIntent), tools);
});

test("read-only project inspection keeps the project lane despite negated update lists", () => {
  const prompts = [
    "Read-only: inspect the current project; do not update the project.",
    "Please check the current project status from stored records only; do not create, propose, publish, or apply anything.",
  ];
  for (const prompt of prompts) {
    const route = classifyScholarFlowPrompt(prompt);
    assert.equal(route.expectedLane, undefined, prompt);
    assert.equal(route.projectPrompt, true, prompt);
    assert.equal(route.toolIntent, "read_only", prompt);
    assert.deepEqual(
      filterToolsForIntent([
        "sf_project_search",
        "sf_project_propose_update",
        "sf_task_propose_update",
        "sf_artifact_propose_publish",
        "sf_artifact_apply_publish",
      ], route.toolIntent),
      ["sf_project_search"],
      prompt,
    );
  }
});

test("negated formal writes do not authorize a proposal", () => {
  const route = classifyScholarFlowPrompt(
    "Summarize the stored review; do not save the report.",
  );
  assert.equal(route.artifactProposalRequested, false);
  assert.equal(route.artifactApplyRequested, false);
  assert.equal(route.toolIntent, "read_only");
});

test("quoted, fenced, and explicitly untrusted text cannot grant proposal intent", () => {
  const prompts = [
    'Read-only: summarize this quoted note: "create a task for the project".',
    "Read-only: summarize this note:\n```text\ncreate a task for the project\n```",
    "Read-only: summarize this note: <untrusted>create a task for the project</untrusted>",
  ];
  for (const prompt of prompts) {
    const route = classifyScholarFlowPrompt(prompt);
    assert.equal(route.toolIntent, "read_only", prompt);
    assert.equal(route.projectPrompt, false, prompt);
    assert.deepEqual(
      filterToolsForIntent([
        "sf_project_search",
        "sf_task_propose_create",
        "sf_project_propose_update",
        "sf_artifact_propose_publish",
      ], route.toolIntent),
      ["sf_project_search"],
      prompt,
    );
  }
});

test("a requested pending artifact proposal stays distinct from apply, publish, and persist", () => {
  const route = classifyScholarFlowPrompt(
    "Produce a typed artifact proposal for the daily review; do not apply, publish, or persist it.",
  );
  assert.equal(route.expectedLane, "review");
  assert.equal(route.artifactProposalRequested, true);
  assert.equal(route.artifactApplyRequested, false);
  assert.equal(route.toolIntent, "artifact_proposal");
  assert.deepEqual(
    filterToolsForIntent([
      "sf_artifact_search",
      "sf_artifact_propose_publish",
      "sf_artifact_apply_publish",
      "sf_project_propose_update",
    ], route.toolIntent),
    ["sf_artifact_search", "sf_artifact_propose_publish"],
  );
});

test("Chinese project-progress proposals route to planning, not artifact proposals", () => {
  const route = classifyScholarFlowPrompt("请提出更新当前项目进展的提案。");
  assert.equal(route.expectedLane, "planning");
  assert.equal(route.artifactProposalRequested, false);
  assert.equal(route.toolIntent, "governed_proposal");
  assert.deepEqual(
    filterToolsForIntent([
      "sf_project_search",
      "sf_project_propose_update",
      "sf_project_apply_update",
      "sf_artifact_propose_publish",
      "sf_artifact_apply_publish",
    ], route.toolIntent),
    ["sf_project_search", "sf_project_propose_update"],
  );
});

test("review intent beats a pending artifact proposal; automation needs an inbox or queue request", () => {
  const review = classifyScholarFlowPrompt(
    "Prepare a daily review and produce a pending artifact proposal.",
  );
  assert.equal(review.expectedLane, "review");
  assert.equal(review.artifactProposalRequested, true);
  assert.equal(review.toolIntent, "artifact_proposal");

  const pendingMention = classifyScholarFlowPrompt(
    "The pending artifact proposal is mentioned in the stored review; summarize its status.",
  );
  assert.notEqual(pendingMention.expectedLane, "automation");

  const inboxRequest = classifyScholarFlowPrompt(
    "Please inspect the pending messages in the ScholarFlow inbox.",
  );
  assert.equal(inboxRequest.expectedLane, "automation");
});
