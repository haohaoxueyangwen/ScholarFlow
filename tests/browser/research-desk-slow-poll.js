async (page) => {
  // Only the disposable H037 control workspace is accepted. Responses are
  // delayed and their status is changed in the browser; no backend data changes.
  const target = new URL(page.url());
  if (target.hostname !== "127.0.0.1" || target.port !== "13137" ||
      target.searchParams.get("project") !== "stage-h037-control" ||
      !/^workspace-[a-f0-9]{32}$/.test(target.searchParams.get("workspace") || "")) {
    throw new Error("H037 isolated control workspace required");
  }
  const workspace = target.searchParams.get("workspace");
  let active = 0, maximum = 0, requests = 0;
  const pattern = (url) => url.pathname === `/workspaces/${workspace}`;
  await page.route(pattern, async (route) => {
    active++; maximum = Math.max(maximum, active);
    try {
      const response = await route.fetch();
      const payload = await response.json();
      requests++;
      payload.knowledge.status = requests < 3 ? "running" : "indexed";
      await new Promise((resolve) => setTimeout(resolve, 3200));
      await route.fulfill({ response, json: payload });
    } finally { active--; }
  });
  try {
    await page.goto(target.toString());
    await page.getByRole("heading", { name: "document.pdf" }).waitFor({ timeout: 30000 });
    const pi = page.getByRole("button", { name: "问 Pi" });
    await pi.waitFor({ timeout: 30000 });
    await pi.click();
    const notes = page.getByRole("button", { name: /笔记审阅/ });
    await notes.waitFor({ timeout: 30000 });
    await notes.click();
    await page.getByText("此笔记索引：已入库").waitFor({ timeout: 60000 });
    if (requests < 3 || maximum !== 1) throw new Error(`poll concurrency=${maximum}, requests=${requests}`);
    return { requests, max_concurrent: maximum, terminal_status: "indexed", model_calls: 0 };
  } finally {
    await page.unroute(pattern);
  }
}
