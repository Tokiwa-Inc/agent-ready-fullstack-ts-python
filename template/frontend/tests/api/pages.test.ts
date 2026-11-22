import { expect, test } from "vitest";
import { API_BASE_URL } from "@/api/base";
import { PAGES } from "@/api/parts";
import { pageAtMost } from "@/mocks/pages";
import { pagePaths } from "../read-budgets.ts";

const NAMED = /\{[^}]+\}/;

function pathOf(page: string): string {
  const path = NAMED.test(page) ? pagePaths[page] : page;
  if (path === undefined) {
    throw new Error(`${page} names a parameter: give it a path to read in pagePaths`);
  }
  return path;
}

test("pagePaths names a path for each page that takes a parameter, and for nothing else", () => {
  expect(Object.keys(pagePaths).sort()).toEqual(PAGES.filter((page) => NAMED.test(page)).sort());
});

test.each(PAGES)(
  "the mock of %s answers one row a page when a page holds one row",
  async (page) => {
    pageAtMost(1);

    const path = pathOf(page);
    const answer: Record<string, unknown> = await (await fetch(`${API_BASE_URL}${path}`)).json();

    const rows = Object.values(answer).filter((value) => Array.isArray(value));
    expect(rows, `${path} answered no list`).toHaveLength(1);
    expect(rows[0], `the mock of ${path} does not page: read pageRows() in it`).toHaveLength(1);
    expect(
      answer.next,
      `the mock of ${path} named no next page: seed it with two rows or more, or read pageRows() in it`,
    ).not.toBeNull();
  },
);
