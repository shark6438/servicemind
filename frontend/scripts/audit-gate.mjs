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
  const declared = new Set();
  for (const entry of spec.advisories ?? []) {
    if (!entry || typeof entry.id !== "string" || !entry.id ||
        typeof entry.statement !== "string" || !entry.statement ||
        !/^\d{4}-\d{2}-\d{2}$/.test(entry.expired_at ?? "") ||
        !Number.isFinite(Date.parse(`${entry.expired_at}T00:00:00Z`)) ||
        new Date(`${entry.expired_at}T00:00:00Z`).toISOString().slice(0, 10) !== entry.expired_at ||
        declared.has(entry.id)) {
      throw new Error("Invalid or duplicate dependency audit exception");
    }
    declared.add(entry.id);
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
if (run.error || run.signal || ![0, 1].includes(run.status)) {
  console.error("npm audit did not complete successfully; refusing to report a pass.");
  process.exit(1);
}
let report;
try {
  report = JSON.parse(run.stdout);
} catch {
  console.error("npm audit produced no readable JSON report; refusing to report a pass.");
  console.error(run.stderr || run.stdout || "(no output)");
  process.exit(1);
}

if (
  !report || report.error || report.auditReportVersion !== 2 ||
  !report.metadata?.vulnerabilities || !report.vulnerabilities ||
  !["info", "low", "moderate", "high", "critical", "total"].every(
    (key) => Number.isInteger(report.metadata.vulnerabilities[key]) && report.metadata.vulnerabilities[key] >= 0
  ) ||
  Object.values(report.vulnerabilities).some(
    (pkg) => !pkg || !Array.isArray(pkg.via) || !["info", "low", "moderate", "high", "critical"].includes(pkg.severity)
  ) ||
  (run.status === 1 && report.metadata.vulnerabilities.total === 0)
  || (report.metadata.vulnerabilities.total !== Object.keys(report.vulnerabilities).length)
  || !["info", "low", "moderate", "high", "critical"].every(
    (severity) => Object.values(report.vulnerabilities).filter(pkg => pkg.severity === severity).length === report.metadata.vulnerabilities[severity]
  )
) {
  console.error("npm audit returned an error or incomplete report; refusing to report a pass.");
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
function hasGatingAdvisory(name, visiting = new Set()) {
  if (visiting.has(name)) return false;
  visiting.add(name);
  return (report.vulnerabilities[name]?.via ?? []).some(via =>
    typeof via === "string" ? hasGatingAdvisory(via, new Set(visiting)) : GATING.has(via.severity)
  );
}
for (const [name, pkg] of Object.entries(report.vulnerabilities)) {
  if (GATING.has(pkg.severity) && !hasGatingAdvisory(name)) {
    console.error(`Incomplete dependency audit graph for ${name}; refusing to report a pass.`);
    process.exit(1);
  }
}
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
