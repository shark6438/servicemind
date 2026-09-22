import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("authenticated operator console is usable and has no serious accessibility violations", async ({ page }, testInfo) => {
  const password = process.env.ACME_APPROVER_PASSWORD;
  test.skip(!password, "ACME_APPROVER_PASSWORD is required for the live identity flow");

  await page.goto("/");
  await page.getByRole("button", { name: "使用企业身份登录" }).click();
  await page.getByLabel(/username or email/i).fill("acme-approver");
  await page.getByRole("textbox", { name: "Password", exact: true }).fill(password as string);
  await page.getByRole("button", { name: /sign in/i }).click();

  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "向运维智能体提问" })).toBeVisible();
  const prompt = page.getByRole("textbox", { name: "你的问题" });
  await expect(prompt).toBeVisible();
  await prompt.fill("分析该工单的 VPN 连接失败原因，并给出已验证的处置建议。");
  await expect(prompt).toHaveValue("分析该工单的 VPN 连接失败原因，并给出已验证的处置建议。");
  await expect(page.getByRole("spinbutton", { name: "关联 GLPI 工单" })).toBeVisible();
  if (testInfo.project.name === "desktop") {
    await expect(page.getByText("受控环境")).toBeVisible();
  } else {
    await expect(page.getByRole("link", { name: "ServiceMind" })).toBeVisible();
  }
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations.filter((item) => ["serious", "critical"].includes(item.impact ?? ""))).toEqual([]);

  await page.screenshot({ path: testInfo.outputPath("workbench.png"), fullPage: true });
  if (testInfo.project.name === "mobile") {
    await page.getByRole("button", { name: "打开导航" }).click();
  }
  await page.getByRole("link", { name: "质量与发布", exact: true }).click();
  await expect(page.getByRole("heading", { name: "质量与发布状态" })).toBeVisible();
  await expect(page.getByText("领域质量尚未认证")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("quality-gates.png"), fullPage: true });
});
