import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
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
    passes: needsNoKeyboardRoute,
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
const FORM_EVENTS = new Set(["onSubmit", "onReset"]);
const HOVER_EVENTS = new Set(["onPointerEnter", "onPointerLeave", "onMouseEnter", "onMouseLeave"]);
const HANDLER_NAME = /^on[A-Z]/;
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

function offsetOf(code, { line, column }) {
  const lines = code.split("\n");
  const before = lines.slice(0, line - 1).reduce((sum, one) => sum + one.length + 1, 0);
  return (
    before +
    Array.from(lines[line - 1])
      .slice(0, column - 1)
      .join("").length
  );
}

function openingElementAt(node, offset) {
  if (node === null || typeof node !== "object") return null;
  if (node.type === "JSXOpeningElement" && node.start === offset) return node;
  for (const value of Object.values(node)) {
    const found = openingElementAt(value, offset);
    if (found !== null) return found;
  }
  return null;
}

function readTree(parseAst, file) {
  let code;
  try {
    code = readFileSync(file, "utf8");
  } catch (error) {
    return fail(`conformance could not read ${file}: ${error.message}`);
  }
  try {
    return { code, tree: parseAst(code, { lang: "tsx" }) };
  } catch (error) {
    return fail(`conformance could not parse ${file}: ${error.message}`);
  }
}

function treesOf(parseAst) {
  const held = new Map();
  return (file) => {
    if (!held.has(file)) held.set(file, readTree(parseAst, file));
    return held.get(file);
  };
}

function handlerPasses(element, handler, handlers) {
  if (FOCUS_EVENTS.has(handler)) return true;
  if (HOVER_EVENTS.has(handler)) return handlers.some((one) => FOCUS_EVENTS.has(one));
  if (FORM_EVENTS.has(handler)) return element === "form";
  return RESOURCE_EVENTS.has(handler) && RESOURCE_ELEMENTS.has(element);
}

function needsNoKeyboardRoute(read, file, location) {
  const { code, tree } = read(file);
  const opening = openingElementAt(tree, offsetOf(code, location.start));
  if (opening === null || opening.name.type !== "JSXIdentifier") return false;
  const handlers = opening.attributes
    .filter((one) => one.type === "JSXAttribute" && one.name.type === "JSXIdentifier")
    .map((one) => one.name.name)
    .filter((name) => HANDLER_NAME.test(name));
  const element = opening.name.name;
  return handlers.length > 0 && handlers.every((one) => handlerPasses(element, one, handlers));
}

function readerFor(diagnostics) {
  if (!diagnostics.some((one) => checkOf(one).passes !== undefined)) return null;
  return treesOf(createRequire(import.meta.url)("vite").parseAst);
}

export function biomeViolations(files) {
  const linted = files.filter((file) => TYPESCRIPT_FILE.test(file));
  if (linted.length === 0) return [];
  const asked = new Map(linted.map((file) => [resolve(file), file]));
  const diagnostics = diagnosticsOf(linted);
  const read = readerFor(diagnostics);
  return diagnostics.flatMap((diagnostic) => {
    const check = checkOf(diagnostic);
    const file = asked.get(resolve(diagnostic.location.path));
    if (file === undefined) {
      fail(`biome reported ${diagnostic.location.path}, which conformance did not ask about.`);
    }
    if (check.passes?.(read, file, diagnostic.location)) return [];
    return {
      file,
      line: diagnostic.location.start.line,
      rule: check.name,
      text: diagnostic.message.trim(),
      hint: check.hint,
    };
  });
}
