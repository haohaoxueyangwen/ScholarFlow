async (page) => {
  function syntheticPdf(pageCount) {
    const objects = [
      "<< /Type /Catalog /Pages 2 0 R >>",
      `<< /Type /Pages /Count ${pageCount} /Kids [${Array.from({ length: pageCount }, (_, index) => `${index + 3} 0 R`).join(" ")}] >>`,
      ...Array.from({ length: pageCount }, (_, index) => {
        const mediaBox = index % 3 === 0 ? "[0 0 612 792]" : index % 3 === 1 ? "[0 0 792 612]" : "[0 0 612 612]";
        const rotation = index % 2 === 1 ? " /Rotate 90" : "";
        const fontObjectId = pageCount * 2 + 3;
        const contentObjectId = pageCount + 3 + index;
        return "<< /Type /Page /Parent 2 0 R /MediaBox " + mediaBox + rotation + " /Resources << /Font << /F1 " + fontObjectId + " 0 R >> >> /Contents " + contentObjectId + " 0 R >>";
      }),
      ...Array.from({ length: pageCount }, (_, index) => {
        const text = "SF acceptance page " + (index + 1);
        const stream = "BT /F1 12 Tf 72 180 Td (" + text + ") Tj ET";
        const newline = String.fromCharCode(10);
        return "<< /Length " + stream.length + " >>" + newline + "stream" + newline + stream + newline + "endstream";
      }),
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ];
    let output = "%PDF-1.7\n";
    const offsets = [0];
    objects.forEach((object, index) => { offsets.push(output.length); output += `${index + 1} 0 obj\n${object}\nendobj\n`; });
    const xref = output.length;
    output += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
    return output;
  }
  const consoleErrors = [];
  let expectedOfflineFailure = false;
  let workbenchApiBase = "";
  page.on("console", (message) => {
    if (message.type() === "error") {
      const text = message.text();
      if (!(expectedOfflineFailure && text.includes("net::ERR_FAILED"))) {
        consoleErrors.push({ text, location: message.location(), args: message.args().map((arg) => arg.toString()) });
      }
    }
  });
  page.on("request", (request) => {
    const apiOrigin = /^(https?:\/\/[^/?#]+)\/api\//.exec(request.url());
    if (apiOrigin) workbenchApiBase = `${apiOrigin[1]}/api`;
  });

  await page.reload();
  await page.getByText("本地数据已连接", { exact: true }).waitFor({ state: "visible", timeout: 30000 });
  if (!workbenchApiBase) throw new Error("could not discover the isolated Workbench API base URL");
  if ((await page.getByRole("navigation", { name: "主导航" }).count()) !== 1) {
    throw new Error("main navigation is missing");
  }

  const workspaceScroll = await page.locator("#main-workspace").evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    overflowY: getComputedStyle(element).overflowY,
  }));
  if (!['auto', 'scroll'].includes(workspaceScroll.overflowY) || workspaceScroll.clientHeight <= 0) {
    throw new Error(`main workspace is not independently scrollable: ${JSON.stringify(workspaceScroll)}`);
  }
  const conversationScroll = await page.locator(".conversation").evaluate((element) => ({
    clientHeight: element.clientHeight,
    overflowY: getComputedStyle(element).overflowY,
  }));
  if (!['auto', 'scroll'].includes(conversationScroll.overflowY) || conversationScroll.clientHeight <= 0) {
    throw new Error(`Pi conversation is not independently scrollable: ${JSON.stringify(conversationScroll)}`);
  }
  if (!(await page.getByLabel("向 Pi 提问").isVisible())) {
    throw new Error("Pi composer is missing");
  }
  const sessionLevels = page.getByLabel("会话层级");
  for (const level of ["总控", "项目", "任务"]) {
    if (!(await sessionLevels.getByRole("button", { name: level, exact: true }).isVisible())) {
      throw new Error(`Pi ${level} session level is missing`);
    }
  }

  await page.getByRole("button", { name: "系统", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "能力验证", exact: true }).click();
  if (!(await page.getByRole("heading", { name: /运行边界和验证证据/ }).isVisible())) {
    throw new Error("agent capability evidence is not reachable");
  }
  if (!(await page.getByText("Pi 基座", { exact: true }).isVisible())) {
    throw new Error("Pi runtime evidence is missing");
  }
  const liveHandshake = page.getByRole("button", { name: /验证 Pi 会话|验证通过/ });
  await liveHandshake.waitFor({ state: "visible" });
  if (!(await liveHandshake.isVisible())) {
    throw new Error("live Pi handshake control is missing");
  }

  await page.getByRole("navigation", { name: "主导航" }).getByRole("button", { name: "写作", exact: true }).click();
  await page.getByRole("heading", { name: "论文写作", exact: true }).waitFor({ state: "visible" });
  await page.getByLabel("切换写作项目").selectOption("stage-e2e");
  const manuscriptTitle = `Browser gate inline manuscript ${Date.now()}`;
  await page.getByRole("button", { name: "新建本地工程", exact: true }).click();
  await page.getByLabel("论文标题").fill(manuscriptTitle);
  await page.getByRole("button", { name: "生成创建 Proposal", exact: true }).click();
  await page.getByRole("heading", { name: "Proposal Center", exact: true }).waitFor({ state: "visible" });
  const manuscriptCreateProposal = page.locator(".proposal-card")
    .filter({ hasText: "manuscript.create" })
    .filter({ hasText: manuscriptTitle });
  await manuscriptCreateProposal.waitFor({ state: "visible" });
  await manuscriptCreateProposal.getByRole("button", { name: "确认并应用", exact: true }).click();

  await page.getByRole("navigation", { name: "主导航" }).getByRole("button", { name: "写作", exact: true }).click();
  const manuscriptRow = page.locator(".writing-manuscript-list article").filter({ hasText: manuscriptTitle });
  await manuscriptRow.waitFor({ state: "visible", timeout: 30000 });
  if (!(await manuscriptRow.getByText(/免费版手动交接/).isVisible())) {
    throw new Error("manual free-Overleaf ZIP handoff instructions are missing");
  }
  const bundleDownloadEvent = page.waitForEvent("download");
  await manuscriptRow.getByRole("button", { name: "下载 Overleaf ZIP", exact: true }).click();
  const bundleDownload = await bundleDownloadEvent;
  if (!bundleDownload.suggestedFilename().endsWith(".zip")) {
    throw new Error(`Overleaf handoff did not download a ZIP: ${bundleDownload.suggestedFilename()}`);
  }

  const fixtureManuscriptId = await page.evaluate(async ({ apiBase, title }) => {
    const response = await fetch(`${apiBase}/manuscripts?project_stage_id=stage-e2e`, { credentials: "include" });
    const payload = await response.json();
    return payload.items.find((item) => item.title === title).manuscript_id;
  }, { apiBase: workbenchApiBase, title: manuscriptTitle });
  const detailUrl = `${workbenchApiBase}/manuscripts/${encodeURIComponent(fixtureManuscriptId)}`;
  const mismatchedDetail = async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), project_stage_id: "stage-other-fixture" } });
  };
  await page.route(detailUrl, mismatchedDetail);
  await manuscriptRow.getByRole("button", { name: "写作", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "稿件不属于当前写作项目" }).waitFor();
  await page.unroute(detailUrl, mismatchedDetail);

  let releaseInitialSection;
  let initialSectionStarted;
  let initialSectionFinished;
  const initialSectionGate = new Promise((resolve) => { releaseInitialSection = resolve; });
  const initialSectionSeen = new Promise((resolve) => { initialSectionStarted = resolve; });
  const initialSectionDone = new Promise((resolve) => { initialSectionFinished = resolve; });
  const abstractUrl = `${detailUrl}/sections/abstract`;
  const delayedInitialSection = async (route) => {
    const response = await route.fetch();
    initialSectionStarted();
    await initialSectionGate;
    await route.fulfill({ response });
    initialSectionFinished();
  };
  await page.route(abstractUrl, delayedInitialSection);
  await manuscriptRow.getByRole("button", { name: "写作", exact: true }).click();
  const manuscriptEditor = page.locator(".writing-section-editor");
  await manuscriptEditor.waitFor({ state: "visible" });
  await initialSectionSeen;
  await manuscriptEditor.getByLabel("目标章节").selectOption("results");
  await manuscriptEditor.locator("button:enabled").filter({ hasText: "刷新章节与 Proposal" }).waitFor();
  const selectedResultsLatex = await manuscriptEditor.getByLabel("当前已接受的 LaTeX（只读）").inputValue();
  releaseInitialSection();
  await initialSectionDone;
  await manuscriptRow.locator("button:enabled").filter({ hasText: /^写作$/ }).waitFor();
  if (await manuscriptEditor.getByLabel("目标章节").inputValue() !== "results") {
    throw new Error("delayed initial section overwrote the newer section selection");
  }
  if (await manuscriptEditor.getByLabel("当前已接受的 LaTeX（只读）").inputValue() !== selectedResultsLatex) {
    throw new Error("delayed initial section replaced the selected results snapshot");
  }
  await page.unroute(abstractUrl, delayedInitialSection);
  await manuscriptEditor.getByLabel("目标章节").selectOption("abstract");
  const proseInput = manuscriptEditor.getByLabel("你想表达的普通文本");
  await proseInput.fill("A browser-gate prose note that remains routed through Pi.");
  await manuscriptEditor.locator("button:enabled").filter({ hasText: "交给 Pi 转为 LaTeX Proposal" }).waitFor();
  if (!(await manuscriptEditor.getByRole("button", { name: "交给 Pi 转为 LaTeX Proposal", exact: true }).isEnabled())) {
    throw new Error("natural-language-to-Pi manuscript proposal path is not available");
  }

  const manuscriptPatch = await page.evaluate(async ({ apiBase, stageId, title }) => {
    async function readJson(response) {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || `Workbench request failed (${response.status})`);
      return payload;
    }
    const list = await readJson(await fetch(`${apiBase}/manuscripts?project_stage_id=${encodeURIComponent(stageId)}`, {
      credentials: "include",
      cache: "no-store",
    }));
    const manuscript = list.items.find((item) => item.title === title);
    if (!manuscript) throw new Error("isolated manuscript fixture was not found");
    const section = await readJson(await fetch(`${apiBase}/manuscripts/${encodeURIComponent(manuscript.manuscript_id)}/sections/abstract`, {
      credentials: "include",
      cache: "no-store",
    }));
    const proposal = await readJson(await fetch(`${apiBase}/manuscripts/${encodeURIComponent(manuscript.manuscript_id)}/patch-proposals`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-ScholarFlow-Intent": "confirm" },
      body: JSON.stringify({
        section: "abstract",
        replacement_latex: "Browser gate accepted patch.",
        evidence_refs: ["paper:paper-e2e"],
        base_file_hash: section.file_hash,
      }),
    }));
    return { manuscriptId: manuscript.manuscript_id, proposalId: proposal.proposal_id };
  }, { apiBase: workbenchApiBase, stageId: "stage-e2e", title: manuscriptTitle });

  const patchPanel = manuscriptEditor.getByRole("region", { name: "待确认章节补丁" });
  await patchPanel.getByRole("button", { name: "刷新章节与 Proposal", exact: true }).click();
  await patchPanel.getByRole("heading", { name: "章节修改待你审阅", exact: true }).waitFor({ state: "visible" });
  if (!(await patchPanel.getByText("paper:paper-e2e", { exact: true }).isVisible())) {
    throw new Error("inline manuscript proposal is missing its typed evidence reference");
  }
  const proposedLatex = await patchPanel.locator("pre.after").textContent();
  if (!proposedLatex?.includes("Browser gate accepted patch.")) {
    throw new Error("inline manuscript before/after review did not show the proposed LaTeX");
  }
  const priorLatex = await patchPanel.locator("pre.before").textContent();
  if (!priorLatex) throw new Error("inline manuscript review is missing the previous section text");
  // A second display-only proposal tests global decision serialization; it is never submitted.
  const proposalsUrl = `${workbenchApiBase}/proposals`;
  const secondProposalFixture = async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const original = payload.items.find((item) => item.proposal_id === manuscriptPatch.proposalId);
    if (original) payload.items.push({ ...original, proposal_id: "browser-fixture-never-submit", diff: { ...original.diff, section: { ...original.diff.section, after: "Second display-only proposal." } } });
    await route.fulfill({ response, json: payload });
  };
  await page.route(proposalsUrl, secondProposalFixture);
  await patchPanel.getByRole("button", { name: "刷新章节与 Proposal", exact: true }).click();
  await patchPanel.getByText("Second display-only proposal.", { exact: true }).waitFor();
  let releaseDecision;
  let decisionStarted;
  const decisionGate = new Promise((resolve) => { releaseDecision = resolve; });
  const decisionSeen = new Promise((resolve) => { decisionStarted = resolve; });
  const confirmUrl = `${workbenchApiBase}/proposals/${encodeURIComponent(manuscriptPatch.proposalId)}/confirm`;
  const delayedDecision = async (route) => {
    decisionStarted();
    await decisionGate;
    await route.continue();
  };
  await page.route(confirmUrl, delayedDecision);
  await patchPanel.getByRole("button", { name: "确认并应用章节补丁", exact: true }).first().click();
  await decisionSeen;
  const decisionButtons = patchPanel.locator(".proposal-actions button");
  if (await decisionButtons.count() !== 4) throw new Error("concurrent-decision fixture did not render both proposals");
  for (const button of await decisionButtons.all()) {
    if (await button.isEnabled()) throw new Error("a manuscript decision remained enabled while another decision was in flight");
  }
  if (await patchPanel.getByRole("button", { name: "刷新章节与 Proposal", exact: true }).isEnabled()) {
    throw new Error("refresh remained enabled and could supersede the in-flight manuscript decision");
  }
  releaseDecision();
  await patchPanel.getByText("章节补丁已确认并应用", { exact: false }).waitFor({ state: "visible", timeout: 30000 });
  await patchPanel.getByText("此章节目前没有待确认补丁。", { exact: false }).waitFor({ state: "visible", timeout: 30000 });
  await page.unroute(confirmUrl, delayedDecision);
  await page.unroute(proposalsUrl, secondProposalFixture);
  const acceptedLatex = await manuscriptEditor.getByLabel("当前已接受的 LaTeX（只读）").inputValue();
  if (!acceptedLatex.includes("Browser gate accepted patch.")) {
    throw new Error(`confirmed manuscript patch did not refresh the accepted section (proposal ${manuscriptPatch.proposalId})`);
  }

  await page.getByRole("button", { name: "文献", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "AI 知识库", exact: true }).click();
  await page.getByRole("region", { name: "知识库运行状态" }).waitFor();
  const runtimeHeading = page.getByRole("heading", { name: /LightRAG/ });
  if (!(await runtimeHeading.isVisible())) {
    throw new Error("knowledge runtime truth is missing");
  }
  await page.getByText("paper:paper-e2e", { exact: true }).waitFor({ timeout: 5000 });
  const runtimeText = await runtimeHeading.textContent();
  const searchEnabled = await page.getByRole("button", { name: "检索", exact: true }).isEnabled();
  if (runtimeText?.includes("当前可检索") !== searchEnabled) {
    throw new Error("knowledge search availability does not match current runtime truth");
  }
  if (!(await page.getByRole("heading", { name: "项目知识自动更新", exact: true }).isVisible())) {
    throw new Error("automatic project knowledge status is missing");
  }
  if (!(await page.getByRole("button", { name: "重试待处理项目", exact: true }).isVisible())) {
    throw new Error("project knowledge retry control is missing");
  }

  await page.getByRole("button", { name: "研究", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "项目", exact: true }).click();
  await page.getByRole("navigation", { name: "项目内部视图" }).waitFor({ state: "visible" });
  if (!(await page.getByRole("navigation", { name: "项目内部视图" }).isVisible()) ||
      !(await page.getByLabel("切换项目中心").isVisible())) {
    throw new Error("project center is missing");
  }
  await page.getByText("编辑项目事实与状态", { exact: true }).click();
  await page.getByRole("button", { name: "编辑并生成 Proposal", exact: true }).click();
  await page.getByLabel("项目进展说明").fill("Browser gate project proposal.");
  await page.getByRole("button", { name: "生成更新 Proposal", exact: true }).click();
  await page.getByRole("heading", { name: "Proposal Center" }).waitFor();
  const projectProposal = page.locator(".proposal-card").filter({ hasText: "project_stage.update" });
  await projectProposal.getByLabel("project_stage.update 的拒绝理由").fill("Browser gate rejects this project update.");
  await projectProposal.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.getByText("Browser gate rejects this project update.", { exact: true }).waitFor();
  if (await projectProposal.count()) {
    throw new Error("rejected project proposal remained pending");
  }
  await page.screenshot({
    path: "output/playwright/workbench-proposal-receipt.png",
    fullPage: true,
  });

  await page.getByRole("button", { name: "创建首个任务", exact: true }).click();
  await page.getByRole("heading", { name: /定义首个可交付任务/ }).waitFor();
  await page.getByLabel("首个任务标题").fill("Extract comparable evidence fields");
  await page.getByLabel("首个任务完成定义").fill("Produce a bounded comparison table linked to the fixture paper.");
  await page.getByRole("button", { name: "生成首任务 Proposal", exact: true }).click();
  const firstTaskProposal = page.locator(".proposal-card").filter({ hasText: "task.create" });
  await firstTaskProposal.waitFor();
  if (!(await firstTaskProposal.getByText("paper:paper-e2e", { exact: false }).isVisible())) {
    throw new Error("first task proposal did not preserve project paper evidence");
  }
  await firstTaskProposal.getByLabel("task.create 的拒绝理由").fill("Browser gate verified the first-task proposal.");
  await firstTaskProposal.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.getByText("Browser gate verified the first-task proposal.", { exact: true }).waitFor();

  await page.getByRole("button", { name: "研究", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "任务", exact: true }).click();
  await page.getByText("任务说明", { exact: true }).waitFor({ state: "visible" });
  if (!(await page.getByText("任务说明", { exact: true }).isVisible())) {
    throw new Error("task domain detail is missing");
  }
  await page.getByRole("button", { name: "编辑并生成 Proposal", exact: true }).click();
  await page.getByLabel("任务优先级").fill("browser-gate-high");
  await page.getByRole("button", { name: "生成更新 Proposal", exact: true }).click();
  await page.getByRole("heading", { name: "Proposal Center" }).waitFor();
  const taskProposal = page.locator(".proposal-card")
    .filter({ hasText: "task.update" })
    .filter({ hasText: "browser-gate-high" });
  await taskProposal.getByLabel("task.update 的拒绝理由").fill("Browser gate rejects this task update.");
  await taskProposal.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.getByText("Browser gate rejects this task update.", { exact: true }).waitFor();

  await page.getByRole("button", { name: "研究", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "实验", exact: true }).click();
  await page.getByText("实验目标", { exact: true }).waitFor({ state: "visible" });
  if (!(await page.getByText("实验目标", { exact: true }).isVisible())) {
    throw new Error("experiment domain detail is missing");
  }
  if (!(await page.getByText("结果摘要", { exact: true }).isVisible())) {
    throw new Error("experiment result surface is missing");
  }
  const metrics = page.locator(".experiment-metrics");
  if (!(await metrics.getByText("accuracy", { exact: true }).isVisible()) ||
      !(await metrics.getByText("0.92", { exact: true }).isVisible())) {
    throw new Error("experiment metric visualization is missing");
  }

  await page.getByLabel("搜索所有研究对象").fill("Browser gate report");
  await page.getByRole("button", { name: /研究产物 Browser gate report/ }).waitFor({ state: "visible" });
  await page.getByRole("button", { name: /研究产物 Browser gate report/ }).click();
  await page.getByLabel("Markdown 预览").waitFor();
  if (!(await page.getByText("Deterministic fixture output.", { exact: true }).isVisible())) {
    throw new Error("artifact Markdown preview is missing");
  }
  await page.getByRole("button", { name: "编辑并生成 Proposal", exact: true }).click();
  await page.getByLabel("研究产物 Markdown 内容").fill("## Browser gate\n\nReviewed artifact content.");
  await page.getByRole("button", { name: "生成更新 Proposal", exact: true }).click();
  await page.getByRole("heading", { name: "Proposal Center" }).waitFor();
  const artifactProposal = page.locator(".proposal-card").filter({ hasText: "research_artifact.publish" });
  await artifactProposal.getByLabel("research_artifact.publish 的拒绝理由").fill("Browser gate rejects this artifact update.");
  await artifactProposal.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.getByText("Browser gate rejects this artifact update.", { exact: true }).waitFor();

  await page.getByRole("button", { name: "研究", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "产物", exact: true }).click();
  await page.getByRole("button", { name: "写入 Obsidian（Proposal）", exact: true }).waitFor({ state: "visible" });
  await page.getByRole("button", { name: "写入 Obsidian（Proposal）", exact: true }).click();
  await page.getByLabel("Obsidian 追踪链接").fill("[[wiki/entities/BrowserGate]]");
  await page.getByRole("button", { name: "生成 Obsidian Proposal", exact: true }).click();
  await page.getByRole("heading", { name: "Proposal Center" }).waitFor();
  const vaultProposal = page.locator(".proposal-card").filter({ hasText: "kb.object.write" });
  await vaultProposal.getByLabel("kb.object.write 的拒绝理由").fill("Browser gate rejects this Vault write.");
  await vaultProposal.getByRole("button", { name: "拒绝", exact: true }).click();
  await page.getByText("Browser gate rejects this Vault write.", { exact: true }).waitFor();

  await page.getByRole("button", { name: "文献", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "论文", exact: true }).click();
  const paperFixture = page.locator(".literature-results .literature-result").filter({ hasText: "E2E Zotero paper" });
  await paperFixture.waitFor({ state: "visible" });
  await paperFixture.click();
  const paperContextButton = page.getByRole("button", { name: /设为 Pi 当前论文/ });
  await paperContextButton.waitFor({ state: "visible" });
  await paperContextButton.click();
  if (!(await page.locator(".context-chip").getByText("paper:paper-e2e", { exact: true }).isVisible())) {
    throw new Error("paper context was not mounted");
  }

  await page.getByRole("button", { name: "文献", exact: true }).click();
  await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "论文", exact: true }).click();
  const upload = page.locator('input[type="file"][accept="application/pdf,.pdf"]');
  await upload.evaluate((element, source) => {
    const bytes = new TextEncoder().encode(source);
    const file = new File([bytes], "workbench-700-page.pdf", { type: "application/pdf" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    element.files = transfer.files;
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }, syntheticPdf(700));
  const stagedUpload = page.locator(".literature-results .literature-result.temporary").filter({ hasText: "workbench-700-page.pdf" });
  await stagedUpload.waitFor({ state: "visible", timeout: 15000 });
  await page.getByRole("button", { name: "正式暂存并解析" }).click();
  const openFullReader = page.getByRole("button", { name: /独立窗口打开/ });
  await page.locator(".literature-reader-identity > span").filter({ hasText: "STAGED PDF" }).waitFor({ state: "visible", timeout: 60000 });
  await openFullReader.waitFor({ state: "visible", timeout: 60000 });
  const popout = page.context().waitForEvent("page");
  await openFullReader.click();
  const readerPage = await popout;
  await readerPage.getByText("/ 700", { exact: true }).waitFor({ timeout: 20000 });
  const initialCanvasPages = await readerPage.locator(".react-pdf__Page").count();
  if (initialCanvasPages > 5) throw new Error(`long PDF rendered ${initialCanvasPages} canvases at once`);
  await readerPage.locator(".pdf-reader-toolbar input[type=number]").fill("350");
  await readerPage.locator('.pdf-page-shell[data-page-number="350"] .react-pdf__Page').waitFor({ timeout: 15000 });
  const jumpedCanvasPages = await readerPage.locator(".react-pdf__Page").count();
  if (jumpedCanvasPages > 5) throw new Error(`long PDF rendered ${jumpedCanvasPages} canvases after navigation`);
  const page350Text = readerPage.locator('.pdf-page-shell[data-page-number="350"] [data-sf-text-item]').filter({ hasText: "SF acceptance page 350" }).first();
  await page350Text.waitFor({ state: "visible", timeout: 20000 });
  await page350Text.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    element.closest(".pdf-page-viewport")?.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  await readerPage.getByRole("button", { name: "高亮所选" }).click();
  const annotationQuote = readerPage.locator(".annotation-editor textarea").first();
  await annotationQuote.waitFor({ state: "visible" });
  if (!(await annotationQuote.inputValue()).includes("SF acceptance page 350")) {
    throw new Error("selected PDF text did not populate the annotation draft");
  }
  if (!(await readerPage.locator(".annotation-editor strong").textContent())?.includes("350")) {
    throw new Error("the selected text was assigned to the wrong annotation page");
  }
  await readerPage.getByRole("button", { name: "纯净阅读" }).click();
  if (!(await readerPage.locator(".pdf-reader-body").isVisible())) {
    throw new Error("focus mode hid the PDF reading surface");
  }
  await readerPage.close();

  await page.goto(page.url().split("?")[0]);
  await page.getByRole("navigation", { name: "主导航" }).waitFor({ state: "visible" });
  await page.keyboard.press("Tab");
  const focusClass = (await page.locator(":focus").getAttribute("class")) || "";
  if (!focusClass.includes("skip-link")) {
    throw new Error("skip link is not the first keyboard target");
  }
  await page.keyboard.press("Enter");
  const activeElementId = await page.evaluate(() => document.activeElement?.id);
  if (activeElementId !== "main-workspace") {
    throw new Error("skip link did not focus main content");
  }

  await page.addScriptTag({
    path: "apps/research-workbench/node_modules/axe-core/axe.min.js",
  });
  const axeResult = await page.evaluate(async () => globalThis.axe.run(document));
  const serious = axeResult.violations.filter((item) =>
    ["serious", "critical"].includes(item.impact),
  );
  if (serious.length) {
    throw new Error(
      `axe violations: ${JSON.stringify(
        serious.map((item) => ({
          id: item.id,
          impact: item.impact,
          nodes: item.nodes.map((node) => ({
            target: node.target,
            html: node.html,
            failureSummary: node.failureSummary,
          })),
        })),
      )}`,
    );
  }

  expectedOfflineFailure = true;
  await page.route("**/api/**", (route) => route.abort());
  await page.reload();
  await page.getByText("隔离演示模式", { exact: true }).waitFor({ state: "visible", timeout: 15000 });
  await page.unroute("**/api/**");
  expectedOfflineFailure = false;
  await page.getByRole("button", { name: "重新检查连接" }).click();
  await page.getByText("本地数据已连接", { exact: true }).waitFor({ state: "visible", timeout: 15000 });

  await page.setViewportSize({ width: 800, height: 900 });
  if (!(await page.getByRole("button", { name: "系统", exact: true }).isVisible())) {
    throw new Error("mobile navigation is not reachable");
  }
  await page.getByRole("button", { name: "系统", exact: true }).click();
  if (!(await page.getByRole("navigation", { name: "当前工作区分类" }).getByRole("button", { name: "集成", exact: true }).isVisible())) {
    throw new Error("mobile system tabs are not reachable");
  }
  if (consoleErrors.length) {
    throw new Error(`browser console errors: ${JSON.stringify(consoleErrors)}`);
  }
}
