import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
    excuses: (file, location) => needsNoKeyboardRoute(file, location),
  },
  {
    name: "query-result-rest",
    biome: "nursery/noTanstackQueryRestDestructuring",
    hint: "a rest element reads every field of a query result, so the component renders again when any field changes. Name the fields you use",
  },
];

const TYPESCRIPT_FILE = /\.tsx?$/;

const RESOURCE_EVENTS = new Set(["onLoad", "onError"]);
const RESOURCE_ELEMENTS = new Set(["img", "iframe"]);
const FOCUS_EVENTS = new Set(["onFocus", "onBlur", "onFocusCapture", "onBlurCapture"]);
const OPENING_TAG = /^<([A-Za-z][\w.]*)/;
const ATTRIBUTE_NAME = /(?:^|\s)(on[A-Z]\w*)\s*=/g;
const LITERAL = /"[^"]*"|'[^']*'|`[^`]*`/g;
const INNERMOST_EXPRESSION = /\{[^{}]*\}/g;
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

function openingTag(file, location) {
  const lines = readFileSync(file, "utf8").split("\n");
  const { start, end } = location;
  if (start.line === end.line) {
    return lines[start.line - 1].slice(start.column - 1, end.column - 1);
  }
  return [
    lines[start.line - 1].slice(start.column - 1),
    ...lines.slice(start.line, end.line - 1),
    lines[end.line - 1].slice(0, end.column - 1),
  ].join("\n");
}

function attributesOnly(tag) {
  let kept = tag.replace(LITERAL, "");
  for (let left = kept.replace(INNERMOST_EXPRESSION, ""); left !== kept; ) {
    kept = left;
    left = kept.replace(INNERMOST_EXPRESSION, "");
  }
  return kept;
}

function excused(element, handler) {
  if (RESOURCE_EVENTS.has(handler)) return RESOURCE_ELEMENTS.has(element);
  return FOCUS_EVENTS.has(handler);
}

function needsNoKeyboardRoute(file, location) {
  const tag = openingTag(file, location);
  const element = OPENING_TAG.exec(tag)?.[1];
  const handlers = [...attributesOnly(tag).matchAll(ATTRIBUTE_NAME)].map((found) => found[1]);
  if (element === undefined || handlers.length === 0) return false;
  return handlers.every((handler) => excused(element, handler));
}

export function biomeViolations(files) {
  const linted = files.filter((file) => TYPESCRIPT_FILE.test(file));
  if (linted.length === 0) return [];
  const asked = new Map(linted.map((file) => [resolve(file), file]));
  return diagnosticsOf(linted).flatMap((diagnostic) => {
    const check = checkOf(diagnostic);
    const file = asked.get(resolve(diagnostic.location.path));
    if (file === undefined) {
      fail(`biome reported ${diagnostic.location.path}, which conformance did not ask about.`);
    }
    if (check.excuses?.(file, diagnostic.location)) return [];
    return {
      file,
      line: diagnostic.location.start.line,
      rule: check.name,
      text: diagnostic.message.trim(),
      hint: check.hint,
    };
  });
}
