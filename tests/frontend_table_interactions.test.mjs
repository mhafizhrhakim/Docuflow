import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../frontend/src/js/extraction-table.js", import.meta.url), "utf8");
const extractionTable = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const {
  filterTableRows,
  selectedTableRows,
  serializeTablesForCopy,
  serializeTablesForHtml,
  serializeTablesForCsv,
  addExcelCsvSeparatorDirective,
  buildXlsxWorkbook,
  copyTablesToClipboard,
} = extractionTable;
const progressSource = await readFile(new URL("../frontend/src/js/progress.js", import.meta.url), "utf8");
const progressModule = await import(`data:text/javascript;base64,${Buffer.from(progressSource).toString("base64")}`);
const apiSource = await readFile(new URL("../frontend/src/js/api.js", import.meta.url), "utf8");
const apiModule = await import(`data:text/javascript;base64,${Buffer.from(apiSource).toString("base64")}`);
const concurrencySource = await readFile(new URL("../frontend/src/js/concurrency.js", import.meta.url), "utf8");
const concurrencyModule = await import(`data:text/javascript;base64,${Buffer.from(concurrencySource).toString("base64")}`);
const elapsedTimerSource = await readFile(new URL("../frontend/src/js/elapsed-timer.js", import.meta.url), "utf8");
const elapsedTimerModule = await import(`data:text/javascript;base64,${Buffer.from(elapsedTimerSource).toString("base64")}`);

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

test("copying one selected row includes dynamic headers without source metadata", () => {
  const selected = new Set([1]);
  const selectedRows = selectedTableRows(rows, selected);
  const tsv = serializeTablesForCopy({
    tables: [{ headers: ["Item", "Description", "Unit", "Qty", "Rate", "Amount"], rows: selectedRows }],
    sourceName: "purchase.pdf",
    pageNumber: 3,
  });
  assert.match(tsv, /^Item\tDescription\tUnit\tQty\tRate\tAmount/);
  assert.match(tsv, /1002\tCarbon steel flange\tPCS\t1\t15\t15/);
  assert.doesNotMatch(tsv, /Sumber file|purchase\.pdf/);
  assert.doesNotMatch(tsv, /1001|1003/);
});

test("clipboard HTML carries grid borders and dynamic table structure", () => {
  const html = serializeTablesForHtml({
    tables: [{ headers: ["Item", "Description"], rows: [["1", "Valve <A>"]] }],
    sourceName: "private.pdf",
    pageNumber: 4,
  });
  assert.match(html, /<table border="1"/);
  assert.match(html, /xmlns:x="urn:schemas-microsoft-com:office:excel"/);
  assert.match(html, /mso-width-alt:\d+/);
  assert.match(html, /border:1px solid #b8cbe5/);
  assert.match(html, /<th[^>]*>Item<\/th>/);
  assert.match(html, /Valve &lt;A&gt;/);
  assert.doesNotMatch(html, /Sumber file|private\.pdf|Halaman/);
});

test("copy writes bordered HTML plus a plain TSV fallback", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const originalClipboardItem = Object.getOwnPropertyDescriptor(globalThis, "ClipboardItem");
  let writtenItem;
  class TestClipboardItem {
    constructor(formats) { this.formats = formats; }
  }
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { write: async ([item]) => { writtenItem = item; } } },
  });
  Object.defineProperty(globalThis, "ClipboardItem", {
    configurable: true,
    value: TestClipboardItem,
  });
  try {
    await copyTablesToClipboard({
      tables: [{ headers: ["No.", "Description"], rows: [["1", "Valve"]] }],
      sourceName: "internal.pdf",
      pageNumber: 2,
    });
    assert.deepEqual(Object.keys(writtenItem.formats).sort(), ["text/html", "text/plain"]);
    assert.match(await writtenItem.formats["text/html"].text(), /border:1px solid/);
    assert.match(await writtenItem.formats["text/plain"].text(), /^No\.\tDescription/);
    assert.doesNotMatch(await writtenItem.formats["text/plain"].text(), /internal\.pdf/);
  } finally {
    if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
    else delete globalThis.navigator;
    if (originalClipboardItem) Object.defineProperty(globalThis, "ClipboardItem", originalClipboardItem);
    else delete globalThis.ClipboardItem;
  }
});

test("copy retries rich HTML through native selection before plain-text fallback", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const originalSelection = Object.getOwnPropertyDescriptor(globalThis, "getSelection");
  const originalClipboardItem = Object.getOwnPropertyDescriptor(globalThis, "ClipboardItem");
  let selectedHtml = "";
  let plainFallbackUsed = false;
  let selected = false;
  const selection = {
    removeAllRanges() {},
    addRange() { selected = true; },
  };
  const fakeDocument = {
    body: { append(container) { selectedHtml = container.innerHTML; } },
    createElement() {
      return {
        style: {},
        setAttribute() {},
        remove() {},
      };
    },
    createRange() { return { selectNodeContents() {} }; },
    execCommand(command) { return command === "copy" && selected; },
  };
  class TestClipboardItem { constructor() {} }
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: {
      write: async () => { throw new Error("HTML clipboard blocked"); },
      writeText: async () => { plainFallbackUsed = true; },
    } },
  });
  Object.defineProperty(globalThis, "document", { configurable: true, value: fakeDocument });
  Object.defineProperty(globalThis, "getSelection", { configurable: true, value: () => selection });
  Object.defineProperty(globalThis, "ClipboardItem", { configurable: true, value: TestClipboardItem });
  try {
    await copyTablesToClipboard({ tables: [{ headers: ["Description"], rows: [["Long description"]] }] });
    assert.match(selectedHtml, /Long description/);
    assert.match(selectedHtml, /mso-width-alt/);
    assert.equal(plainFallbackUsed, false);
  } finally {
    if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
    else delete globalThis.navigator;
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else delete globalThis.document;
    if (originalSelection) Object.defineProperty(globalThis, "getSelection", originalSelection);
    else delete globalThis.getSelection;
    if (originalClipboardItem) Object.defineProperty(globalThis, "ClipboardItem", originalClipboardItem);
    else delete globalThis.ClipboardItem;
  }
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

test("CSV keeps extracted table columns first, separates tables, and appends provenance", () => {
  const csv = serializeTablesForCsv({
    tables: [
      { headers: ["Item", "Description", "Qty"], rows: [["1", "Valve, stainless", "2"]], table_index: 1 },
      { headers: ["No.", "Name"], rows: [["A2", "Gasket"]], table_index: 2 },
    ],
    sourceName: "vendor.pdf",
    pageNumber: 4,
    extraColumns: ["Jenis halaman"],
    extraValues: () => ["Digital/native"],
  });
  const content = csv.slice(1);
  assert.match(content, /^Item,Description,Qty,Sumber file,Halaman,Tabel,Jenis halaman\r\n/);
  assert.match(content, /1,"Valve, stainless",2,vendor\.pdf,4,1,Digital\/native/);
  assert.match(content, /\r\n\r\nNo\.,Name,Sumber file,Halaman,Tabel,Jenis halaman/);
  assert.match(content, /A2,Gasket,vendor\.pdf,4,2,Digital\/native/);
});

test("CSV downloads include an Excel delimiter directive while retaining UTF-8 BOM", () => {
  const csv = "\uFEFFItem,Description\r\n1,Valve";
  assert.equal(addExcelCsvSeparatorDirective(csv), "\uFEFFsep=,\r\nItem,Description\r\n1,Valve");
  assert.equal(addExcelCsvSeparatorDirective(csv, false), csv);
});

test("document-form blocks export their displayed cells without invented Kolom headers", () => {
  const csv = serializeTablesForCsv({
    tables: [{
      layout_type: "document_form",
      column_count: 3,
      rows: [["TO:", "OWNER", "DATE"]],
      form_rows: [[
        { column: 0, text: "TO:" },
        { column: 1, text: "MASTER & OWNER" },
        { column: 2, text: "DATE: 06/07/2026" },
      ]],
    }],
    includeSourceColumns: false,
  }).slice(1);
  assert.match(csv, /^,,\r\nTO:,MASTER & OWNER,DATE: 06\/07\/2026$/);
  assert.doesNotMatch(csv, /Kolom [1-3]/);
});

test("rich clipboard places several extracted tables vertically in one worksheet table", () => {
  const html = serializeTablesForHtml({
    tables: [
      { layout_type: "document_form", column_count: 2, rows: [["TO:", "Owner"]], form_rows: [[
        { column: 0, text: "TO:" }, { column: 1, text: "Owner" },
      ]] },
      { headers: ["No.", "Description", "Qty"], rows: [["1", "Valve", "2"]] },
    ],
  });
  assert.equal((html.match(/<table\b/g) ?? []).length, 1);
  assert.ok(html.indexOf("TO:") < html.indexOf("No."));
  assert.ok(html.includes('colspan="3"'));
  assert.doesNotMatch(html, /Kolom [1-3]/);
  assert.match(html, /<col width="\d+" style="width:\d+px;mso-width-source:userset;mso-width-alt:\d+;">/);
  assert.match(html, /<td width="\d+" style="[^"]*min-width:\d+px;mso-width-source:userset;/);
  assert.match(html, /table-layout:auto/);
  assert.doesNotMatch(html, /table-layout:fixed/);
});

test("rich clipboard gives long description columns practical content-based widths", () => {
  const html = serializeTablesForHtml({
    tables: [{
      headers: ["Line", "Description", "Unit price"],
      rows: [["1.00", "DETECTOR TUBES\nBENZENE #121L 10 PCS\tGASTEC 4 DAYS DELIVERY", "206.70"]],
    }],
  });
  const descriptionWidth = Number(html.match(/<td width="(\d+)" style="[^"]*">DETECTOR TUBES/)?.[1]);
  assert.ok(descriptionWidth >= 500, `expected a roomy description column, got ${descriptionWidth}px`);
  assert.match(html, />DETECTOR TUBES BENZENE #121L 10 PCS GASTEC 4 DAYS DELIVERY<\/td>/);
  assert.doesNotMatch(html, /<br>/);
  assert.match(html, /white-space:normal;word-break:normal;overflow-wrap:normal/);
});

test("Excel workbook export is a styled XLSX archive with borders, widths, and separate tables", () => {
  const workbook = buildXlsxWorkbook({
    tables: [
      { layout_type: "document_form", column_count: 2, rows: [["TO:", "Owner"]], form_rows: [[
        { column: 0, text: "TO:" }, { column: 1, text: "Owner & Company" },
      ]] },
      { headers: ["No.", "Description", "Qty"], rows: [["001", "Valve <A>", "2"]] },
    ],
    includeSourceColumns: false,
  });
  const archiveText = new TextDecoder().decode(workbook);
  const view = new DataView(workbook.buffer, workbook.byteOffset, workbook.byteLength);
  assert.equal(view.getUint32(0, true), 0x04034b50);
  assert.equal(view.getUint32(workbook.byteLength - 22, true), 0x06054b50);
  assert.ok(archiveText.includes("[Content_Types].xml"));
  assert.ok(archiveText.includes("xl/worksheets/sheet1.xml"));
  assert.ok(archiveText.includes('<borders count="2">'));
  assert.ok(archiveText.includes('customWidth="1"'));
  assert.ok(archiveText.includes("Owner &amp; Company"));
  assert.ok(archiveText.includes("Valve &lt;A&gt;"));
  assert.ok(!archiveText.includes("Kolom 1"));
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
  assert.match(tsv, /^Item No\.\tDescription\tQty\tUnit\r\n\tSPARE PARTS F\. INCINERATOR\t\t\r\n/);
  assert.match(tsv, /57K4012\tFLAME CONTROL BOX\t1\tPC/);
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

test("completed document progress overrides a stale last polling percentage", () => {
  assert.equal(progressModule.getProgressPercentage({
    status: "completed", progress_percent: 93, completed_pages: 12, total_pages: 12,
  }), 100);
  assert.equal(progressModule.getProgressPercentage({
    status: "processing", progress_percent: 93, completed_pages: 12, total_pages: 12,
  }), 93);
  assert.equal(progressModule.aggregatePageProgress([
    { status: "completed", progress_percent: 93, completed_pages: 12, total_pages: 12 },
  ]).percentage, 100);
  assert.equal(progressModule.aggregatePageProgress([
    { status: "completed", progress_percent: 93, completed_pages: 12, total_pages: 12 },
    { status: "processing", progress_percent: 82, completed_pages: 4, total_pages: 8 },
  ]).percentage, 93);
});

test("elapsed timer displays seconds while running and freezes when processing ends", () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalNow = Date.now;
  let now = 1_000;
  let tick;
  let cleared = false;
  const element = { textContent: "" };
  Date.now = () => now;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setInterval(callback) { tick = callback; return 1; },
      clearInterval() { cleared = true; },
    },
  });
  try {
    const timer = elapsedTimerModule.createElapsedTimer(element);
    timer.start();
    now = 4_200;
    tick();
    assert.equal(element.textContent, "Durasi 00:03");
    timer.stop();
    assert.equal(cleared, true);
    now = 9_000;
    tick();
    assert.equal(element.textContent, "Durasi 00:03");
    timer.reset();
    assert.equal(element.textContent, "Durasi 00:00");
  } finally {
    Date.now = originalNow;
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
    else delete globalThis.window;
  }
});

test("API client polls opaque page progress while preserving the extraction response", async () => {
  const originalFetch = globalThis.fetch;
  const progressEvents = [];
  const requestHeaders = [];
  const file = new Blob(["pdf"], { type: "application/pdf" });
  Object.defineProperty(file, "name", { value: "progress.pdf" });
  let progressUrl = "";
  let inspectUrl = "";
  globalThis.fetch = async (input, options = {}) => {
    const url = String(input);
    requestHeaders.push(new Headers(options.headers ?? {}));
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
    assert.ok(requestHeaders.every((headers) => !headers.has("Authorization")));
    assert.equal("getAuthStatus" in apiModule, false);
    assert.equal(progressEvents[0].completed_pages, 1);
    assert.equal(progressEvents[0].total_pages, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("frontend HTML opens on the workspace without an application login gate", async () => {
  const html = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
  const app = await readFile(new URL("../frontend/src/js/app.js", import.meta.url), "utf8");

  assert.match(html, /<div class="app-shell">/);
  assert.match(html, /<details class="table-export-menu">[\s\S]*?id="digital-export-button"[\s\S]*?id="digital-xlsx-button"[\s\S]*?<\/details>/);
  assert.match(html, /<details class="table-export-menu table-export-menu-bottom">[\s\S]*?id="multi-export-button"[\s\S]*?id="multi-xlsx-export-button"[\s\S]*?<\/details>/);
  assert.doesNotMatch(html, /auth-gate|auth-form|logout-button|Password akses/);
  assert.match(app, /void setMode\("single"\)/);
  assert.doesNotMatch(app, /createAccessSession|validateAccessSession|initializeAccess/);
});
