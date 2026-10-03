#!/usr/bin/env node
/**
 * Dependency gate for the operator console.
 *
 * `npm audit` is the gate. This wrapper exists for exactly one reason: an advisory
 * that has no released fix cannot be cleared by changing a dependency, so a bare
 * `npm audit --audit-level=high` would fail on every push -- including pushes that
 * touch nothing in frontend/. Suppressions live in .npmauditignore.json, must name
 * an advisory id, and carry an expiry date; past that date the finding is reported
 * again and the gate fails. That is the same contract as .trivyignore.yaml, and for
 * the same reason: an exception should be visible in the log and should expire on
 * its own rather than silently turning the gate off.
 *
 * Coverage is NOT narrowed: the whole installed tree is audited, dev dependencies
 * included. Only the named advisory is excused, and only until it expires.
 *
 * Exit codes: 0 = no unsuppressed high/critical advisory; 1 = at least one, or the
 * audit itself could not be read (an unreadable audit must not read as green).
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const GATING = new Set(["high", "critical"]);
const root = join(dirname(fileURLToPath(import.meta.url)), "..");

function suppressionMap() {
  const spec = JSON.parse(readFileSync(join(root, ".npmauditignore.json"), "utf8"));
  const today = new Date().toISOString().slice(0, 10);
  const live = new Map();
  for (const entry of spec.advisories ?? []) {
    // ISO dates compare lexicographically, so no Date parsing is needed here.
    if (entry.expired_at < today) {
      console.log(`[expired] ${entry.id} -- suppressed until ${entry.expired_at}; enforced again as of ${today}.`);
    } else {
      live.set(entry.id, { ...entry, used: false });
    }
  }
  return live;
}

const run = spawnSync("npm", ["audit", "--json"], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
let report;
try {
  report = JSON.parse(run.stdout);
} catch {
  console.error("npm audit produced no readable JSON report; refusing to report a pass.");
  console.error(run.stderr || run.stdout || "(no output)");
  process.exit(1);
}

const suppressions = suppressionMap();
const advisories = new Map();
for (const pkg of Object.values(report.vulnerabilities ?? {})) {
  for (const via of pkg.via ?? []) {
    if (typeof via !== "object" || via === null) continue; // a string names a vulnerable dependency, already counted itself
    const id = (via.url ?? "").split("/").pop() || `npm-${via.source}`;
    if (advisories.has(id)) continue;
    advisories.set(id, { id, package: via.name, severity: via.severity, title: via.title, url: via.url });
  }
}

const blocking = [];
for (const advisory of advisories.values()) {
  const entry = suppressions.get(advisory.id);
  if (entry && GATING.has(advisory.severity)) {
    entry.used = true;
    console.log(`[suppressed] ${advisory.id} ${advisory.package} (${advisory.severity}) -- ${entry.statement}`);
    console.log(`             expires ${entry.expired_at}`);
    continue;
  }
  if (GATING.has(advisory.severity)) blocking.push(advisory);
}

for (const entry of suppressions.values()) {
  if (!entry.used) console.log(`[unused] ${entry.id} is suppressed but no longer reported -- delete it from .npmauditignore.json.`);
}

console.log(`audit totals: ${JSON.stringify(report.metadata?.vulnerabilities ?? {})}`);
for (const advisory of blocking) {
  console.error(`[blocking] ${advisory.id} ${advisory.package} (${advisory.severity}): ${advisory.title} -- ${advisory.url}`);
}
if (blocking.length > 0) {
  console.error(`\n${blocking.length} high/critical advisor${blocking.length === 1 ? "y" : "ies"} not covered by .npmauditignore.json.`);
  process.exit(1);
}
console.log("no unsuppressed high/critical advisories.");
