export const PAGE_MAX = 100;

let rowsPerPage = PAGE_MAX;

export function pageRows(): number {
  return rowsPerPage;
}

export function pageAtMost(rows: number): void {
  rowsPerPage = rows;
}
