import { expect, test } from '@playwright/test';

const runId = '77777777-7777-4777-8777-777777777777';
const payload = '<img src="https://security-sink.invalid/leak" onerror="window.__securityExecuted=1"><script>window.__securityExecuted=2</script><a href="javascript:window.__securityExecuted=3">click</a>';

test('untrusted run, approval and memory content stays text; tokens stay out of storage and URLs', async ({ page }) => {
  const password = process.env.ACME_APPROVER_PASSWORD;
  expect(password, 'real fixture identity credentials must be supplied').toBeTruthy();
  const sinkRequests: string[] = [];
  const tokenUrls: string[] = [];
  page.on('request', request => {
    if (request.url().includes('security-sink.invalid')) sinkRequests.push(request.url());
    if (/[?&](access_token|refresh_token|id_token)=/.test(request.url())) tokenUrls.push(request.url());
  });
  await page.goto('/');
  await page.getByRole('button', { name: '使用企业身份登录' }).click();
  await page.getByLabel(/username or email/i).fill('acme-approver');
  await page.getByRole('textbox', { name: 'Password', exact: true }).fill(password!);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible();
  const now = new Date().toISOString();
  await page.route(`**/v1/servicemind/runs/${runId}*`, async route => {
    const timeline = new URL(route.request().url()).pathname.endsWith('/timeline');
    await route.fulfill({ json: timeline ? [] : {
      id: runId, ticket_id: 1, goal: payload, request_write: true, status: 'waiting_approval',
      created_at: now, updated_at: now, tenant_id: '11111111-1111-4111-8111-111111111111',
      user_id: 'security-fixture', error: null,
      result: { analysis: { summary: payload, root_cause: payload, followup: payload, problem_recommendation: payload, change_recommendation: payload } },
      action_intent: { id: runId, action_type: 'append_followup', target_id: 1, arguments: { content: payload }, risk_level: 'high', requires_approval: true, action_hash: 'a'.repeat(64), status: 'pending', intent_version: 'v1', policy_version: 'v1', review_digest: null, evidence_digest: null, evidence_refs: [], expires_at: null, dry_run_preview: payload },
    } });
  });
  await page.goto(`/runs/${runId}`);
  await expect(page.getByRole('heading', { name: payload, exact: true })).toBeVisible();
  await expect(page.getByText(payload, { exact: true }).first()).toBeVisible();
  expect(await page.locator('img[src*="security-sink"], a[href^="javascript:"]').count()).toBe(0);
  await page.route('**/v1/servicemind/memories/review-queue*', route => route.fulfill({ json: { items: [{ memory_id: runId, memory_type: 'procedural', subject_key: 'security fixture', content: payload, content_hash: 'b'.repeat(64), version: 1, status: 'quarantined', confidence: 0.9, importance: 0.9, evidence_refs: [], supporting_episode_ids: [], provenance: {}, created_at: now }] } }));
  await page.goto('/memory');
  await expect(page.getByText(payload, { exact: true }).first()).toBeVisible();
  expect(await page.locator('img[src*="security-sink"], a[href^="javascript:"]').count()).toBe(0);
  const observed = await page.evaluate(() => ({
    executed: (window as unknown as { __securityExecuted?: number }).__securityExecuted ?? 0,
    jwtInStorage: [localStorage, sessionStorage].some(storage => Object.values(storage).some(value => /eyJ[A-Za-z0-9_-]+\.eyJ/.test(String(value)))),
  }));
  expect(observed).toEqual({ executed: 0, jwtInStorage: false });
  expect(sinkRequests).toEqual([]);
  expect(tokenUrls).toEqual([]);
});
