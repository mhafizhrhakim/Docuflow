import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../frontend/src/js/extraction-table.js", import.meta.url), "utf8");
const extractionTable = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const { filterTableRows, selectedTableRows, serializeTablesForCopy, serializeTablesForCsv } = extractionTable;
const progressSource = await readFile(new URL("../frontend/src/js/progress.js", import.meta.url), "utf8");
const progressModule = await import(`data:text/javascript;base64,${Buffer.from(progressSource).toString("base64")}`);
const apiSource = await readFile(new URL("../frontend/src/js/api.js", import.meta.url), "utf8");
const apiModule = await import(`data:text/javascript;base64,${Buffer.from(apiSource).toString("base64")}`);
const concurrencySource = await readFile(new URL("../frontend/src/js/concurrency.js", import.meta.url), "utf8");
const concurrencyModule = await import(`data:text/javascript;base64,${Buffer.from(concurrencySource).toString("base64")}`);

const rows = [
  ["1001", "Stainless steel pipe", "EA", "2", "10", "20"],
  ["1002", "Carbon steel flange", "PCS", "1", "15", "15"],
  ["1003", "Rubber gasket", "SET", "4", "2.5", "10"],
];

test("aggregate progress uses reported work without claiming pages finished", () => {
  const progress = progressModule.aggregatePageProgress([
    { total_pages: 10, completed_pages: 0, progress_percent: 50 },
    { total_pages: 10, completed_pages: 0, progress_percent: 0 },
  ]);
  assert.equal(progress.percentage, 25);
  assert.equal(progress.completed_pages, 0);
  assert.equal(progress.total_pages, 20);
  assert.equal(progress.progress_mode, "processing_work");
});

test("search matches item numbers and descriptions across the full row", () => {
  assert.deepEqual(filterTableRows(rows, "1002").map(({ index }) => index), [1]);
  assert.deepEqual(filterTableRows(rows, "steel").map(({ index }) => index), [0, 1]);
});

test("search with no matches returns an empty result", () => {
  assert.deepEqual(filterTableRows(rows, "not present"), []);
});

test("copying one selected row includes dynamic headers and only that row", () => {
  const selected = new Set([1]);
  const selectedRows = selectedTableRows(rows, selected);
  const tsv = serializeTablesForCopy({
    tables: [{ headers: ["Item", "Description", "Unit", "Qty", "Rate", "Amount"], rows: selectedRows }],
    sourceName: "purchase.pdf",
    pageNumber: 3,
  });
  assert.match(tsv, /Sumber file\tHalaman\tTabel\tItem\tDescription\tUnit\tQty\tRate\tAmount/);
  assert.match(tsv, /purchase\.pdf\t3\t1\t1002\tCarbon steel flange/);
  assert.doesNotMatch(tsv, /1001|1003/);
});

test("copying several selected rows retains source ordering", () => {
  const selectedRows = selectedTableRows(rows, new Set([0, 2]));
  assert.deepEqual(selectedRows, [rows[0], rows[2]]);
});

test("copy all rows ignores filtering and viewport state", () => {
  assert.equal(filterTableRows(rows, "1001").length, 1);
  assert.deepEqual(selectedTableRows(rows, new Set([0, 1, 2])), rows);
  const tsv = serializeTablesForCopy({ tables: [{ headers: ["ID", "Description"], rows }] });
  for (const itemNumber of ["1001", "1002", "1003"]) assert.ok(tsv.includes(itemNumber));
});

test("a search filter preserves hidden selections and selections are table-local", () => {
  const docASelection = new Map([["1:1", new Set([0, 2])]]);
  const docBSelection = new Map([["1:1", new Set([1])]]);
  assert.deepEqual(filterTableRows(rows, "1001").map(({ index }) => index), [0]);
  assert.deepEqual(selectedTableRows(rows, docASelection.get("1:1")), [rows[0], rows[2]]);
  assert.deepEqual(selectedTableRows(rows, docBSelection.get("1:1")), [rows[1]]);
});

test("CSV and TSV serializers preserve delimiters, quotes, and newlines", () => {
  const quote = String.fromCharCode(34);
  const newline = String.fromCharCode(10);
  const value = `Valve, ${quote}A${quote}${newline}assembly`;
  const table = [{ headers: ["Description"], rows: [[value]] }];
  const csv = serializeTablesForCsv({ tables: table });
  const tsv = serializeTablesForCopy({ tables: table });
  const quoted = `${quote}Valve, ${quote}${quote}A${quote}${quote}${newline}assembly${quote}`;
  assert.ok(csv.includes(quoted));
  assert.ok(tsv.includes(quoted));
});

test("source context is inserted under Description and included in copy and CSV without counting as an item", () => {
  const table = {
    headers: ["Item No.", "Description", "Qty", "Unit"],
    column_count: 4,
    context_rows: [{ text: "SPARE PARTS F. INCINERATOR" }, { text: "ATLAS 800 SL WS P, 403616, 025-6" }],
    rows: [["57K4012", "FLAME CONTROL BOX", "1", "PC"]],
    row_count: 1,
  };
  assert.deepEqual(extractionTable.getTableContextRows(table, 4), [
    ["", "SPARE PARTS F. INCINERATOR", "", ""],
    ["", "ATLAS 800 SL WS P, 403616, 025-6", "", ""],
  ]);
  const tsv = serializeTablesForCopy({ tables: [table] });
  assert.match(tsv, /\tSPARE PARTS F\. INCINERATOR\t\t\r\n/);
  assert.match(tsv, /\t\t1\t57K4012\tFLAME CONTROL BOX/);
  const csv = serializeTablesForCsv({ tables: [table] });
  assert.match(csv, /SPARE PARTS F\. INCINERATOR/);
  assert.equal(table.row_count, 1);
});

test("multi-document work is bounded and one rejection does not stop other documents", async () => {
  const { runBounded } = concurrencyModule;
  let active = 0;
  let maxActive = 0;
  const results = await runBounded([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await new Promise((resolve) => setTimeout(resolve, value === 1 ? 12 : 2));
    active -= 1;
    if (value === 2) throw new Error("single document failed");
    return value * 2;
  });

  assert.equal(maxActive, 2);
  assert.deepEqual(results.map((result) => result.status), [
    "fulfilled", "rejected", "fulfilled", "fulfilled", "fulfilled",
  ]);
  assert.equal(results[4].value, 10);
});

test("per-document and overall progress use completed pages over known total pages", () => {
  assert.equal(progressModule.calculatePageProgress(5, 10).percentage, 50);
  assert.deepEqual(progressModule.aggregatePageProgress([
    { completed_pages: 3, total_pages: 4 },
    { completed_pages: 2, total_pages: 6 },
  ]), {
    counts_known: true,
    completed_pages: 5,
    total_pages: 10,
    percentage: 50,
    progress_mode: "completed_pages",
  });
});

test("overall percentage stays unavailable until every document page count is known", () => {
  assert.deepEqual(progressModule.aggregatePageProgress([
    { completed_pages: 2, total_pages: 4 },
    { completed_pages: 0, total_pages: 0 },
  ]), {
    counts_known: false,
    completed_pages: 2,
    total_pages: null,
    percentage: null,
    progress_mode: "completed_pages",
  });
});

test("API client polls opaque page progress while preserving the extraction response", async () => {
  const originalFetch = globalThis.fetch;
  const progressEvents = [];
  const file = new Blob(["pdf"], { type: "application/pdf" });
  Object.defineProperty(file, "name", { value: "progress.pdf" });
  let progressUrl = "";
  let inspectUrl = "";
  globalThis.fetch = async (input, options = {}) => {
    const url = String(input);
    if (options.method === "POST") {
      inspectUrl = url;
      return new Response(JSON.stringify({ pages: [{ page_number: 1 }], total_tables_detected: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    progressUrl = url;
    return new Response(JSON.stringify({
      phase: "extracting_page",
      status: "processing",
      completed_pages: 1,
      total_pages: 2,
      current_page: 2,
      failed_pages: 0,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const result = await apiModule.inspectPdfDocument(file, { onProgress: (event) => progressEvents.push(event) });
    assert.equal(result.total_tables_detected, 1);
    assert.match(inspectUrl, /progress_id=/);
    assert.match(progressUrl, /\/documents\/inspect\/progress\//);
    assert.equal(progressEvents[0].completed_pages, 1);
    assert.equal(progressEvents[0].total_pages, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
