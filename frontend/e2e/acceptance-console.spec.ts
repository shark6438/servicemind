/**
 * The Phase 7.6 browser probe: one real operator, one real console, one real run.
 *
 * This exists because no amount of calling the API proves the console works. Every other
 * case in the acceptance drives HTTP directly, and a frontend that renders nothing, or
 * that reads a field the API no longer sends, would leave all of them passing while the
 * only surface a human actually uses is broken. So the claim here is deliberately narrow
 * and entirely about the browser: the console authenticates, submits a run through its own
 * form, follows it to its own detail page, and shows the run the platform actually
 * created.
 *
 * Credentials arrive by environment variable *name*, not by value, so this file never
 * carries a secret and the runner decides which subject to use. Both the value and its
 * variable name are supplied by the driver, which reads them from ``deploy/glpi/.env``.
 */

import { expect, test, type Page } from "@playwright/test";

const SUBJECT = process.env.SERVICEMIND_ACCEPTANCE_SUBJECT ?? "";
const PASSWORD_VAR = process.env.SERVICEMIND_ACCEPTANCE_PASSWORD_VAR ?? "";
const PASSWORD = PASSWORD_VAR ? process.env[PASSWORD_VAR] : undefined;
const TICKET_ID = process.env.SERVICEMIND_ACCEPTANCE_TICKET ?? "";
const MODE = process.env.SERVICEMIND_ACCEPTANCE_MODE ?? "login";

const GOAL = "请在不修改工单的前提下，说明该工单的 VPN 多因素认证失败原因。";

/**
 * Sign in through the console's own button, which hands off to Keycloak.
 *
 * The same flow a person performs: nothing here reaches the API directly, because a probe
 * that minted its own token would skip the part of the console that is most likely to be
 * wrong.
 */
async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: "使用企业身份登录" }).click();
  await page.getByLabel(/username or email/i).fill(SUBJECT);
  await page.getByRole("textbox", { name: "Password", exact: true }).fill(PASSWORD as string);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
}

test.beforeAll(() => {
  test.skip(!PASSWORD, `${PASSWORD_VAR || "the password variable"} is required`);
  test.skip(!SUBJECT, "SERVICEMIND_ACCEPTANCE_SUBJECT is required");
  test.skip(!TICKET_ID, "SERVICEMIND_ACCEPTANCE_TICKET is required");
});

test("the console authenticates an operator and shows the workbench", async ({ page }) => {
  test.skip(MODE !== "login", "this run is the submit probe");
  await signIn(page);
  await expect(page.getByRole("heading", { name: "向运维智能体提问" })).toBeVisible();

  // The controlled-environment marker and the link to the quality page live in the
  // topbar, which the stylesheet hides below 780px (``.topbar { display: none }``)
  // without restating either in the mobile header that replaces it. So at a phone
  // width the console shows an operator neither the marker nor the route to the
  // release status -- a real property of the rendered console, recorded here and in
  // the report as an observation rather than skipped past.
  //
  // Asserted in both directions so that neither half can drift unnoticed: the desktop
  // assertion fails if the marker disappears, and the mobile one fails if someone
  // restores it there.
  const marker = page.getByText("受控环境");
  if (test.info().project.name === "mobile") {
    await expect(marker).toBeHidden();
  } else {
    await expect(marker).toBeVisible();
  }
});

test("an operator submits a run in the console and sees it reach a terminal state", async ({
  page,
}) => {
  test.skip(MODE !== "submit", "this run is the login probe");

  // This probe waits on a real run driven by a real model, which is slower than anything
  // else in this suite; the configured 45s would kill the test mid-run, and a test-level
  // timeout supersedes the assertion's own budget -- so the 300s below would never be
  // reached and the failure would report the harness's patience rather than the run's
  // progress. The budget has to cover the thing being measured.
  test.setTimeout(360_000);
  await signIn(page);

  await page.getByRole("textbox", { name: "你的问题" }).fill(GOAL);
  await page.getByRole("spinbutton", { name: "关联 GLPI 工单" }).fill(TICKET_ID);
  await page.getByRole("button", { name: "发送给智能体" }).click();

  // The console routes to the new run's own page, so the URL is the first thing that says
  // a run was created rather than merely submitted.
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}/, { timeout: 30_000 });
  const runId = page.url().split("/").pop();
  expect(runId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);

  // The detail page's title is the goal that was typed, so seeing it means the page is
  // rendering the run the platform stored and not a locally-invented placeholder.
  await expect(page.getByRole("heading", { name: GOAL })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(`运行 ${runId?.slice(0, 8)}`)).toBeVisible();

  // Read-only, so it must come to rest at 已完成 without any human step. Polling here
  // rather than asserting immediately is the point: a probe that only checked the page
  // had rendered would pass against a console backed by a run that never progressed.
  const badge = page.locator(".status");
  await expect(badge).toContainText("已完成", { timeout: 300_000 });

  await expect(page.getByRole("heading", { name: "证据记录" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "分析与建议" })).toBeVisible();
});
