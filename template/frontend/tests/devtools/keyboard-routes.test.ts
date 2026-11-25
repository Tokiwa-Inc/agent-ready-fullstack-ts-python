import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { gate, NO_PROJECT_RULES } from "./harness";

const conformance = gate("conformance.mjs", "conformance");

function scan(name: string, lines: string[]) {
  const { status, lines: reported } = conformance.run(name, lines, NO_PROJECT_RULES);
  return { status, rules: reported.map((line) => line.split(/\s{2,}/)[1]) };
}

const HANDLER_RULE = "handler-on-a-non-interactive-element";

function handledBy(markup: string) {
  return [
    "export function Handled({ f }: { f: () => void }) {",
    `  return (\n    ${markup}\n  );`,
    "}",
  ];
}

describe("the conformance gate on a handler that needs no keyboard route", () => {
  beforeEach(conformance.open);

  afterEach(conformance.close);

  it.each([
    ["a resource event on an image", "<img src='a.png' alt='' onLoad={f} />"],
    ["a resource event on a frame", "<iframe src='a.html' title='a' onError={f} />"],
    ["a focus handler on any element", "<div onBlur={f}>a</div>"],
    [
      "focus handlers across lines",
      "<section\n      onFocusCapture={f}\n      onBlur={f}\n    >\n      a\n    </section>",
    ],
    ["a submit and a focus handler on a form", "<form onSubmit={f} onBlur={f}>a</form>"],
    [
      "a handler name inside an attribute's value",
      "<div onBlur={() => { const onClick = f; onClick(); }} title='a onKeyDown = x'>a</div>",
    ],
  ])("lets %s pass", (_case, markup) => {
    const { rules } = scan("handled.tsx", handledBy(markup));
    expect(rules).not.toContain(HANDLER_RULE);
  });

  it.each([
    ["a click on a paragraph", "<p onClick={f}>a</p>"],
    ["a key handler on a container", "<form onKeyDown={f}>a</form>"],
    ["a click beside a focus handler", "<div onBlur={f} onClick={f}>a</div>"],
    ["a resource event on a paragraph", "<p onLoad={f}>a</p>"],
    [
      "a click after an escaped quote",
      "<i><b onBlur={f} title={'it\\'s'} /><p onClick={f}>a</p></i>",
    ],
    [
      "a click after a regular expression",
      "<i><b onBlur={() => /'/.test('a')} /><p onClick={f}>a</p></i>",
    ],
    [
      "a click after emoji on its line",
      "<i>😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀<b onBlur={f} /><p onClick={f}>a</p></i>",
    ],
  ])("reports %s", (_case, markup) => {
    const { status, rules } = scan("handled.tsx", handledBy(markup));
    expect(rules).toContain(HANDLER_RULE);
    expect(status).toBe(1);
  });

  it("reports the one element of a line that fails, and lets the other pass", () => {
    const run = conformance.run(
      "handled.tsx",
      handledBy("<i><b onBlur={f} /><p onClick={f}>a</p></i>"),
      NO_PROJECT_RULES,
    );
    expect(run.lines.filter((line) => line.includes(HANDLER_RULE))).toHaveLength(1);
    expect(run.status).toBe(1);
  });
});
