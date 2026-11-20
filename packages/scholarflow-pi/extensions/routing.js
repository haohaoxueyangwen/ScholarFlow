/**
 * @typedef {"project"|"paper"|"intake"|"literature"|"writing"|"experiment"|"analysis"|"planning"|"review"|"automation"|"all"} ScholarFlowLane
 */

/**
 * Classify only broad workflow routing hints. The selected lane still
 * controls the actual tool surface and all tools enforce their own contracts.
 *
 * @param {string} rawPrompt
 * @returns {{expectedLane?: ScholarFlowLane, memoryPrompt: boolean, memoryAuthorityClaim: boolean, historyRetrievalPrompt: boolean, projectPrompt: boolean, projectBootstrapPrompt: boolean, projectTaskBootstrapPrompt: boolean, knowledgePrompt: boolean, openResearchRoute: boolean, artifactProposalRequested: boolean, artifactApplyRequested: boolean, groundedProjectStatusPrompt: boolean, toolIntent: "default"|"read_only"|"artifact_proposal"|"artifact_apply"|"manuscript_proposal"|"governed_proposal"|"intake_proposal"}}
 */
export function classifyScholarFlowPrompt(rawPrompt) {
  const userText = stripUntrustedIntentContent(extractScholarFlowUserPrompt(rawPrompt)).toLowerCase();
  const prompt = removeNegatedActionClauses(userText);
  const openResearchRoute = /(?:^|\n)dynamic context route:\s*open_research\b/i.test(String(rawPrompt ?? ""));
  /** @type {ScholarFlowLane | undefined} */
  let expectedLane;
  const writingPrompt =
    /\b(draft|write|rewrite|structure)\b.*\b(manuscript|paper|abstract|introduction|methods?|results?|discussion|conclusion|title)\b|\b(manuscript|paper)\b.*\b(section|draft|writing)\b/.test(prompt) ||
    /(起草|撰写|写作|重写|改写|搭建).*(论文|手稿|摘要|引言|方法|结果|讨论|结论|标题)|(论文|手稿).*(章节|初稿|写作)/.test(prompt);
  const projectBootstrapPrompt = !writingPrompt && (
    /\b(turn|start|create|build|bootstrap)\b[^.!?\n]{0,120}\b(papers?|literature|evidence)\b[^.!?\n]{0,80}\b(into|as|to)\b[^.!?\n]{0,40}\b(?:new\s+)?(?:[a-z-]+\s+){0,3}(project|thesis)\b/.test(prompt) ||
    /\b(?:create|start|build|bootstrap)\b[^.!?\n]{0,45}\b(?:new\s+)?(?:[a-z-]+\s+){0,2}(project|thesis)\b[^.!?\n]{0,60}\b(from|using|based on)\b[^.!?\n]{0,60}\b(papers?|literature|evidence)\b/.test(prompt) ||
    /\b(?:create|start|build)\b[^.!?\n]{0,100}\b(?:new\s+)?(?:evidence-grounded\s+)?project(?:\s+stage)?\b/.test(prompt) &&
      /\b(papers?|literature|evidence|duplicates?|metrology|inventory)\b/.test(prompt) ||
    /\b(?:another|equivalent|reuse)\b[^.!?\n]{0,80}\bproject(?:\s+stage)?\b/.test(prompt) &&
      /\b(duplicates?|papers?|literature|evidence)\b/.test(prompt) ||
    /\b(?:maybe start|kind of)\b[^.!?\n]{0,80}\bproject\b/.test(prompt) &&
      /\b(papers?|stored|exists|clarifying)\b/.test(prompt) ||
    /(?:从|基于).{0,50}(?:论文|文献|证据).{0,50}(?:启动|创建|建立|生成).{0,25}(?:新)?(?:项目|课题)|(?:新建|创建|启动).{0,20}(?:项目|课题).{0,40}(?:论文|文献|证据)|把.{0,40}(?:论文|文献).{0,30}(?:变成|转成).{0,20}(?:项目|课题)/.test(prompt)
  );
  const projectTaskBootstrapPrompt =
    /\b(first|initial)\b.*\b(task|deliverable)\b.*\b(project|stage|thesis)\b|\b(project|stage|thesis)\b.*\b(first|initial)\b.*\b(task|deliverable)\b/.test(prompt) ||
    /(项目|课题|阶段).*(首个|第一个|第一项).*(任务|交付)|为.*(项目|课题|阶段).*(创建|建立|生成).*(首个|第一个|第一项).*(任务|交付)/.test(prompt);
  const artifactProposalIntent = explicitlyRequestsArtifactProposal(prompt);
  const manuscriptPatchIntent = explicitlyRequestsManuscriptPatch(prompt);
  const otherProposalIntent = !artifactProposalIntent && explicitlyRequestsOtherGovernedProposal(prompt);
  const intakeProposalIntent = !writingPrompt && isIntakePreparationRequest(prompt);
  const explicitProposalIntent = artifactProposalIntent || manuscriptPatchIntent || otherProposalIntent || intakeProposalIntent;
  const explicitReadOnly = isExplicitlyReadOnlyRequest(userText, explicitProposalIntent);
  const artifactProposalRequested = !explicitReadOnly && artifactProposalIntent;
  const artifactApplyRequested = !explicitReadOnly && explicitlyRequestsArtifactApply(prompt);
  if (writingPrompt) {
    expectedLane = "writing";
  } else if (projectBootstrapPrompt || projectTaskBootstrapPrompt) {
    // The bounded project lane handles this dedicated creation workflow.
  } else if (
    /\b(weekly review|daily review|weekly report|daily report|this week(?:'s|s) review|today(?:'s|s) review)\b/.test(prompt) ||
    /(周报|日报|每周回顾|每日回顾|本周复盘|今日复盘|研究复盘)/.test(prompt)
  ) {
    expectedLane = "review";
  } else if (isAutomationQueueRequest(prompt)) {
    expectedLane = "automation";
  } else if (
    /\b(capture|research idea|new idea|add an idea|new request|align it to existing state)\b/.test(prompt) ||
    /(收集|记录想法|研究想法|新想法|新需求|归入项目|放进收件箱)/.test(prompt)
  ) {
    expectedLane = "intake";
  } else if (/(论文筛选|文献筛选|论文元数据|录入论文|论文录入)/.test(prompt)) {
    expectedLane = "paper";
  } else if (
    /\b(literature|literature review|related work|papers|citations|citation|synthesis|knowledge base|rag)\b/.test(prompt) ||
    /(文献综述|相关工作|论文综述|引用|引文|综合文献|知识库|检索增强)/.test(prompt)
  ) {
    expectedLane = "literature";
  } else if (/\bintake\b/.test(prompt)) {
    expectedLane = "intake";
  } else if (
    /\b(?:analy[sz]e|analysis)\b.*\b(?:simulation|run|convergence|log)\b/.test(prompt) ||
    /\b(?:simulation results?|convergence analysis)\b/.test(prompt)
  ) {
    expectedLane = "analysis";
  } else if (
    /\b(experiment|run log|metrics)\b/.test(prompt) ||
    /\b(?:stored\s+)?[\w -]*run\s+(?:failed|completed|blocked|running)\b/.test(prompt) ||
    /(实验|仿真|运行日志|实验指标|实验结果)/.test(prompt)
  ) {
    expectedLane = "experiment";
  } else if (
    /\b(plan|planning|next steps|roadmap)\b/.test(prompt) ||
    /(计划|规划|下一步|路线图|后续安排)/.test(prompt)
  ) {
    expectedLane = "planning";
  } else if (
    /\b(triage|paper metadata|paper intake)\b/.test(prompt) ||
    /(筛选这篇论文|论文条目)/.test(prompt)
  ) {
    expectedLane = "paper";
  }

  // A grounded question about the currently recorded blocker/next action is
  // a project-state read, even when the phrase "next step" resembles a plan
  // request. Only suppress the planning route when there is no explicit
  // proposal/apply request and no request to create a new plan.
  const manuscriptPatchRequested = !explicitReadOnly && manuscriptPatchIntent;
  const otherProposalRequested = !explicitReadOnly && otherProposalIntent;
  const intakeProposalRequested = !explicitReadOnly && intakeProposalIntent;
  if (
    otherProposalRequested && !projectBootstrapPrompt && !projectTaskBootstrapPrompt && !expectedLane &&
    /\b(?:task|project|stage)\b|(?:任务|项目|阶段)/.test(prompt)
  ) {
    expectedLane = "planning";
  }
  const groundedProjectStatusPrompt = isGroundedProjectStatusQuestion(prompt) &&
    !artifactProposalRequested && !artifactApplyRequested && !manuscriptPatchRequested && !otherProposalRequested &&
    !/\b(?:make|create|draft|design|develop|prepare)\b[\s\S]{0,40}\b(?:plan|roadmap)\b/.test(prompt) &&
    !/(?:制定|编制|设计|撰写|生成).{0,12}(?:计划|规划|方案)/.test(prompt);
  if (groundedProjectStatusPrompt && expectedLane === "planning") expectedLane = undefined;
  if (artifactApplyRequested && !expectedLane) expectedLane = "review";

  const memoryPrompt =
    /\b(memory|authoritative|skip scholarflow tools)\b/.test(prompt) ||
    /(记忆|权威来源|跳过\s*scholarflow\s*工具)/.test(prompt);
  const memoryAuthorityClaim = isExplicitMemoryAuthorityPrompt(prompt);
  const historyRetrievalPrompt = isExplicitHistoryRetrievalPrompt(prompt);
  const projectPrompt =
    memoryPrompt || historyRetrievalPrompt || projectBootstrapPrompt || projectTaskBootstrapPrompt || groundedProjectStatusPrompt || expectedLane === "writing" ||
    /\b(eval thesis|project|stage|blocker|next action|current status|stored ids?)\b/.test(prompt) ||
    /(项目|阶段|阻塞|下一步行动|当前状态|项目进展|已有编号)/.test(prompt);
  const knowledgePrompt =
    /\b(knowledge base|rag)\b/.test(prompt) ||
    /(知识库|检索增强)/.test(prompt);

  const clearlyReadOnly = explicitReadOnly || isGenericAffirmativePrompt(prompt) ||
    (!artifactProposalRequested && !artifactApplyRequested &&
    !manuscriptPatchRequested && !otherProposalRequested && !intakeProposalRequested &&
    (groundedProjectStatusPrompt || isClearlyReadOnlyInformationRequest(prompt)));
  const toolIntent = clearlyReadOnly
    ? "read_only"
    : intakeProposalRequested
      ? "intake_proposal"
      : artifactProposalRequested
      ? "artifact_proposal"
      : artifactApplyRequested
        ? "artifact_apply"
        : manuscriptPatchRequested
          ? "manuscript_proposal"
          : otherProposalRequested
            ? "governed_proposal"
            : "default";

  return {
    expectedLane,
    memoryPrompt,
    memoryAuthorityClaim,
    historyRetrievalPrompt,
    projectPrompt,
    projectBootstrapPrompt,
    projectTaskBootstrapPrompt,
    knowledgePrompt,
    openResearchRoute,
    artifactProposalRequested,
    artifactApplyRequested,
    groundedProjectStatusPrompt,
    toolIntent,
  };
}

/**
 * Restrict a lane's advertised tools to the user's per-turn mutation intent.
 * Read tools remain available in every mode. "artifact_proposal" authorizes
 * only a pending artifact proposal, never a formal apply/publish operation.
 *
 * @param {string[]} toolNames
 * @param {"default"|"read_only"|"artifact_proposal"|"artifact_apply"|"manuscript_proposal"|"governed_proposal"|"intake_proposal"} toolIntent
 * @returns {string[]}
 */
export function filterToolsForIntent(toolNames, toolIntent = "default") {
  if (toolIntent === "default") return [...toolNames];
  return toolNames.filter((name) => {
    if (toolIntent === "intake_proposal" && ["sf_intake_create", "sf_intake_anchor", "sf_intake_prepare", "sf_intake_propose"].includes(name)) return true;
    if (name === "sf_artifact_propose_publish") return toolIntent === "artifact_proposal";
    if (name === "sf_artifact_apply_publish") return toolIntent === "artifact_apply";
    if (name === "sf_manuscript_propose_patch") return toolIntent === "manuscript_proposal";
    if (!isGovernedMutationTool(name)) return true;
    if (toolIntent === "governed_proposal") return isProposalTool(name);
    return false;
  });
}

/** @param {string} name */
export function isToolAllowedForIntent(name, toolIntent = "default") {
  return filterToolsForIntent([name], toolIntent).includes(name);
}

function stripUntrustedIntentContent(text) {
  return String(text ?? "")
    .replace(/```[\s\S]*?```|~~~[\s\S]*?~~~/g, " ")
    .replace(/BEGIN UNTRUSTED(?: CONTENT)?[\s\S]*?END UNTRUSTED(?: CONTENT)?/gi, " ")
    .replace(/<(?:untrusted|quoted)(?:[-_ ]content)?\b[^>]*>[\s\S]*?<\/(?:untrusted|quoted)(?:[-_ ]content)?\s*>/gi, " ")
    .replace(/^\s*>[^\r\n]*(?:\r?\n|$)/gm, " ")
    .replace(/`[^`\r\n]*`/g, " ")
    .replace(/“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』/g, " ")
    .replace(/"[^"\r\n]*"/g, " ")
    .replace(/(?<![A-Za-z0-9])'[^'\r\n]+'(?![A-Za-z0-9])/g, " ");
}

function removeNegatedActionClauses(prompt) {
  const englishNegatedClause =
    /\b(?:do not|don['’]t|never|without|no need to)\b(?:(?![.!?;\r\n]|\b(?:but|however|instead|rather|except)\b)[^.!?;\r\n])*/gi;
  const englishMutationAction =
    /\b(?:write|edit|revise|patch|change|create|start|build|bootstrap|propose|proposal|apply|publish|commit|persist|save|store|update|record|add|delete|remove|upsert|sync|move|mark|set|log|intake|draft|prepare|generate|produce|emit|provide|submit)\w*\b/i;
  const chineseNegatedClause =
    /(?:不要|无需|不需要|不用|不必|不得|不应)(?:(?![。！？；.!?\r\n]|(?:但是|不过|然而|而是|但))[^。！？；.!?\r\n])*/gu;
  const chineseMutationAction =
    /(?:写入|编辑|修订|修改|更改|变更|创建|新建|启动|建立|提出|提议|提案|应用|发布|提交|持久化|保存|存储|归档|更新|记录|添加|删除|同步|标记|设置|移动|起草|准备|生成|产出|给出|拟定|编制)/u;

  return prompt
    // Consume a complete negated action clause, including comma-separated
    // action lists. Stop before contrastive language so a positive request
    // after "but" / "但" remains classifiable.
    .replace(englishNegatedClause, (clause) => englishMutationAction.test(clause) ? " " : clause)
    .replace(/\bno\s+(?:(?:typed|pending|artifact)\s+)?(?:proposal|writes?|changes?)\b[^,;.!?\r\n]*/gi, " ")
    .replace(chineseNegatedClause, (clause) => chineseMutationAction.test(clause) ? " " : clause);
}

function isGenericAffirmativePrompt(prompt) {
  const normalized = prompt.trim().replace(/[.!?。！？]+$/g, "").trim();
  return /^(?:yes|yeah|yep|sure|ok(?:ay)?|go ahead|proceed|sounds good|approved|confirmed|confirm)(?:[\s,]+(?:go ahead|proceed|please))?$/i.test(normalized) ||
    /^(?:是的|好的|好|可以|同意|确认|继续|就这样)(?:[，,\s]+(?:继续|照办))?$/.test(normalized);
}

function explicitlyRequestsArtifactProposal(prompt) {
  const artifactSubject =
    /\b(?:artifact|report|review|synthesis|manuscript|research plan|daily report|weekly report)\b/.test(prompt) ||
    /(?:产物|成果|报告|综述|手稿|稿件|研究计划|日报|周报)/.test(prompt);
  if (!artifactSubject) return false;

  const proposalRequest =
    /\b(?:produce|create|prepare|draft|generate|emit|provide|submit)\b[\s\S]{0,80}\b(?:typed\s+|pending\s+)?(?:artifact|report|review|synthesis|manuscript|research plan|daily report|weekly report)\b[\s\S]{0,50}\bproposal\b/.test(prompt) ||
    /\b(?:produce|create|prepare|draft|generate|emit|provide|submit)\b[\s\S]{0,80}\b(?:typed\s+)?proposal\b[\s\S]{0,60}\b(?:artifact|report|review|synthesis|manuscript|research plan)\b/.test(prompt) ||
    /\bpropose\b[\s\S]{0,60}\b(?:artifact|report|review|synthesis|manuscript|research plan)\b/.test(prompt) ||
    /(?:生成|创建|准备|起草|产出|给出|提出|拟定|编制).{0,35}(?:产物|成果|报告|综述|手稿|稿件|研究计划|日报|周报).{0,30}(?:提案|proposal|草案)/.test(prompt);
  const explicitlySavesArtifact =
    /\b(?:save|persist|store)\b[\s\S]{0,60}\b(?:artifact|report|review|synthesis|manuscript|research plan|daily report|weekly report)\b/.test(prompt) ||
    /\b(?:artifact|report|review|synthesis|manuscript|research plan|daily report|weekly report)\b[\s\S]{0,60}\b(?:save|persist|store)\b/.test(prompt) ||
    /(?:保存|存储|归档).{0,30}(?:产物|成果|报告|综述|手稿|研究计划|日报|周报|artifact)/.test(prompt);
  return proposalRequest || explicitlySavesArtifact;
}

function explicitlyRequestsArtifactApply(prompt) {
  return /\b(?:apply|publish|commit)\b[\s\S]{0,60}\b(?:artifact|proposal|report|review|synthesis)\b/.test(prompt) ||
    /\b(?:apply|publish|commit)\s+(?:the|this|existing|pending)\s+(?:artifact|proposal)\b/.test(prompt) ||
    /(?:应用|发布|提交).{0,24}(?:提案|产物|报告|综述|手稿)/.test(prompt);
}

function isGroundedProjectStatusQuestion(prompt) {
  const asksAboutState =
    /\b(?:blocker|blocked|stuck|current status|current state|progress|next action|next step|risk)\b/.test(prompt) ||
    /(卡在哪里|卡点|卡住|阻塞|当前状态|目前状态|进展|风险|下一步)/.test(prompt);
  const groundingCue =
    /\b(?:current|currently|stored|recorded|according to|scholarflow|from the record|in the record)\b/.test(prompt) ||
    /(当前|目前|记录|以.{0,8}为准|核对|查证)/.test(prompt);
  const asksForRead =
    /\b(?:what|where|which|check|verify|confirm|tell me|show me|report)\b/.test(prompt) ||
    /(核对|检查|查询|告诉|说明|确认|查一下)/.test(prompt);
  return asksAboutState && groundingCue && asksForRead;
}

function explicitlyRequestsManuscriptPatch(prompt) {
  return /\b(?:edit|revise|patch|update)\b[\s\S]{0,50}\b(?:existing|local|manuscript|section)\b/.test(prompt) ||
    /(?:修改|修订|编辑|更新).{0,24}(?:已有|本地|现有)?(?:手稿|论文|章节)/.test(prompt);
}

function explicitlyRequestsOtherGovernedProposal(prompt) {
  const diagnosticProposal = /\b(?:experiment|run|convergence)\b|实验|收敛|运行/.test(prompt) && (
    /\bpropose\b[^.!?\n]{0,45}\bdiagnostic\b[^.!?\n]{0,25}\b(?:step|check|plan)\b/.test(prompt) ||
    /(?:提出|拟定|准备|给出).{0,20}诊断.{0,12}(?:步骤|方案|提案|检查)/.test(prompt)
  );
  return diagnosticProposal || /\b(?:prepare|produce|generate|draft|create|submit|provide)\b[^.!?\n]{0,60}\b(?:task|project|stage|experiment|paper|relation|citation)\b[^.!?\n]{0,35}\bproposals?\b/.test(prompt) ||
    /(?:准备|生成|产出|起草|拟定|编制|给出).{0,24}(?:任务|项目|阶段|实验|论文|关系|引用).{0,24}(?:提案|草案|proposal)/.test(prompt) ||
    /\bpropose\b[\s\S]{0,60}\b(?:task|project|stage|experiment|paper|relation|citation)\b/.test(prompt) ||
    /^(?:please\s+)?request\b[\s\S]{0,60}\b(?:task|project|stage|experiment|paper|relation|citation)\b/.test(prompt) ||
    /\b(?:create|start|add|update|change|move|mark|set|log|intake)\b[\s\S]{0,60}\b(?:task|project|stage|experiment|paper|relation|citation)\b/.test(prompt) ||
    /\b(?:task|project|stage|experiment|paper)\b[\s\S]{0,60}\b(?:propose|create|update|change|mark|set|log|intake)\b/.test(prompt) ||
    /(?:提出|提议|创建|新建|启动|添加|更新|修改|变更|标记|设置).{0,32}(?:任务|项目|阶段|实验|论文|关系|引用)/.test(prompt);
}

function isIntakePreparationRequest(prompt) {
  return /^(?:(?:please|can you|could you)\s+)?(?:add|capture|record|collect)\b[^.!?\n]{0,70}\b(?:idea|research request)\b/.test(prompt) ||
    /^(?:请|请你|帮我|请帮我)?(?:收集|记录|添加|捕获|收录).{0,24}(?:想法|研究需求|研究请求)/.test(prompt) ||
    /^(?:请)?(?:把|将).{0,40}(?:想法|需求).{0,24}(?:放进|加入|收录).{0,12}(?:收件箱|项目)/.test(prompt);
}

function isExplicitlyReadOnlyRequest(prompt, hasExplicitProposalIntent = false) {
  const readOnlyLabel = /\b(?:read[- ]only|read only|readonly|no changes|just answer|only inspect|only check)\b/.test(prompt) ||
    /(?:只读|仅查看|仅查询|仅检查)/.test(prompt);
  if (readOnlyLabel) return true;
  if (/^(?:please\s+)?(?:explain|describe|show(?: me)?|tell me)\b.{0,24}\bhow (?:to|i can)\b|^how (?:do|can|should) (?:i|we)\b/.test(prompt) ||
    /^(?:请)?(?:解释|说明|告诉我|教我|介绍).{0,12}(?:如何|怎么|怎样)/.test(prompt)) return true;

  // A specific pending-proposal request is still a proposal when the user
  // explicitly says not to apply it. An explicit read-only label above still
  // wins, while denials without a proposal request remain read-only.
  // Use the same negated-action vocabulary as routing, so an operation cannot
  // be removed from one stage yet accidentally authorize another stage.
  return removeNegatedActionClauses(prompt) !== prompt && !hasExplicitProposalIntent;
}

function isAutomationQueueRequest(prompt) {
  const queueTarget =
    /\b(?:automation\s+)?(?:inbox|queue)\b|\b(?:queued|pending)\s+(?:inbox\s+|queue\s+)(?:message|item|request)s?\b|\b(?:queue|inbox)\s+(?:message|item|request)s?\b|\bautomation\s+(?:message|item|request)s?\b/i.test(prompt) ||
    /(?:收件箱|消息队列|队列消息|队列中的消息|待处理收件箱|待处理队列|待确认消息|自动化.{0,8}(?:待处理)?(?:消息|请求))/u.test(prompt);
  if (!queueTarget) return false;

  return /\b(?:check|inspect|list|show|search|read|review|process|handle|triage|acknowledge|open|view|retrieve|fetch|look\s+at|go\s+through)\b/i.test(prompt) ||
    /\b(?:what|which|any|how\s+many)\b.{0,40}\b(?:in|inside|from|within)\b.{0,20}\b(?:inbox|queue)\b/i.test(prompt) ||
    /(?:检查|查看|列出|搜索|读取|审查|处理|接收|核对|有什么|有哪些|是否有|多少)/u.test(prompt);
}

function isClearlyReadOnlyInformationRequest(prompt) {
  const asksForInformation =
    /^(?:please\s+)?(?:what|where|when|who|which|how many|how much|can you|could you|tell me|show(?: me)?|read|inspect|view|get|retrieve|fetch|check|verify|confirm|explain|summari[sz]e|list|describe)\b/.test(prompt) ||
    /^(?:请)?(?:核对|检查|查询|查看|读取|显示|审阅|告诉我|说明|确认|总结|列出|描述)/.test(prompt);
  const groundedSource =
    /\b(?:current|currently|stored|recorded|existing|proposal|from the record|according to|scholarflow|evidence|only the literature already stored)\b/.test(prompt) ||
    /(当前|目前|记录|存储|已有|现有|提案|依据|根据|证据|文献)/.test(prompt);
  const recommendationRequest =
    /\b(?:should|recommend|recommendation|suggest|what would you do|how should)\b/.test(prompt) ||
    /(建议|应该|怎么规划|如何安排)/.test(prompt);
  const explicitAction =
    /\b(?:create|draft|write|prepare|generate|propose|publish|save|apply|update|change|add|record)\b/.test(prompt) ||
    /(创建|起草|撰写|准备|生成|提出|发布|保存|应用|更新|修改|添加|记录)/.test(prompt);
  return asksForInformation && groundedSource && !recommendationRequest && !explicitAction;
}

function isProposalTool(name) {
  return /(?:^|_)(?:propose|proposal)(?:_|$)/.test(name) || name === "sf_intake_propose";
}

function isGovernedMutationTool(name) {
  return /(?:^|_)(?:propose|apply|create|anchor|write|ack|commit|delete|update|upsert|sync|ingest|import|refresh|prepare|compile)(?:_|$)/.test(name);
}

/**
 * Workbench prompts contain an auditable context-policy preamble. Routing must
 * classify the user's request, not keywords inside that generated preamble.
 *
 * @param {string} rawPrompt
 * @returns {string}
 */
export function extractScholarFlowUserPrompt(rawPrompt) {
  const marker = "\n\nUser request:\n";
  const prompt = String(rawPrompt ?? "");
  const markerIndex = prompt.indexOf(marker);
  return markerIndex >= 0 ? prompt.slice(markerIndex + marker.length) : prompt;
}

/**
 * "My memory says …" / treat-memory-as-authority claims (T3.4).
 * These are unverified user statements — they must not open history tools.
 *
 * @param {string} prompt
 */
export function isExplicitMemoryAuthorityPrompt(prompt) {
  const text = String(prompt ?? "");
  const negatedMemory =
    /\b(?:do not|don['’]t|never|without|no need to)\b[^.!?;\r\n]{0,80}\bmemory\b/i.test(text) ||
    /(?:不要|无需|不需要|不用|不必|不得|不应)[^。！？；.!?\r\n]{0,60}记忆/u.test(text);
  if (negatedMemory) return false;
  return (
    /\bmemory\s+(?:says|states|claims|confirms)\b/i.test(text) ||
    /\b(?:treat|use|consider)\b[^.!?;\r\n]{0,50}\bmemory\b[^.!?;\r\n]{0,50}\bauthoritative\b/i.test(text) ||
    /\bskip\s+scholarflow(?:\s+\w+){0,3}\s+tools?\b/i.test(text) ||
    /(?:记忆|memory)[^。！？；.!?\r\n]{0,40}(?:权威|为准|可信|跳过.{0,12}(?:工具|scholarflow))/u.test(text)
  );
}

/**
 * User explicitly asks to retrieve prior discussion / session history (T3.5).
 *
 * @param {string} prompt
 */
export function isExplicitHistoryRetrievalPrompt(prompt) {
  const text = String(prompt ?? "");
  return (
    /\b(?:search|retrieve|find|look\s+up|recall)\b[^.!?;\r\n]{0,60}\b(?:prior|previous|earlier|past|old)\b[^.!?;\r\n]{0,40}\b(?:discussion|conversation|session|chat|history|messages?)\b/i.test(text) ||
    /\b(?:search|retrieve|find)\b[^.!?;\r\n]{0,40}\b(?:session_search|memory_search|session history|chat history)\b/i.test(text) ||
    /\bwhat\s+did\s+we\s+(?:discuss|say|decide)\b[^.!?;\r\n]{0,40}\b(?:earlier|before|previously|last\s+time)\b/i.test(text) ||
    /(?:检索|查找|搜索|回看|调出).{0,40}(?:历史讨论|历史会话|之前的讨论|以前聊过|会话历史|对话记录)/u.test(text) ||
    /(?:之前|上次|早先).{0,20}(?:讨论|说过|决定).{0,30}(?:查一下|找一下|搜索|检索)/u.test(text)
  );
}

