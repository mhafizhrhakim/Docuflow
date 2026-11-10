/**
 * Dynamic, page-scoped extraction rendering for DocuFlow.
 * Column labels are always read from the extracted PDF data; this module never
 * creates invoice-specific headers.
 */

function setText(element, value) {
  if (element) element.textContent = value ?? "";
}

function statusMessage(status) {
  switch (status) {
    case "awaiting_ocr":
      return {
        title: "OCR diperlukan",
        message: "Halaman ini memerlukan OCR sebelum tabel dapat diperiksa.",
        className: "status-pending",
      };
    case "ocr_pending":
      return {
        title: "OCR sedang berjalan",
        message: "Halaman scan sedang dibaca dengan OCR. Hasil tabel akan muncul otomatis.",
        className: "status-pending",
      };
    case "ocr_failed":
      return {
        title: "OCR gagal",
        message: "Halaman scan tidak dapat dibaca. Lihat pesan error di bawah panel scan.",
        className: "status-error",
      };
    case "no_table_detected":
      return {
        title: "Tidak ada tabel terdeteksi",
        message: "Tidak ada tabel terdeteksi pada halaman ini.",
        className: "status-neutral",
      };
    case "detection_error":
      return {
        title: "Ekstraksi gagal",
        message: "Tabel tidak dapat diperiksa karena proses deteksi mengalami kendala.",
        className: "status-error",
      };
    default:
      return {
        title: "Menunggu ekstraksi",
        message: "Ekstraksi belum dijalankan untuk halaman ini.",
        className: "status-pending",
      };
  }
}

function reviewReasonLabel(reason) {
  const labels = {
    row_alignment_uncertain: "Posisi teks baris menyimpang dari lane kolom yang diharapkan.",
    header_not_detected: "Header tidak dikenali; periksa susunan kolom terhadap PDF sumber.",
    low_ocr_confidence: "Teks OCR pada baris ini memiliki confidence rendah.",
    ocr_text_not_assigned_to_cell: "Teks OCR ada di area sel yang hasilnya kosong.",
    ocr_numeric_confusable_normalized: "Angka dinormalisasi dari karakter OCR yang ambigu; cocokkan dengan PDF sumber.",
    empty_cell: "Ada sel kosong yang perlu dibandingkan dengan sumber PDF.",
    column_count_mismatch: "Jumlah sel baris berbeda dari jumlah kolom tabel.",
    duplicate_identifier: "Nomor item terdeteksi berulang.",
    possible_sequence_gap: "Ada kemungkinan jeda nomor item; ini bukan bukti baris sumber hilang.",
    headerless_repeated_numeric_lanes: "Header tidak dikenali; kolom disusun dari pola posisi teks.",
  };
  return labels[reason] ?? "Baris ini perlu dibandingkan dengan halaman sumber PDF.";
}

function createStateElement(status) {
  const state = statusMessage(status);
  const wrapper = document.createElement("div");
  wrapper.className = `extraction-empty-state ${state.className}`;
  const heading = document.createElement("strong");
  heading.textContent = state.title;
  const message = document.createElement("p");
  message.textContent = state.message;
  wrapper.append(heading, message);
  return wrapper;
}

function renderState(container, status) {
  if (!container) return;
  container.replaceChildren(createStateElement(status));
}

function renderLegacyEmptyRow(tbody, message, columnCount = 1) {
  if (!tbody) return;
  const row = document.createElement("tr");
  row.className = "empty-row";
  const cell = document.createElement("td");
  cell.colSpan = Math.max(1, Number(columnCount) || 1);
  cell.textContent = message;
  row.append(cell);
  tbody.replaceChildren(row);
}

export function showExtractionPending({
  container,
  tbody,
  subtitle,
  footer,
  pageNumber,
  sourceName,
  columnCount = 7,
  context = "halaman",
}) {
  if (container) renderState(container, "not_processed");
  renderLegacyEmptyRow(tbody, `Ekstraksi belum dijalankan untuk ${context} ${pageNumber ?? "ini"}.`, columnCount);
  setText(subtitle, pageNumber ? `Halaman ${pageNumber} · belum dianalisis` : "Belum dianalisis");
  setText(
    footer,
    sourceName
      ? `Sumber: ${sourceName} · halaman ${pageNumber ?? "-"}. Data tetap terhubung ke halaman asal.`
      : `Sumber halaman ${pageNumber ?? "-"}. Data belum tersedia karena ekstraksi belum dijalankan.`,
  );
}

export function showNoTableFound({
  container,
  tbody,
  subtitle,
  footer,
  pageNumber,
  sourceName,
  columnCount = 7,
}) {
  if (container) renderState(container, "no_table_detected");
  renderLegacyEmptyRow(tbody, "Tidak ada tabel terdeteksi pada halaman ini.", columnCount);
  setText(subtitle, pageNumber ? `Halaman ${pageNumber} · analisis selesai` : "Analisis selesai");
  setText(
    footer,
    sourceName
      ? `Sumber: ${sourceName} · halaman ${pageNumber ?? "-"}. Tidak ada tabel yang terdeteksi pada hasil analisis.`
      : `Halaman ${pageNumber ?? "-"} telah dianalisis dan tidak memiliki tabel terdeteksi.`,
  );
}

export function renderExtractionRows({ tbody, rows, columns, columnCount = 7 }) {
  if (!tbody) return;
  if (!Array.isArray(rows) || rows.length === 0) {
    renderLegacyEmptyRow(tbody, "Tidak ada baris hasil ekstraksi.", columnCount);
    return;
  }

  const fragment = document.createDocumentFragment();
  rows.forEach((item, index) => {
    const row = document.createElement("tr");
    const values = Array.isArray(item)
      ? item
      : Array.isArray(columns)
        ? columns.map((column) => item?.[column] ?? "")
        : Object.values(item ?? {});
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "");
      if (isNumericValue(cell.textContent)) cell.classList.add("is-numeric");
      row.append(cell);
    });
    fragment.append(row);
  });
  tbody.replaceChildren(fragment);
}

function isNumericValue(value) {
  const text = String(value ?? "").trim();
  if (!text) return false;
  return /^(?:[-–—]|\(?-?[$€£₹]?\s*\d[\d,.]*(?:\s*%)?\)?)$/.test(text);
}

function makeCell(value, tagName = "td") {
  const cell = document.createElement(tagName);
  const text = String(value ?? "");
  cell.textContent = text;
  if (isNumericValue(text) || (tagName === "th" && /\b(?:qty|quantity|rate|price|amount|amt|total|number|s\.?no|line no)\b/i.test(text))) {
    cell.classList.add("is-numeric");
  }
  if (text.length >= 28 && /\s/.test(text)) cell.classList.add("cell-long-text");
  return cell;
}

export function filterTableRows(rows, query = "") {
  const sourceRows = Array.isArray(rows) ? rows : [];
  const normalizedQuery = String(query ?? "").trim().toLocaleLowerCase();
  if (!normalizedQuery) return sourceRows.map((row, index) => ({ row, index }));
  return sourceRows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => (Array.isArray(row) ? row : Object.values(row ?? {}))
      .some((value) => String(value ?? "").toLocaleLowerCase().includes(normalizedQuery)));
}

export function selectedTableRows(rows, selectedIndices) {
  const sourceRows = Array.isArray(rows) ? rows : [];
  const selected = selectedIndices instanceof Set ? selectedIndices : new Set();
  return sourceRows.filter((_row, index) => selected.has(index));
}

export function getTableContextRows(tableData, columnCount) {
  const headers = Array.isArray(tableData?.headers) ? tableData.headers : [];
  const descriptionColumn = Math.max(0, headers.findIndex((header) =>
    /description|desc\.?|name of part|item description/i.test(String(header ?? ""))
  ));
  const contextRows = Array.isArray(tableData?.context_rows) ? tableData.context_rows : [];
  const values = contextRows.map((row) => String(row?.text ?? "").trim()).filter(Boolean);
  const parent = tableData?.parent_item_context;
  if (parent && (parent.identifier || parent.description)) {
    values.push([parent.identifier ? `${parent.identifier})` : "", parent.description]
      .filter(Boolean).join(" ").trim());
  }
  return [...new Set(values)].map((text) => {
    const row = Array(columnCount).fill("");
    row[descriptionColumn] = text;
    return row;
  });
}

function renderDetectedTable(tableData, index, options = {}) {
  if (tableData?.layout_type === "document_form" && Array.isArray(tableData.form_rows)) {
    return renderDocumentFormTable(tableData, index, options);
  }

  const wrapper = document.createElement("section");
  wrapper.className = "extracted-table-block";

  const title = document.createElement("div");
  title.className = "extracted-table-title";
  const sourcePageText = Number(tableData.source_page) > 0 ? ` · halaman ${tableData.source_page}` : "";
  title.textContent = `Tabel ${tableData.table_index ?? index + 1} · ${tableData.row_count ?? tableData.rows?.length ?? 0} baris${sourcePageText}`;
  wrapper.append(title);

  const table = document.createElement("table");
  table.className = "data-table extracted-data-table";
  const headers = Array.isArray(tableData.headers) ? tableData.headers : [];
  const headerRows = Array.isArray(tableData.header_rows) ? tableData.header_rows : [];
  const structuredHeaderCells = Array.isArray(tableData.header_cells) ? tableData.header_cells : [];
  const rows = Array.isArray(tableData.rows) ? tableData.rows : [];
  const columnCount = Math.max(
    Number(tableData.column_count) || 0,
    headers.length,
    ...rows.map((row) => Array.isArray(row) ? row.length : 0),
    1,
  );
  const contextRows = getTableContextRows(tableData, columnCount);
  const isCompactIdentifierHeader = (value) =>
    /^[#№]$/.test(String(value ?? "").trim())
    || /^(?:no\.?|l\.?\s*no\.?|item\s*(?:no\.?|number))$/i.test(String(value ?? "").trim());
  const codeColumns = new Set();
  for (let column = 0; column < columnCount; column += 1) {
    const values = rows.map((row) => String(Array.isArray(row) ? row[column] ?? "" : "").trim()).filter(Boolean);
    const compactCodes = values.filter((value) => {
      const compact = value.replace(/\s+/g, "");
      return compact.length >= 7 && /\d/.test(compact) && /^[\p{L}\d._/#-]+$/u.test(compact);
    });
    if (compactCodes.length >= 2 && compactCodes.length / values.length >= 0.6) codeColumns.add(column);
  }
  const { sourceName = "", pageNumber = tableData.source_page, searchText = "", selectionState = new Map() } = options;
  const matches = filterTableRows(rows, searchText);
  const tableKey = `${Number(pageNumber) || 0}:${tableData.table_index ?? index + 1}`;
  let selected = selectionState.get(tableKey);
  if (!(selected instanceof Set)) {
    selected = new Set();
    selectionState.set(tableKey, selected);
  }
  [...selected].forEach((rowIndex) => {
    if (!Number.isInteger(rowIndex) || rowIndex < 0 || rowIndex >= rows.length) selected.delete(rowIndex);
  });

  const selectionActions = document.createElement("div");
  selectionActions.className = "table-row-actions";
  const selectionCount = document.createElement("span");
  selectionCount.className = "table-selection-count";
  const copySelectedButton = document.createElement("button");
  copySelectedButton.type = "button";
  copySelectedButton.className = "table-row-action";
  copySelectedButton.textContent = "Copy Baris Terpilih";
  copySelectedButton.title = "Menyalin pilihan yang masih tersimpan, termasuk baris yang sedang tersembunyi oleh pencarian.";
  const exportSelectedButton = document.createElement("button");
  exportSelectedButton.type = "button";
  exportSelectedButton.className = "table-row-action";
  exportSelectedButton.textContent = "Excel Baris Terpilih";
  exportSelectedButton.title = "Unduh baris terpilih sebagai file Excel dengan lebar kolom dan format tabel.";
  const copyAllButton = document.createElement("button");
  copyAllButton.type = "button";
  copyAllButton.className = "table-row-action";
  copyAllButton.textContent = "Copy Semua";
  copyAllButton.title = "Menyalin seluruh baris tabel, termasuk baris yang tidak tampil karena pencarian atau scrolling.";
  selectionActions.append(selectionCount, copySelectedButton, exportSelectedButton, copyAllButton);
  wrapper.append(selectionActions);

  const updateSelectionControls = () => {
    const hiddenSelectionCount = [...selected].filter((rowIndex) => !matches.some((item) => item.index === rowIndex)).length;
    selectionCount.textContent = String(searchText ?? "").trim()
      ? `${selected.size} dipilih · pencarian hanya menyaring tampilan${hiddenSelectionCount ? ` · ${hiddenSelectionCount} tersembunyi` : ""}`
      : `${selected.size} baris dipilih`;
    copySelectedButton.disabled = selected.size === 0;
    exportSelectedButton.disabled = selected.size === 0;
    copyAllButton.disabled = rows.length === 0;
  };

  const copyRows = async (rowsToCopy, actionLabel) => {
    try {
      await copyTablesToClipboard({
        tables: [{ ...tableData, rows: rowsToCopy }],
        sourceName,
        pageNumber,
      });
      selectionCount.textContent = `${actionLabel} · ${rowsToCopy.length} baris disalin`;
    } catch (error) {
      selectionCount.textContent = error?.message || "Baris gagal disalin.";
    }
  };
  copySelectedButton.addEventListener("click", () => {
    void copyRows(selectedTableRows(rows, selected), "Pilihan");
  });
  exportSelectedButton.addEventListener("click", () => {
    const selectedRows = selectedTableRows(rows, selected);
    if (selectedRows.length === 0) return;
    try {
      downloadTablesAsXlsx({
        tables: [{ ...tableData, rows: selectedRows }],
        sourceName,
        pageNumber,
        includeSourceColumns: false,
        filename: `${(sourceName || "docuflow").replace(/\.pdf$/i, "")}-halaman-${pageNumber || ""}-baris-terpilih.xlsx`,
      });
      selectionCount.textContent = `Excel · ${selectedRows.length} baris terpilih diunduh`;
    } catch (error) {
      selectionCount.textContent = error?.message || "Excel baris terpilih gagal dibuat.";
    }
  });
  copyAllButton.addEventListener("click", () => void copyRows(rows, "Semua baris"));
  updateSelectionControls();

  const hasReadableHeader = structuredHeaderCells.length > 0
    || headerRows.some((row) => Array.isArray(row) && row.some((value) => String(value ?? "").trim()))
    || headers.some((value) => String(value ?? "").trim());
  const selectAllCheckbox = document.createElement("input");
  selectAllCheckbox.type = "checkbox";
  selectAllCheckbox.className = "table-select-all";
  selectAllCheckbox.setAttribute("aria-label", "Pilih seluruh baris hasil pencarian");
  selectAllCheckbox.title = "Pilih seluruh baris yang sedang cocok dengan pencarian";
  selectAllCheckbox.disabled = matches.length === 0;
  const selectionHeader = document.createElement("th");
  selectionHeader.className = "table-selection-column";
  selectionHeader.append(selectAllCheckbox);
  const syncSelectAll = () => {
    const matchedIndices = matches.map((item) => item.index);
    const selectedMatched = matchedIndices.filter((rowIndex) => selected.has(rowIndex)).length;
    selectAllCheckbox.checked = matchedIndices.length > 0 && selectedMatched === matchedIndices.length;
    selectAllCheckbox.indeterminate = selectedMatched > 0 && selectedMatched < matchedIndices.length;
    selectAllCheckbox.disabled = matchedIndices.length === 0;
  };
  selectAllCheckbox.addEventListener("change", () => {
    const matchedIndices = matches.map((item) => item.index);
    if (selectAllCheckbox.checked) matchedIndices.forEach((rowIndex) => selected.add(rowIndex));
    else matchedIndices.forEach((rowIndex) => selected.delete(rowIndex));
    tbody.querySelectorAll("input[data-row-index]").forEach((checkbox) => {
      checkbox.checked = selected.has(Number(checkbox.dataset.rowIndex));
    });
    syncSelectAll();
    updateSelectionControls();
  });
  if (hasReadableHeader) {
    const thead = document.createElement("thead");
    if (structuredHeaderCells.length > 0) {
      // Backend-provided coordinates describe grouped parent headers and merged
      // cells. They are source-derived; the renderer does not infer any labels.
      const headerRowCount = Math.max(
        1,
        ...structuredHeaderCells.map((cell) => Number(cell.row || 0) + Math.max(1, Number(cell.rowspan) || 1)),
      );
      const renderedRows = Array.from({ length: headerRowCount }, () => document.createElement("tr"));
      const orderedCells = [...structuredHeaderCells].sort(
        (left, right) => Number(left.row || 0) - Number(right.row || 0) || Number(left.column || 0) - Number(right.column || 0),
      );
      orderedCells.forEach((item) => {
        const rowIndex = Math.max(0, Math.min(headerRowCount - 1, Number(item.row) || 0));
        const cell = makeCell(item.text ?? "", "th");
        cell.rowSpan = Math.max(1, Number(item.rowspan) || 1);
        cell.colSpan = Math.max(1, Number(item.colspan) || 1);
        cell.dataset.sourceColumn = String(Number(item.column) || 0);
        renderedRows[rowIndex].append(cell);
      });
      renderedRows[0].prepend(selectionHeader);
      selectionHeader.rowSpan = headerRowCount;
      renderedRows.forEach((row) => thead.append(row));
    } else if (headerRows.length > 1) {
      headerRows.forEach((values, headerIndex) => {
        const row = document.createElement("tr");
        if (headerIndex === 0) {
          selectionHeader.rowSpan = headerRows.length;
          row.append(selectionHeader);
        }
        for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {
          row.append(makeCell(values?.[columnIndex] ?? "", "th"));
        }
        thead.append(row);
      });
    } else {
      const headerRow = document.createElement("tr");
      headerRow.append(selectionHeader);
      const flatHeaders = headerRows[0] ?? headers;
      flatHeaders.forEach((header) => {
        const cell = makeCell(header, "th");
        if (isCompactIdentifierHeader(header)) cell.classList.add("table-identifier-column");
        headerRow.append(cell);
      });
      for (let i = flatHeaders.length; i < columnCount; i += 1) {
        headerRow.append(makeCell("", "th"));
      }
      thead.append(headerRow);
    }
    table.append(thead);
  } else if (rows.length > 0) {
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    headerRow.append(selectionHeader);
    for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {
      headerRow.append(makeCell("", "th"));
    }
    thead.append(headerRow);
    table.append(thead);
  }

  const tbody = document.createElement("tbody");
  const rowsNeedReview = Array.isArray(tableData.rows_need_review) ? tableData.rows_need_review : [];
  const cellReviewFlags = Array.isArray(tableData.cell_review_flags) ? tableData.cell_review_flags : [];
  const rowSourceBboxes = Array.isArray(tableData.row_source_bboxes) ? tableData.row_source_bboxes : [];
  contextRows.forEach((values) => {
    const row = document.createElement("tr");
    row.className = "table-context-row";
    const selectionCell = document.createElement("td");
    selectionCell.className = "table-selection-column";
    row.append(selectionCell);
    values.forEach((value) => row.append(makeCell(value)));
    tbody.append(row);
  });
  if (rows.length > 0 && matches.length > 0) {
    matches.forEach(({ row: rowValues, index: rowIndex }) => {
      const row = document.createElement("tr");
      const values = Array.isArray(rowValues) ? rowValues : Object.values(rowValues ?? {});
      if (rowsNeedReview[rowIndex]) {
        row.classList.add("row-needs-review");
        row.title = (tableData.row_review_reasons?.[rowIndex] ?? ["Perlu diperiksa"])
          .map(reviewReasonLabel)
          .join(" ");
      }
      if (Array.isArray(rowSourceBboxes[rowIndex])) row.dataset.sourceBbox = rowSourceBboxes[rowIndex].join(",");
      const selectCell = document.createElement("td");
      selectCell.className = "table-selection-column";
      const rowCheckbox = document.createElement("input");
      rowCheckbox.type = "checkbox";
      rowCheckbox.className = "table-row-checkbox";
      rowCheckbox.dataset.rowIndex = String(rowIndex);
      rowCheckbox.setAttribute("aria-label", `Pilih baris ${rowIndex + 1}`);
      rowCheckbox.checked = selected.has(rowIndex);
      rowCheckbox.addEventListener("change", () => {
        if (rowCheckbox.checked) selected.add(rowIndex);
        else selected.delete(rowIndex);
        syncSelectAll();
        updateSelectionControls();
      });
      selectCell.append(rowCheckbox);
      row.append(selectCell);
      for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {
        const cell = makeCell(values[columnIndex] ?? "");
        if (codeColumns.has(columnIndex)) cell.classList.add("table-code-column");
        if (isCompactIdentifierHeader(headers[columnIndex] ?? headerRows[0]?.[columnIndex])) {
          cell.classList.add("table-identifier-column");
        }
        if (cellReviewFlags[rowIndex]?.[columnIndex]) {
          cell.classList.add("cell-needs-review");
          cell.title = "Perlu diperiksa terhadap sumber PDF";
        }
        row.append(cell);
      }
      tbody.append(row);
    });
  } else if (rows.length > 0 && searchText.trim()) {
    renderLegacyEmptyRow(tbody, "Tidak ada baris yang cocok dengan pencarian.", columnCount + 1);
  } else {
    renderLegacyEmptyRow(tbody, "Tidak ada baris hasil ekstraksi.", columnCount);
  }
  table.append(tbody);
  syncSelectAll();
  wrapper.append(table);

  if (!hasReadableHeader && rows.length > 0) {
    const note = document.createElement("p");
    note.className = "table-note";
    note.textContent = "Header tidak ditemukan pada halaman ini. Kolom ditampilkan sesuai urutan data yang terdeteksi.";
    wrapper.append(note);
  }

  const reviewIssues = Array.isArray(tableData.review_issues) ? tableData.review_issues : [];
  if (tableData.review_required || reviewIssues.length > 0) {
    const review = document.createElement("aside");
    review.className = "table-review-summary";
    review.setAttribute("role", "note");
    const heading = document.createElement("strong");
    heading.textContent = `Perlu diperiksa · ${reviewIssues.length || "ada"} indikator`;
    review.append(heading);
    if (reviewIssues.length > 0) {
      const list = document.createElement("ul");
      reviewIssues.slice(0, 8).forEach((issue) => {
        const item = document.createElement("li");
        item.textContent = issue.message ?? "Struktur atau nilai perlu dibandingkan dengan sumber.";
        list.append(item);
      });
      if (reviewIssues.length > 8) {
        const more = document.createElement("li");
        more.textContent = `${reviewIssues.length - 8} indikator lainnya tersedia pada data validasi.`;
        list.append(more);
      }
      review.append(list);
    }
    wrapper.append(review);
  }

  return wrapper;
}

function renderDocumentFormTable(tableData, index, options = {}) {
  const wrapper = document.createElement("section");
  wrapper.className = "extracted-table-block extracted-document-form-block";

  const formRows = Array.isArray(tableData.form_rows) ? tableData.form_rows : [];
  const columnCount = Math.max(1, Number(tableData.column_count) || 1);
  const flatRows = formRows.map((formRow) => {
    const values = Array.from({ length: columnCount }, () => "");
    for (const item of Array.isArray(formRow) ? formRow : []) {
      const column = Math.max(0, Math.min(columnCount - 1, Number(item.column) || 0));
      values[column] = String(item.text ?? "");
    }
    return values;
  });
  const { sourceName = "", pageNumber = tableData.source_page, searchText = "", selectionState = new Map() } = options;
  const matches = filterTableRows(flatRows, searchText);
  const tableKey = `${Number(pageNumber) || 0}:${tableData.table_index ?? index + 1}`;
  let selected = selectionState.get(tableKey);
  if (!(selected instanceof Set)) {
    selected = new Set();
    selectionState.set(tableKey, selected);
  }
  [...selected].forEach((rowIndex) => {
    if (!Number.isInteger(rowIndex) || rowIndex < 0 || rowIndex >= formRows.length) selected.delete(rowIndex);
  });
  const title = document.createElement("div");
  title.className = "extracted-table-title";
  title.textContent = `Tabel ${tableData.table_index ?? index + 1} · Blok informasi dokumen · ${tableData.row_count ?? formRows.length} baris`;
  wrapper.append(title);

  const selectionActions = document.createElement("div");
  selectionActions.className = "table-row-actions";
  const selectionCount = document.createElement("span");
  selectionCount.className = "table-selection-count";
  const copySelectedButton = document.createElement("button");
  copySelectedButton.type = "button";
  copySelectedButton.className = "table-row-action";
  copySelectedButton.textContent = "Copy Baris Terpilih";
  const exportSelectedButton = document.createElement("button");
  exportSelectedButton.type = "button";
  exportSelectedButton.className = "table-row-action";
  exportSelectedButton.textContent = "Excel Baris Terpilih";
  exportSelectedButton.title = "Unduh baris terpilih sebagai file Excel dengan lebar kolom dan format tabel.";
  const copyAllButton = document.createElement("button");
  copyAllButton.type = "button";
  copyAllButton.className = "table-row-action";
  copyAllButton.textContent = "Copy Semua";
  selectionActions.append(selectionCount, copySelectedButton, exportSelectedButton, copyAllButton);
  wrapper.append(selectionActions);

  const updateSelectionControls = () => {
    const hiddenCount = [...selected].filter((rowIndex) => !matches.some((item) => item.index === rowIndex)).length;
    selectionCount.textContent = String(searchText ?? "").trim()
      ? `${selected.size} dipilih · pencarian hanya menyaring tampilan${hiddenCount ? ` · ${hiddenCount} tersembunyi` : ""}`
      : `${selected.size} baris dipilih`;
    copySelectedButton.disabled = selected.size === 0;
    exportSelectedButton.disabled = selected.size === 0;
    copyAllButton.disabled = formRows.length === 0;
  };
  const copyFormRows = async (rowsToCopy, label) => {
    try {
      await copyTablesToClipboard({
        tables: [{
          ...tableData,
          layout_type: undefined,
          form_rows: undefined,
          headers: Array(columnCount).fill(""),
          rows: rowsToCopy,
          column_count: columnCount,
        }],
        sourceName,
        pageNumber,
      });
      selectionCount.textContent = `${label} · ${rowsToCopy.length} baris disalin`;
    } catch (error) {
      selectionCount.textContent = error?.message || "Baris gagal disalin.";
    }
  };
  copySelectedButton.addEventListener("click", () => {
    void copyFormRows(selectedTableRows(flatRows, selected), "Pilihan");
  });
  exportSelectedButton.addEventListener("click", () => {
    const selectedRows = selectedTableRows(flatRows, selected);
    if (selectedRows.length === 0) return;
    try {
      downloadTablesAsXlsx({
        tables: [{
          ...tableData,
          layout_type: undefined,
          form_rows: undefined,
          headers: Array(columnCount).fill(""),
          rows: selectedRows,
          column_count: columnCount,
        }],
        sourceName,
        pageNumber,
        includeSourceColumns: false,
        filename: `${(sourceName || "docuflow").replace(/\.pdf$/i, "")}-halaman-${pageNumber || ""}-informasi-terpilih.xlsx`,
      });
      selectionCount.textContent = `Excel · ${selectedRows.length} baris terpilih diunduh`;
    } catch (error) {
      selectionCount.textContent = error?.message || "Excel baris terpilih gagal dibuat.";
    }
  });
  copyAllButton.addEventListener("click", () => void copyFormRows(flatRows, "Semua baris"));
  updateSelectionControls();

  const table = document.createElement("table");
  table.className = "data-table extracted-data-table extracted-document-form-table";
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  const selectAllHeader = document.createElement("th");
  selectAllHeader.className = "table-selection-column";
  const selectAllCheckbox = document.createElement("input");
  selectAllCheckbox.type = "checkbox";
  selectAllCheckbox.className = "table-select-all";
  selectAllCheckbox.setAttribute("aria-label", "Pilih seluruh baris hasil pencarian");
  selectAllHeader.append(selectAllCheckbox);
  headerRow.append(selectAllHeader);
  for (let column = 0; column < columnCount; column += 1) headerRow.append(makeCell("", "th"));
  thead.append(headerRow);
  table.append(thead);
  const tbody = document.createElement("tbody");
  const syncSelectAll = () => {
    const visibleIndices = matches.map((item) => item.index);
    const selectedVisible = visibleIndices.filter((rowIndex) => selected.has(rowIndex)).length;
    selectAllCheckbox.checked = visibleIndices.length > 0 && selectedVisible === visibleIndices.length;
    selectAllCheckbox.indeterminate = selectedVisible > 0 && selectedVisible < visibleIndices.length;
    selectAllCheckbox.disabled = visibleIndices.length === 0;
  };
  selectAllCheckbox.title = "Pilih seluruh baris yang sedang cocok dengan pencarian";
  selectAllCheckbox.addEventListener("change", () => {
    const visibleIndices = matches.map((item) => item.index);
    if (selectAllCheckbox.checked) visibleIndices.forEach((rowIndex) => selected.add(rowIndex));
    else visibleIndices.forEach((rowIndex) => selected.delete(rowIndex));
    tbody.querySelectorAll("input[data-row-index]").forEach((checkbox) => {
      checkbox.checked = selected.has(Number(checkbox.dataset.rowIndex));
    });
    syncSelectAll();
    updateSelectionControls();
  });

  matches.forEach(({ index: rowIndex }) => {
    const formRow = formRows[rowIndex];
    const row = document.createElement("tr");
    const selectionCell = document.createElement("td");
    selectionCell.className = "table-selection-column";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "table-row-checkbox";
    checkbox.dataset.rowIndex = String(rowIndex);
    checkbox.setAttribute("aria-label", `Pilih baris ${rowIndex + 1}`);
    checkbox.checked = selected.has(rowIndex);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(rowIndex);
      else selected.delete(rowIndex);
      syncSelectAll();
      updateSelectionControls();
    });
    selectionCell.append(checkbox);
    row.append(selectionCell);
    const cells = [...(Array.isArray(formRow) ? formRow : [])]
      .sort((left, right) => Number(left.column || 0) - Number(right.column || 0));
    let cursor = 0;
    for (const item of cells) {
      const start = Math.max(0, Math.min(columnCount - 1, Number(item.column) || 0));
      if (start > cursor) {
        const blank = document.createElement("td");
        blank.colSpan = start - cursor;
        blank.className = "document-form-empty-cell";
        row.append(blank);
        cursor = start;
      }
      // Ignore an overlapping cell instead of rendering duplicate source text.
      if (start < cursor) continue;
      const cell = makeCell(item.text ?? "");
      cell.colSpan = Math.max(1, Math.min(columnCount - start, Number(item.colspan) || 1));
      cell.classList.add("document-form-cell");
      row.append(cell);
      cursor = start + cell.colSpan;
    }
    if (cursor < columnCount) {
      const blank = document.createElement("td");
      blank.colSpan = columnCount - cursor;
      blank.className = "document-form-empty-cell";
      row.append(blank);
    }
    tbody.append(row);
  });

  if (formRows.length > 0 && matches.length === 0) {
    renderLegacyEmptyRow(tbody, "Tidak ada baris yang cocok dengan pencarian.", columnCount + 1);
  } else if (formRows.length === 0) {
    renderLegacyEmptyRow(tbody, "Tidak ada baris hasil ekstraksi.", columnCount + 1);
  }

  table.append(tbody);
  syncSelectAll();
  wrapper.append(table);
  return wrapper;
}

function getSummaryItems(tables, pageSummaryItems = []) {
  const summaryItems = [];
  const seen = new Set();
  const candidates = [
    ...(Array.isArray(pageSummaryItems) ? pageSummaryItems : []),
    ...tables.flatMap((table) => Array.isArray(table?.summary_items) ? table.summary_items : []),
  ];
  for (const item of candidates) {
    const key = `${String(item?.label ?? "").trim().toLowerCase()}|${String(item?.value ?? "").trim()}`;
    if (!item?.label || !item?.value || seen.has(key)) continue;
    seen.add(key);
    summaryItems.push(item);
  }
  return summaryItems;
}

function getMetadataItems(tables, extraItems = []) {
  const candidates = [
    ...(Array.isArray(extraItems) ? extraItems : []),
    ...tables.flatMap((table) => Array.isArray(table?.metadata_items) ? table.metadata_items : []),
  ];
  const items = [];
  const seen = new Set();
  for (const item of candidates) {
    const label = String(item?.label ?? "").trim();
    const value = String(item?.value ?? "").trim();
    if (!label || !value) continue;
    const box = Array.isArray(item?.source_bbox) && item.source_bbox.length === 4
      ? item.source_bbox.map((part) => {
          const number = Number(part);
          return Number.isFinite(number) ? number.toFixed(1) : "";
        }).join(",")
      : "";
    const sourcePage = Number(item?.source_page) || 0;
    // Identical records can arrive through both the page-level metadata table
    // and the compatibility field on the source table. Include source geometry
    // to dedupe those copies without dropping repeated fields from distinct spots.
    const key = `${label.toLocaleLowerCase()}|${value.toLocaleLowerCase()}|${sourcePage}|${box}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ ...item, label, value });
  }
  return items;
}

function medianNumber(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

/**
 * Render all detected label/value metadata for the active page in one table.
 *
 * Earlier grouping split records whenever vertical whitespace exceeded a
 * threshold. This produced many tiny tables for one logical document header
 * when a PDF laid out related fields in separate visual zones. The renderer
 * now keeps one metadata table per active-page result and sorts records by
 * their source geometry when available. Actual line-item and form tables are
 * still rendered separately from this metadata table.
 */
function groupMetadataItemsByLayout(items) {
  const ordered = [...items].sort((a, b) => {
    const pageA = Number(a?.source_page) || 0;
    const pageB = Number(b?.source_page) || 0;
    if (pageA !== pageB) return pageA - pageB;

    const bboxA = Array.isArray(a?.source_bbox) && a.source_bbox.length === 4
      ? a.source_bbox.map(Number)
      : null;
    const bboxB = Array.isArray(b?.source_bbox) && b.source_bbox.length === 4
      ? b.source_bbox.map(Number)
      : null;
    const yA = bboxA && bboxA.every(Number.isFinite) ? (bboxA[1] + bboxA[3]) / 2 : Number.POSITIVE_INFINITY;
    const yB = bboxB && bboxB.every(Number.isFinite) ? (bboxB[1] + bboxB[3]) / 2 : Number.POSITIVE_INFINITY;
    if (yA !== yB) return yA - yB;
    const xA = bboxA && bboxA.every(Number.isFinite) ? bboxA[0] : Number.POSITIVE_INFINITY;
    const xB = bboxB && bboxB.every(Number.isFinite) ? bboxB[0] : Number.POSITIVE_INFINITY;
    return xA - xB;
  });

  if (!ordered.length) return [];

  // renderDetectedTables is called for one active PDF page at a time, so one
  // unified metadata table is the clearest representation for that page.
  // If a future caller supplies multiple pages in one call, keep those pages
  // separate without splitting a page into coordinate-based micro-tables.
  const groups = [];
  for (const item of ordered) {
    const page = Number(item?.source_page) || 0;
    let group = groups.find((candidate) => candidate.page === page);
    if (!group) {
      group = { page, items: [] };
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups.map((group) => group.items);
}

function renderMetadataTableGroup(items, index) {
  const wrapper = document.createElement("section");
  wrapper.className = "extracted-table-block extracted-document-metadata-table";

  const title = document.createElement("div");
  title.className = "extracted-table-title";
  title.textContent = `Informasi dokumen · ${items.length} field`;
  const heading = document.createElement("div");
  heading.className = "metadata-table-heading";
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "button button-small metadata-copy-button";
  copyButton.textContent = "Salin informasi";
  copyButton.addEventListener("click", async () => {
    const tsv = [["Label", "Informasi"], ...items.map((item) => [item.label ?? "", item.value ?? ""])]
      .map((row) => row.map((value) => String(value).replace(/\t/g, " ").replace(/\r?\n/g, " ")).join("\t"))
      .join("\n");
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(tsv);
      } else {
        const temporary = document.createElement("textarea");
        temporary.value = tsv;
        temporary.style.position = "fixed";
        temporary.style.opacity = "0";
        document.body.append(temporary);
        temporary.select();
        const copied = document.execCommand("copy");
        temporary.remove();
        if (!copied) throw new Error("Browser menolak akses clipboard.");
      }
      copyButton.textContent = "Tersalin";
      window.setTimeout(() => { copyButton.textContent = "Salin informasi"; }, 1400);
    } catch {
      copyButton.textContent = "Gagal menyalin";
      window.setTimeout(() => { copyButton.textContent = "Salin informasi"; }, 1800);
    }
  });
  heading.append(title, copyButton);
  wrapper.append(heading);

  const table = document.createElement("table");
  table.className = "data-table extracted-data-table extracted-document-form-table";
  const tbody = document.createElement("tbody");
  for (const item of items) {
    const row = document.createElement("tr");
    const label = makeCell(item.label ?? "");
    label.classList.add("document-form-cell", "extracted-metadata-label-cell");
    const value = makeCell(item.value ?? "");
    value.classList.add("document-form-cell", "extracted-metadata-value-cell");
    row.append(label, value);
    tbody.append(row);
  }
  table.append(tbody);
  wrapper.append(table);
  return wrapper;
}

function renderMetadataItems(items) {
  const wrapper = document.createElement("div");
  wrapper.className = "extracted-document-metadata-tables";
  const groups = groupMetadataItemsByLayout(items);
  groups.forEach((group, index) => wrapper.append(renderMetadataTableGroup(group, index)));
  return wrapper;
}


function renderSummaryItems(items) {
  const wrapper = document.createElement("section");
  wrapper.className = "extraction-summary-list";
  const heading = document.createElement("div");
  heading.className = "extraction-summary-heading";
  heading.textContent = "Ringkasan terdeteksi dari dokumen";
  wrapper.append(heading);

  for (const item of items) {
    const row = document.createElement("div");
    row.className = "extraction-summary-row";
    const label = document.createElement("span");
    label.className = "extraction-summary-label";
    label.textContent = item.label;
    const value = document.createElement("strong");
    value.className = "extraction-summary-value";
    value.textContent = item.value;
    row.append(label, value);
    wrapper.append(row);
  }
  return wrapper;
}

/** Render source tables, one metadata table and page summaries separately. */
export function renderDetectedTables({
  container,
  tables = [],
  status = "not_processed",
  pageNumber,
  sourceName,
  subtitle,
  footer,
  metadataItems = [],
  summaryItems = [],
  searchText = "",
  selectionState = new Map(),
}) {
  if (!container) return;
  const safeTables = Array.isArray(tables) ? tables : [];
  const summaries = getSummaryItems(safeTables, summaryItems);

  // Synthetic metadata display tables are not native PDF table regions. They
  // are rendered once from their source-linked label/value records, and must
  // not affect source-table counts or item-row counts.
  const sourceTables = safeTables.filter((table) =>
    !table?.is_summary
    && table?.is_source_table !== false
    && table?.layout_type !== "document_metadata"
    && table?.table_role !== "document_metadata"
  );
  const metadata = getMetadataItems(safeTables, metadataItems);
  const formTables = sourceTables
    .filter((table) => table?.layout_type === "document_form" || table?.table_role === "document_header")
    .sort(compareSourcePosition);
  const itemTables = sourceTables
    .filter((table) => table?.layout_type !== "document_form" && table?.table_role !== "document_header")
    .sort(compareSourcePosition);

  container.replaceChildren();

  // Physical form/header tables retain source cell boundaries. Label/value
  // metadata is shown as one generic table per active page; it is not split at
  // arbitrary vertical gaps and never uses a fixed list of field names.
  formTables.forEach((table, index) => container.append(renderDetectedTable(table, index, {
    sourceName, pageNumber, searchText, selectionState,
  })));
  if (metadata.length > 0) container.append(renderMetadataItems(metadata));
  itemTables.forEach((table, index) => container.append(renderDetectedTable(table, formTables.length + index, {
    sourceName, pageNumber, searchText, selectionState,
  })));

  if (sourceTables.length === 0) {
    if (metadata.length === 0 && summaries.length === 0) {
      renderState(container, status);
    } else if (["awaiting_ocr", "ocr_pending", "ocr_failed", "no_table_detected", "detection_error"].includes(status)) {
      // Append status without replacing the metadata table already rendered.
      container.append(createStateElement(status));
    }
  }
  if (summaries.length > 0) container.append(renderSummaryItems(summaries));

  const itemRowCount = itemTables.reduce(
    (sum, table) => sum + (Number(table?.row_count) || table?.rows?.length || 0),
    0,
  );
  const reviewIssueCount = sourceTables.reduce(
    (sum, table) => sum + (Array.isArray(table?.review_issues) ? table.review_issues.length : 0),
    0,
  );
  if (subtitle) {
    if (sourceTables.length > 0) {
      subtitle.textContent = `Halaman PDF ${pageNumber ?? "-"} · ${sourceTables.length} tabel sumber · ${itemRowCount} baris item`;
    } else if (status === "awaiting_ocr" || status === "ocr_pending") {
      subtitle.textContent = `Halaman PDF ${pageNumber ?? "-"} · OCR sedang berjalan`;
    } else if (status === "ocr_failed") {
      subtitle.textContent = `Halaman PDF ${pageNumber ?? "-"} · OCR gagal`;
    } else if (metadata.length > 0) {
      subtitle.textContent = `Halaman PDF ${pageNumber ?? "-"} · ${metadata.length} informasi dokumen · tabel sumber tidak terdeteksi`;
    } else {
      subtitle.textContent = `Halaman PDF ${pageNumber ?? "-"} · ${status === "no_table_detected" ? "tidak ada tabel" : "belum tersedia"}`;
    }
  }
  if (footer) {
    const sourceText = sourceName
      ? `Sumber: ${sourceName} · halaman ${pageNumber ?? "-"}.`
      : `Halaman ${pageNumber ?? "-"}.`;
    const tableText = sourceTables.length > 0
      ? `${sourceTables.length} tabel sumber terdeteksi.${reviewIssueCount ? ` ${reviewIssueCount} indikator perlu diperiksa.` : ""}`
      : statusMessage(status).message;
    const summaryText = summaries.length > 0 ? ` ${summaries.length} ringkasan nilai ikut diekstrak terpisah.` : "";
    const metadataText = metadata.length > 0 ? ` ${metadata.length} informasi dokumen terdeteksi dalam satu tabel metadata.` : "";
    footer.textContent = `${sourceText} ${tableText}${summaryText}${metadataText}`;
  }
}

function compareSourcePosition(left, right) {
  const leftBox = Array.isArray(left?.source_bbox) && left.source_bbox.length === 4 ? left.source_bbox.map(Number) : null;
  const rightBox = Array.isArray(right?.source_bbox) && right.source_bbox.length === 4 ? right.source_bbox.map(Number) : null;
  const leftY = leftBox && leftBox.every(Number.isFinite) ? leftBox[1] : Number.POSITIVE_INFINITY;
  const rightY = rightBox && rightBox.every(Number.isFinite) ? rightBox[1] : Number.POSITIVE_INFINITY;
  if (leftY !== rightY) return leftY - rightY;
  const leftX = leftBox && leftBox.every(Number.isFinite) ? leftBox[0] : Number.POSITIVE_INFINITY;
  const rightX = rightBox && rightBox.every(Number.isFinite) ? rightBox[0] : Number.POSITIVE_INFINITY;
  return leftX - rightX;
}

function tableExportBlocks(
  tables, sourceName, pageNumber, extraColumns = [], extraValues = () => [], includeSourceColumns = true,
) {
  return (Array.isArray(tables) ? tables : [])
    .filter((table) => table?.is_source_table !== false && Array.isArray(table?.rows))
    .map((sourceTable, index) => {
      const sourceFormRows = Array.isArray(sourceTable?.form_rows) ? sourceTable.form_rows : [];
      const isDocumentForm = sourceTable?.layout_type === "document_form" && Array.isArray(sourceTable.form_rows);
      const formWidth = Math.max(
        1,
        Number(sourceTable.column_count) || 0,
        ...sourceFormRows.map((row) => (Array.isArray(row) ? row : [])
          .reduce((max, cell) => Math.max(max, (Number(cell?.column) || 0) + 1), 0)),
      );
      const table = isDocumentForm
        ? {
          ...sourceTable,
          headers: Array(formWidth).fill(""),
          rows: sourceFormRows.map((formRow) => {
            const values = Array(formWidth).fill("");
            for (const cell of Array.isArray(formRow) ? formRow : []) {
              const column = Math.max(0, Math.min(formWidth - 1, Number(cell?.column) || 0));
              values[column] = String(cell?.text ?? "");
            }
            return values;
          }),
          column_count: formWidth,
          context_rows: [],
        }
        : sourceTable;
      const rows = table.rows.map((row) => Array.isArray(row) ? row : Object.values(row ?? {}));
      const width = Math.max(
        Number(table.column_count) || 0,
        table.headers?.length || 0,
        ...rows.map((row) => row.length),
      );
      const contextRows = getTableContextRows(table, width);
      const headers = Array.from({ length: width }, (_, column) => {
        if (isDocumentForm) return "";
        const flat = String(table.headers?.[column] ?? "").trim();
        if (flat) return flat;
        const hierarchy = (table.header_rows ?? [])
          .map((row) => String(row?.[column] ?? "").trim())
          .filter(Boolean);
        if (hierarchy.length) return hierarchy.join(" / ");
        const structured = (table.header_cells ?? [])
          .filter((cell) => {
            const start = Number(cell.column) || 0;
            const span = Math.max(1, Number(cell.colspan) || 1);
            return column >= start && column < start + span && String(cell.text ?? "").trim();
          })
          .sort((left, right) => Number(left.row || 0) - Number(right.row || 0))
          .map((cell) => String(cell.text).trim());
        return structured.length ? [...new Set(structured)].join(" / ") : `Kolom ${column + 1}`;
      });
      const page = Number(table.source_page) || Number(pageNumber) || "";
      const tableIndex = Number(table.table_index) || index + 1;
      const source = table.export_source_name || sourceName || "";
      const extra = extraValues(table);
      const sourceContext = includeSourceColumns ? [source, page, tableIndex, ...extra] : extra;
      return {
        header: [
          ...headers,
          ...(includeSourceColumns ? ["Sumber file", "Halaman", "Tabel"] : []),
          ...extraColumns,
        ],
        rows: [...contextRows, ...rows].map((row) => [
          ...Array.from({ length: width }, (_, column) => row[column] ?? ""),
          ...sourceContext,
        ]),
      };
    });
}

function encodeDelimitedRows(rows, delimiter) {
  return rows.map((row) => row.map((value) => {
    const text = String(value ?? "");
    return /["\r\n\t,;]/.test(text)
      ? `"${text.replaceAll('"', '""')}"`
      : text;
  }).join(delimiter)).join("\r\n");
}

export function serializeTablesForCopy({ tables, sourceName, pageNumber, extraColumns, extraValues } = {}) {
  return tableExportBlocks(tables, sourceName, pageNumber, extraColumns, extraValues, false)
    .map((block) => encodeDelimitedRows([block.header, ...block.rows], "\t"))
    .join("\r\n\r\n");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function serializeTablesForHtml(options = {}) {
  const { tables, sourceName, pageNumber, extraColumns, extraValues } = options;
  const blocks = tableExportBlocks(tables, sourceName, pageNumber, extraColumns, extraValues, false);
  if (blocks.length === 0) return "";
  const columnCount = Math.max(...blocks.map((block) => block.header.length));
  const normalizeCell = (value) => String(value ?? "").replace(/[\r\n\t]+/g, " ").replace(/\s{2,}/g, " ").trim();
  const columnWeights = Array.from({ length: columnCount }, (_, column) => {
    const lengths = blocks.flatMap((block) => [block.header, ...block.rows]
      .map((row) => normalizeCell(row[column]).length));
    return Math.max(10, Math.min(96, Math.max(0, ...lengths)));
  });
  // Excel may ignore <colgroup> sizing when rich HTML is pasted from a browser.
  // Repeat the requested width on each cell and avoid fixed table layout so the
  // pasted columns follow their contents instead of collapsing to narrow widths.
  const columnWidths = columnWeights.map((weight) => Math.max(80, Math.min(864, Math.round(weight * 9))));
  const totalTableWidth = columnWidths.reduce((sum, width) => sum + width, 0);
  const cellStyle = "border:1px solid #b8cbe5;padding:5px 8px;vertical-align:top;";
  const columns = columnWidths.map((width) => {
    // Excel's HTML clipboard importer understands mso-width-alt (1/256 of a
    // character unit) more consistently than CSS pixel widths alone.
    const excelWidth = Math.round(width / 7 * 256);
    return `<col width="${width}" style="width:${width}px;mso-width-source:userset;mso-width-alt:${excelWidth};">`;
  }).join("");
  const paddedCells = (row, tag, style) => Array.from({ length: columnCount }, (_, column) => {
    const value = normalizeCell(row[column]);
    const width = columnWidths[column];
    return `<${tag} width="${width}" style="${style}width:${width}px;min-width:${width}px;mso-width-source:userset;mso-width-alt:${Math.round(width / 7 * 256)};">${escapeHtml(value)}</${tag}>`;
  }).join("");
  const body = blocks.map(({ header, rows }, index) => {
    const spacer = index > 0
      ? `<tr><td colspan="${columnCount}" style="${cellStyle}height:8px;border:0;">&nbsp;</td></tr>`
      : "";
    const headerRow = `<tr>${paddedCells(header, "th", `${cellStyle}background-color:#edf3fb;font-weight:bold;`)}</tr>`;
    const dataRows = rows.map((row) => `<tr>${paddedCells(row, "td", `${cellStyle}white-space:normal;word-break:normal;overflow-wrap:normal;`)}</tr>`).join("");
    return `${spacer}${headerRow}${dataRows}`;
  }).join("");
  return `<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel"><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"><meta name="ProgId" content="Excel.Sheet"><style>table{width:${totalTableWidth}px;table-layout:auto;border-collapse:collapse;}td,th{mso-wrap-style: no-wrap;}</style></head><body><table border="1" cellspacing="0" cellpadding="4" style="width:${totalTableWidth}px;table-layout:auto;border-collapse:collapse;border:1px solid #b8cbe5;"><colgroup>${columns}</colgroup><tbody>${body}</tbody></table></body></html>`;
}

export function serializeTablesForCsv({
  tables, sourceName, pageNumber, extraColumns, extraValues, includeSourceColumns = true,
} = {}) {
  const content = tableExportBlocks(
    tables, sourceName, pageNumber, extraColumns, extraValues, includeSourceColumns,
  )
    .map((block) => encodeDelimitedRows([block.header, ...block.rows], ","))
    .join("\r\n\r\n");
  return content ? `\uFEFF${content}` : "";
}

export function addExcelCsvSeparatorDirective(csv, enabled = true) {
  if (!enabled || !csv) return csv;
  return csv.replace(/^\uFEFF/, "\uFEFFsep=,\r\n");
}

function copyRichHtmlWithSelection(html) {
  if (!html || !globalThis.document?.body || typeof document.execCommand !== "function") return false;
  const container = document.createElement("div");
  container.contentEditable = "true";
  container.setAttribute("aria-hidden", "true");
  container.style.position = "fixed";
  container.style.left = "-10000px";
  container.style.top = "0";
  container.style.width = "max-content";
  container.innerHTML = html;
  document.body.append(container);
  const selection = globalThis.getSelection?.();
  const range = document.createRange?.();
  if (!selection || !range) {
    container.remove();
    return false;
  }
  try {
    range.selectNodeContents(container);
    selection.removeAllRanges();
    selection.addRange(range);
    return Boolean(document.execCommand("copy"));
  } catch {
    return false;
  } finally {
    selection.removeAllRanges();
    container.remove();
  }
}

export async function copyTablesToClipboard(options = {}) {
  const text = serializeTablesForCopy(options);
  if (!text) throw new Error("Tidak ada tabel untuk disalin.");
  const html = serializeTablesForHtml(options);
  if (navigator.clipboard?.write && typeof ClipboardItem === "function") {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }),
      })]);
      return;
    } catch {
      // Some browsers expose rich clipboard APIs but block HTML clipboard writes.
      // Try the browser's native rich-copy path before falling back to plain TSV.
    }
  }
  if (copyRichHtmlWithSelection(html)) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const temporary = document.createElement("textarea");
  temporary.value = text;
  temporary.style.position = "fixed";
  temporary.style.opacity = "0";
  document.body.append(temporary);
  temporary.select();
  const copied = document.execCommand("copy");
  temporary.remove();
  if (!copied) throw new Error("Browser tidak mengizinkan penyalinan tabel.");
}

export function downloadTablesAsCsv({
  tables, sourceName, pageNumber, filename, extraColumns, extraValues,
  includeSourceColumns = true, excelSeparatorDirective = true,
} = {}) {
  const csv = serializeTablesForCsv({
    tables, sourceName, pageNumber, extraColumns, extraValues, includeSourceColumns,
  });
  if (!csv) throw new Error("Tidak ada tabel untuk diunduh.");
  // Excel on Windows commonly follows the system list separator instead of
  // recognizing comma-delimited UTF-8 CSV. This directive tells Excel to split
  // the file into columns while keeping the CSV payload itself RFC-style.
  const downloadContent = addExcelCsvSeparatorDirective(csv, excelSeparatorDirective);
  const blob = new Blob([downloadContent], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || `${(sourceName || "docuflow").replace(/\.pdf$/i, "")}.csv`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function xmlEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function excelColumnName(index) {
  let value = index + 1;
  let name = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    name = String.fromCharCode(65 + remainder) + name;
    value = Math.floor((value - 1) / 26);
  }
  return name;
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function concatBytes(chunks) {
  const size = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const output = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.length;
  }
  return output;
}

function littleEndianRecord(byteLength, fields) {
  const bytes = new Uint8Array(byteLength);
  const view = new DataView(bytes.buffer);
  for (const [offset, type, value] of fields) view[type](offset, value, true);
  return bytes;
}

function createStoredZip(files) {
  const encoder = new TextEncoder();
  const localChunks = [];
  const centralChunks = [];
  let localOffset = 0;

  for (const [path, content] of files) {
    const name = encoder.encode(path);
    const data = encoder.encode(content);
    const checksum = crc32(data);
    const localHeader = littleEndianRecord(30, [
      [0, "setUint32", 0x04034b50], [4, "setUint16", 20], [6, "setUint16", 0x0800],
      [8, "setUint16", 0], [10, "setUint16", 0], [12, "setUint16", 0],
      [14, "setUint32", checksum], [18, "setUint32", data.length], [22, "setUint32", data.length],
      [26, "setUint16", name.length], [28, "setUint16", 0],
    ]);
    localChunks.push(localHeader, name, data);

    const centralHeader = littleEndianRecord(46, [
      [0, "setUint32", 0x02014b50], [4, "setUint16", 20], [6, "setUint16", 20],
      [8, "setUint16", 0x0800], [10, "setUint16", 0], [12, "setUint16", 0],
      [14, "setUint16", 0], [16, "setUint32", checksum], [20, "setUint32", data.length],
      [24, "setUint32", data.length], [28, "setUint16", name.length], [30, "setUint16", 0],
      [32, "setUint16", 0], [34, "setUint16", 0], [36, "setUint32", 0],
      [38, "setUint32", 0], [42, "setUint32", localOffset],
    ]);
    centralChunks.push(centralHeader, name);
    localOffset += localHeader.length + name.length + data.length;
  }

  const centralDirectory = concatBytes(centralChunks);
  const endRecord = littleEndianRecord(22, [
    [0, "setUint32", 0x06054b50], [4, "setUint16", 0], [6, "setUint16", 0],
    [8, "setUint16", files.length], [10, "setUint16", files.length],
    [12, "setUint32", centralDirectory.length], [16, "setUint32", localOffset], [20, "setUint16", 0],
  ]);
  return concatBytes([...localChunks, centralDirectory, endRecord]);
}

function buildWorkbookXml() {
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="DocuFlow" sheetId="1" r:id="rId1"/></sheets></workbook>`;
}

const xlsxStylesXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
  `<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FF17365D"/><sz val="11"/><name val="Calibri"/></font></fonts>` +
  `<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEAF2FC"/><bgColor indexed="64"/></patternFill></fill></fills>` +
  `<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border>` +
  `<left style="thin"><color rgb="FFB8CBE5"/></left><right style="thin"><color rgb="FFB8CBE5"/></right>` +
  `<top style="thin"><color rgb="FFB8CBE5"/></top><bottom style="thin"><color rgb="FFB8CBE5"/></bottom><diagonal/>` +
  `</border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>` +
  `<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>` +
  `<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>` +
  `<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>` +
  `</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`;

export function buildXlsxWorkbook({
  tables, sourceName, pageNumber, extraColumns, extraValues, includeSourceColumns = true,
} = {}) {
  const blocks = tableExportBlocks(tables, sourceName, pageNumber, extraColumns, extraValues, includeSourceColumns);
  if (blocks.length === 0) return null;
  const columnCount = Math.max(1, ...blocks.map((block) => Math.max(block.header.length, ...block.rows.map((row) => row.length))));
  const allRows = [];
  blocks.forEach((block, blockIndex) => {
    if (blockIndex > 0) allRows.push(null);
    allRows.push({ values: block.header, style: 1 });
    block.rows.forEach((values) => allRows.push({ values, style: 2 }));
  });
  const widths = Array.from({ length: columnCount }, (_, column) => {
    const longest = allRows.reduce((max, row) => {
      if (!row) return max;
      const text = String(row.values[column] ?? "");
      return Math.max(max, ...text.split(/\r?\n/).map((line) => line.length));
    }, 0);
    return Math.max(10, Math.min(60, longest + 2));
  });
  const columnsXml = widths.map((width, index) =>
    `<col min="${index + 1}" max="${index + 1}" width="${width}" customWidth="1"/>`
  ).join("");
  let excelRow = 0;
  const rowsXml = allRows.map((row) => {
    excelRow += 1;
    if (!row) return `<row r="${excelRow}"/>`;
    const cells = Array.from({ length: columnCount }, (_, column) => {
      const address = `${excelColumnName(column)}${excelRow}`;
      const value = xmlEscape(row.values[column] ?? "");
      return `<c r="${address}" s="${row.style}" t="inlineStr"><is><t xml:space="preserve">${value}</t></is></c>`;
    }).join("");
    return `<row r="${excelRow}">${cells}</row>`;
  }).join("");
  const finalRef = `${excelColumnName(columnCount - 1)}${Math.max(1, excelRow)}`;
  const worksheetXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:${finalRef}"/>` +
    `<sheetViews><sheetView showGridLines="0" workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="18"/>` +
    `<cols>${columnsXml}</cols><sheetData>${rowsXml}</sheetData><pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>` +
    `</worksheet>`;
  const contentTypesXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
    `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
    `<Default Extension="xml" ContentType="application/xml"/>` +
    `<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
    `<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>` +
    `<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>` +
    `</Types>`;
  const packageRelsXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`;
  const workbookRelsXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
    `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>` +
    `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>` +
    `</Relationships>`;
  const files = [
    ["[Content_Types].xml", contentTypesXml],
    ["_rels/.rels", packageRelsXml],
    ["xl/workbook.xml", buildWorkbookXml()],
    ["xl/_rels/workbook.xml.rels", workbookRelsXml],
    ["xl/styles.xml", xlsxStylesXml],
    ["xl/worksheets/sheet1.xml", worksheetXml],
  ];
  return createStoredZip(files);
}

export function downloadTablesAsXlsx({
  tables, sourceName, pageNumber, filename, extraColumns, extraValues,
  includeSourceColumns = true,
} = {}) {
  const xlsx = buildXlsxWorkbook({ tables, sourceName, pageNumber, extraColumns, extraValues, includeSourceColumns });
  if (!xlsx) throw new Error("Tidak ada tabel untuk diunduh.");
  const blob = new Blob([xlsx], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || `${(sourceName || "docuflow").replace(/\.pdf$/i, "")}.xlsx`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
