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
      "<section\n      onFocus={f}\n      onBlur={f}\n    >\n      a\n    </section>",
    ],
    ["a hover handler beside its focus twin", "<label onMouseEnter={f} onFocus={f}>a</label>"],
    ["a hover handler beside a blur", "<li onMouseOver={f} onMouseOut={f} onBlur={f}>a</li>"],
    [
      "a handler Biome does not read beside a focus handler",
      "<div onScroll={f} onBlur={f}>a</div>",
    ],
    ["a focus handler after emoji and an accent on its line", "<i>😀😀😀😀é<b onBlur={f} /></i>"],
    [
      "a handler name inside an attribute's value",
      "<div onBlur={() => { const onClick = f; onClick(); }} title='a onKeyDown = x'>a</div>",
    ],
  ])("lets %s pass", (_case, markup) => {
    const { rules } = scan("handled.tsx", handledBy(markup));
    expect(rules).not.toContain(HANDLER_RULE);
  });
});

describe("the conformance gate on a handler a keyboard cannot reach", () => {
  beforeEach(conformance.open);

  afterEach(conformance.close);

  it.each([
    ["a click on a paragraph", "<p onClick={f}>a</p>"],
    ["a key handler on a container", "<form onKeyDown={f}>a</form>"],
    ["a click beside a focus handler", "<div onBlur={f} onClick={f}>a</div>"],
    ["a hover handler with no focus twin", "<div onMouseEnter={f}>a</div>"],
    ["a mouse move beside a focus handler", "<div onMouseMove={f} onFocus={f}>a</div>"],
    ["a resource event on a paragraph", "<p onLoad={f}>a</p>"],
    ["a spread beside a focus handler", "<div onBlur={f} {...{ onClick: f }}>a</div>"],
    [
      "a click after an escaped quote",
      "<i><b onBlur={f} title={'it\\'s'} /><p onClick={f}>a</p></i>",
    ],
    [
      "a click after a regular expression",
      "<i><b onBlur={() => /'/.test('a')} /><p onClick={f}>a</p></i>",
    ],
    [
      "a click after a lone carriage return",
      "<i>\n      a\rb\n      <p onClick={f}>a</p>\n      <button onBlur={f}>a</button>\n    </i>",
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

describe("the handlers the conformance gate reads", () => {
  beforeEach(conformance.open);

  afterEach(conformance.close);

  const FAILING = [
    ...["onLoad", "onError", "onKeyDown", "onKeyPress", "onKeyUp", "onClick", "onContextMenu"],
    ...["onDblClick", "onDoubleClick", "onDrag", "onDragEnd", "onDragEnter", "onDragExit"],
    ...["onDragLeave", "onDragOver", "onDragStart", "onDrop", "onMouseDown", "onMouseMove"],
    "onMouseUp",
  ];
  const HOVER = ["onMouseEnter", "onMouseLeave", "onMouseOver", "onMouseOut"];
  const FOCUS = ["onFocus", "onBlur"];
  const UNREAD = [
    ...["onScroll", "onWheel", "onChange", "onInput", "onSubmit", "onReset", "onAuxClick"],
    ...["onPointerDown", "onPointerUp", "onPointerMove", "onPointerEnter", "onPointerLeave"],
    ...["onPointerOver", "onPointerOut", "onPointerCancel", "onTouchStart", "onTouchEnd"],
    ...["onTouchMove", "onTouchCancel", "onFocusCapture", "onBlurCapture", "onClickCapture"],
    ...["onKeyDownCapture", "onCopy", "onCut", "onPaste", "onSelect", "onTransitionEnd"],
  ];

  it("are the ones Biome reads, alone and beside a focus handler", () => {
    const names = [...FAILING, ...HOVER, ...FOCUS, ...UNREAD].flatMap((name) => [name, name]);
    const elements = names.map((name, at) =>
      at % 2 === 0 ? `<p ${name}={f}>a</p>` : `<p onBlur={f} ${name}={f}>a</p>`,
    );
    const markup = `<>\n${elements.map((one) => `      ${one}`).join("\n")}\n    </>`;
    const run = conformance.run("handled.tsx", handledBy(markup), NO_PROJECT_RULES);
    const lineOf = (line: string) => Number(line.slice(run.path.length + 1).split(/\s/)[0]);
    const reported = run.lines.map((line) => names[lineOf(line) - 4]);
    expect(reported.sort()).toEqual([...FAILING, ...FAILING, ...HOVER].sort());
  });
});
