import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const requireFromFrontend = createRequire(resolve("frontend/package.json"));
const { chromium } = requireFromFrontend("@playwright/test");

function analystPassword() {
  if (process.env.ACME_ANALYST_PASSWORD) return process.env.ACME_ANALYST_PASSWORD;
  const localEnv = readFileSync("deploy/glpi/.env", "utf8");
  const line = localEnv.split(/\r?\n/).find((entry) => entry.startsWith("ACME_ANALYST_PASSWORD="));
  if (!line) throw new Error("ACME_ANALYST_PASSWORD is not configured");
  const value = line.slice("ACME_ANALYST_PASSWORD=".length).trim();
  return value.replace(/^(['"])(.*)\1$/, "$2");
}

const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1050 }, deviceScaleFactor: 1 });
  await page.goto("http://127.0.0.1:3000/", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "使用企业身份登录" }).click();
  await page.getByLabel(/username or email/i).fill("acme-analyst");
  await page.getByRole("textbox", { name: "Password", exact: true }).fill(analystPassword());
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.getByRole("heading", { name: "工作台" }).waitFor({ timeout: 30000 });
  console.log("Signed in as acme-analyst");

  await page.goto("http://127.0.0.1:3000/runs", { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "运行记录" }).waitFor({ timeout: 20000 });
  await page.getByLabel("状态筛选").selectOption("succeeded");
  await page.waitForURL(/status=succeeded/);
  await page.locator('a[href^="/runs/"]').first().waitFor({ timeout: 20000 });
  const hrefs = await page.locator("tbody tr").filter({ hasText: "只读分析" }).locator("a.primary-cell").evaluateAll((links) =>
    [...new Set(links.map((link) => link.getAttribute("href")).filter(Boolean))],
  );
  console.log(`Found ${hrefs.length} completed read-only runs on this page`);

  let selected = null;
  for (const [index, href] of hrefs.entries()) {
    await page.locator(`a[href="${href}"]`).first().click();
    await page.getByRole("heading", { name: "任务计划" }).waitFor({ timeout: 2500 }).catch(() => {});
    const ids = await page.locator(".task-grid article header span").allTextContents();
    const evidenceCount = await page.locator(".evidence-list article").count();
    const reviewCount = await page.getByRole("heading", { name: "复核结论" }).count();
    const approvalCount = await page.getByRole("button", { name: "批准并继续" }).count();
    const fourTasks = ["T1", "T2", "T3", "T4"].every((id) => ids.some((taskId) => taskId.includes(id)));
    console.log(`Candidate ${index + 1}: tasks=${ids.length}, four=${fourTasks}, evidence=${evidenceCount}, review=${reviewCount}, approve=${approvalCount}`);
    if (fourTasks && evidenceCount > 0 && reviewCount > 0 && approvalCount === 0) {
      selected = href;
      break;
    }
    await page.goBack({ waitUntil: "domcontentloaded" });
    await page.locator('a[href^="/runs/"]').first().waitFor({ timeout: 10000 });
  }
  if (!selected) throw new Error("No existing run matches the T1–T4, evidence, reviewer and analyst-role screenshot requirements");

  await page.evaluate(() => {
    const main = document.querySelector("section.page");
    if (!main) throw new Error("Run page not present");
    const heading = main.querySelector("h1");
    if (heading) heading.textContent = "只读故障调查 · 工单信息已遮盖";
    const header = heading?.closest("header") ?? heading?.parentElement?.parentElement;
    if (header) {
      for (const node of header.querySelectorAll("p, small")) {
        if (node.textContent?.includes("工单") || node.textContent?.includes("运行 ")) node.textContent = "运行与工单编号已遮盖";
      }
    }
    for (const node of main.querySelectorAll(".evidence-list article p, .evidence-list article footer span:first-child, .analysis-panel p, .review-verdict + p")) {
      node.textContent = "内容已遮盖，供公开文档展示";
    }
    const firstControlValue = main.querySelector(".detail-aside .fact-list dd");
    if (firstControlValue) firstControlValue.textContent = "已遮盖";
    for (const node of document.querySelectorAll("small, span, p")) {
      if (/租户\s+[0-9a-f-]{8,}/i.test(node.textContent ?? "")) node.textContent = "租户编号已遮盖";
    }
    for (const panel of main.querySelectorAll(".detail-main > .panel")) {
      if (panel.querySelector("h2")?.textContent === "执行轨迹" || panel.classList.contains("analysis-panel")) panel.remove();
    }
    const timeline = [...main.querySelectorAll(".detail-aside > .panel")].find((panel) => panel.querySelector("h2")?.textContent === "运行事件");
    timeline?.remove();
  });
  await page.screenshot({ path: "media/console-run-detail.png", fullPage: false });
  console.log("Captured real console run detail screenshot with public-documentation redactions");
} finally {
  await browser.close();
}
