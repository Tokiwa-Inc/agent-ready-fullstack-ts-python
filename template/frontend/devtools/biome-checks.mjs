import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fail } from "./gate.mjs";

export const BIOME_CHECKS = [
  {
    name: "react-compiler",
    biome: "nursery/useReactCompiler",
    hint: "React renders a component again whenever it must, so a render that does more than compute is wrong some of the time. Compute a value in the render, and write state in the event that causes it",
  },
  {
    name: "handler-on-a-non-interactive-element",
    biome: "a11y/noNoninteractiveElementInteractions",
    hint: "a keyboard cannot reach a handler on a list item, a paragraph or a landmark. Put the handler on a button or a link, or on a primitive that is one",
  },
  {
    name: "query-result-rest",
    biome: "nursery/noTanstackQueryRestDestructuring",
    hint: "a rest element reads every field of a query result, so the component renders again when any field changes. Name the fields you use",
  },
];

const TYPESCRIPT_FILE = /\.tsx?$/;
const REPORT_BYTES = 256 * 1024 * 1024;

function checkConfig(directory) {
  const path = join(directory, "biome.json");
  writeFileSync(
    path,
    JSON.stringify({
      files: { includes: ["**"] },
      formatter: { enabled: false },
      assist: { enabled: false },
      linter: { enabled: true, rules: { preset: "none" } },
    }),
  );
  return path;
}

function diagnosticsIn(stdout, notAReport) {
  let report;
  try {
    report = JSON.parse(stdout);
  } catch (error) {
    if (error instanceof SyntaxError) return fail(notAReport);
    throw error;
  }
  if (!Array.isArray(report?.diagnostics)) return fail(notAReport);
  return report.diagnostics;
}

function fault(error) {
  if (error.code === "ENOENT") return "biome was not found on PATH; run this through pnpm.";
  const failed = `biome failed (exit ${error.status}): ${String(error.stderr).trim()}`;
  const first = diagnosticsIn(error.stdout, failed).find((one) => one.severity === "error");
  if (first === undefined) return failed;
  return `biome could not check ${first.location.path}: ${first.message.trim()}`;
}

function runBiome(files) {
  const directory = mkdtempSync(join(tmpdir(), "conformance-biome-"));
  const only = BIOME_CHECKS.map((check) => `--only=${check.biome}`);
  const args = [
    "lint",
    "--config-path",
    checkConfig(directory),
    "--reporter=json",
    "--max-diagnostics=none",
  ];
  let failed;
  try {
    return execFileSync("biome", [...args, ...only, ...files], {
      encoding: "utf8",
      maxBuffer: REPORT_BYTES,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    failed = error;
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
  return fail(fault(failed));
}

function diagnosticsOf(files) {
  const stdout = runBiome(files);
  return diagnosticsIn(stdout, `biome answered something other than its JSON report: ${stdout}`);
}

function checkOf(diagnostic) {
  const check = BIOME_CHECKS.find((one) => diagnostic.category === `lint/${one.biome}`);
  if (check === undefined) {
    fail(
      `biome reported ${diagnostic.category} at ${diagnostic.location.path}, ` +
        `which no conformance check names: ${diagnostic.message.trim()}`,
    );
  }
  return check;
}

export function biomeViolations(files) {
  const linted = files.filter((file) => TYPESCRIPT_FILE.test(file));
  if (linted.length === 0) return [];
  const asked = new Map(linted.map((file) => [resolve(file), file]));
  return diagnosticsOf(linted).map((diagnostic) => {
    const check = checkOf(diagnostic);
    const file = asked.get(resolve(diagnostic.location.path));
    if (file === undefined) {
      fail(`biome reported ${diagnostic.location.path}, which conformance did not ask about.`);
    }
    return {
      file,
      line: diagnostic.location.start.line,
      rule: check.name,
      text: diagnostic.message.trim(),
      hint: check.hint,
    };
  });
}
