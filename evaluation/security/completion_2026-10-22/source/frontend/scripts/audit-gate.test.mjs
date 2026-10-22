import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const gate = fileURLToPath(new URL('./audit-gate.mjs', import.meta.url));
const clean = { auditReportVersion: 2, vulnerabilities: {}, metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: 0 } } };
for (const [label, report, exit, expected] of [
  ['normal clean scan', clean, 0, 0],
  ['scanner error JSON', { error: { code: 'ENOAUDIT' } }, 1, 1],
  ['empty object', {}, 0, 1],
  ['invalid JSON', 'not-json', 0, 1],
  ['scanner abnormal exit', clean, 2, 1],
  ['nonzero with zero findings', clean, 1, 1],
  ['missing advisory structure', { ...clean, vulnerabilities: { x: { severity: 'high' } } }, 1, 1],
  ['high finding with no advisory', { ...clean, metadata: { vulnerabilities: { ...clean.metadata.vulnerabilities, high: 1, total: 1 } }, vulnerabilities: { x: { severity: 'high', via: [] } } }, 1, 1],
  ['known high advisory', { ...clean, metadata: { vulnerabilities: { ...clean.metadata.vulnerabilities, high: 1, total: 1 } }, vulnerabilities: { x: { severity: 'high', via: [{ name: 'fixture', severity: 'high', url: 'https://example.invalid/SECURITY-FIXTURE', title: 'fixture' }] } } }, 1, 1],
]) {
  test(label, () => {
    const dir = mkdtempSync(join(tmpdir(), 'audit-gate-'));
    try {
      writeFileSync(join(dir, 'npm'), '#!/bin/sh\nprintf "%s" "$AUDIT_FIXTURE"\nexit "$AUDIT_EXIT"\n', { mode: 0o700 });
      const result = spawnSync(process.execPath, [gate], { encoding: 'utf8', env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, AUDIT_FIXTURE: typeof report === 'string' ? report : JSON.stringify(report), AUDIT_EXIT: String(exit) } });
      assert.equal(result.status, expected, result.stdout + result.stderr);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
}

const advisory = { ...clean, metadata: { vulnerabilities: { ...clean.metadata.vulnerabilities, high: 1, total: 1 } }, vulnerabilities: { braces: { severity: 'high', via: [{ name: 'braces', severity: 'high', url: 'https://example.invalid/FIXTURE', title: 'fixture' }] } } };
for (const [label, exceptions, expected] of [
  ['valid exception', [{ id: 'FIXTURE', statement: 'test only', expired_at: '2026-10-31' }], 0],
  ['expired exception', [{ id: 'FIXTURE', statement: 'test only', expired_at: '2026-10-21' }], 1],
  ['missing expiry', [{ id: 'FIXTURE', statement: 'test only' }], 1],
  ['invalid calendar date', [{ id: 'FIXTURE', statement: 'test only', expired_at: '2026-02-30' }], 1],
  ['duplicate exception', [1, 2].map(() => ({ id: 'FIXTURE', statement: 'test only', expired_at: '2026-10-31' })), 1],
]) {
  test(label, () => {
    const dir = mkdtempSync(join(tmpdir(), 'audit-exception-'));
    try {
      writeFileSync(join(dir, 'gate.mjs'), readFileSync(gate, 'utf8').replace('".."', '"."'));
      writeFileSync(join(dir, '.npmauditignore.json'), JSON.stringify({ advisories: exceptions }));
      writeFileSync(join(dir, 'clock.mjs'), 'const Base = Date; globalThis.Date = class extends Base { constructor(...args) { super(...(args.length ? args : ["2026-10-22T12:00:00Z"])); } };');
      writeFileSync(join(dir, 'npm'), '#!/bin/sh\nprintf "%s" "$AUDIT_FIXTURE"\nexit 1\n', { mode: 0o700 });
      const result = spawnSync(process.execPath, ['--import', join(dir, 'clock.mjs'), join(dir, 'gate.mjs')], { encoding: 'utf8', env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, AUDIT_FIXTURE: JSON.stringify(advisory) } });
      assert.equal(result.status, expected, result.stdout + result.stderr);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
}
