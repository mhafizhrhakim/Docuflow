import { createPdfViewer } from "./pdf-viewer.js";
import { inspectPdfDocument } from "./api.js";
import { pageHasNativeText } from "./page-classification.mjs";
import { runBounded } from "./concurrency.js";
import { aggregatePageProgress, calculatePageProgress, getProgressPercentage } from "./progress.js";
import { createElapsedTimer } from "./elapsed-timer.js";
import {
  copyTablesToClipboard,
  downloadTablesAsCsv,
  downloadTablesAsXlsx,
  renderDetectedTables,
  showExtractionPending,
} from "./extraction-table.js";

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function makeButton(label, className, ariaLabel) {
  const button = createElement("button", className, label);
  button.type = "button";
  if (ariaLabel) button.setAttribute("aria-label", ariaLabel);
  return button;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function createDocumentCard(file, index, { onState = () => {} } = {}) {
  const article = createElement("article", "panel multi-document-card");
  article.dataset.documentIndex = String(index);

  const header = createElement("div", "multi-card-heading");
  const identity = createElement("div", "multi-card-identity");
  const badge = createElement("span", "file-badge", "PDF");
  const fileInfo = createElement("div", "multi-card-file-info");
  const fileName = createElement("strong", "multi-card-file-name", file.name);
  fileName.title = file.name;
  const fileMeta = createElement("span", "multi-card-file-meta", `${formatBytes(file.size)} · membaca PDF…`);
  fileInfo.append(fileName, fileMeta);
  identity.append(badge, fileInfo);
  const classificationBadge = createElement("span", "category-badge multi-classification-badge", "Belum diklasifikasi");
  header.append(identity, classificationBadge);

  const toolbar = createElement("div", "multi-card-toolbar");
  const previousButton = makeButton("←", "toolbar-button", "Halaman sebelumnya");
  previousButton.disabled = true;
  const pageLabel = createElement("span", "multi-page-label", "Halaman 1 / —");
  const nextButton = makeButton("→", "toolbar-button", "Halaman berikutnya");
  nextButton.disabled = true;
  const spacer = createElement("span", "toolbar-spacer");
  const zoomOutButton = makeButton("−", "toolbar-button zoom-button", "Perkecil");
  zoomOutButton.disabled = true;
  const zoomLabel = createElement("span", "zoom-label", "100%");
  const zoomInButton = makeButton("+", "toolbar-button zoom-button", "Perbesar");
  zoomInButton.disabled = true;
  toolbar.append(previousButton, pageLabel, nextButton, spacer, zoomOutButton, zoomLabel, zoomInButton);

  const status = createElement("div", "viewer-status multi-card-status", "Menyiapkan pratinjau PDF…");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const progressPanel = createElement("div", "multi-card-progress");
  const progressCopy = createElement("div", "multi-card-progress-copy");
  const progressSpinner = createElement("span", "multi-progress-spinner");
  progressSpinner.setAttribute("aria-hidden", "true");
  const progressPhase = createElement("strong", "multi-progress-phase", "Menunggu");
  const progressElapsed = createElement("span", "multi-progress-elapsed", "Durasi 00:00");
  progressElapsed.setAttribute("aria-live", "off");
  const progressPercent = createElement("span", "multi-progress-percent", "—");
  progressCopy.append(progressSpinner, progressPhase, progressElapsed, progressPercent);
  const progressBar = document.createElement("progress");
  progressBar.className = "multi-progress-bar";
  progressBar.max = 100;
  progressBar.value = 0;
  progressBar.setAttribute("aria-label", `Progres ekstraksi ${file.name}`);
  progressPanel.append(progressCopy, progressBar);
  const extractionTimer = createElapsedTimer(progressElapsed);

  const viewerContainer = createElement("div", "multi-pdf-viewer-container");
  const empty = createElement("div", "empty-preview multi-card-empty");
  empty.append(
    createElement("div", "empty-document-icon", "PDF"),
    createElement("strong", "", "Membuka dokumen…"),
    createElement("p", "", "Pratinjau diproses secara lokal di browser.")
  );
  const canvas = createElement("canvas", "multi-pdf-canvas");
  canvas.hidden = true;
  canvas.setAttribute("aria-label", `Pratinjau ${file.name}`);
  viewerContainer.append(empty, canvas);

  const extractionSection = createElement("section", "multi-card-extraction");
  const extractionHeader = createElement("div", "multi-card-extraction-heading");
  const extractionTitle = createElement("h3", "", "Tabel ekstraksi");
  const extractionSubtitle = createElement("p", "", "Halaman 1 · belum dianalisis");
  extractionHeader.append(extractionTitle, extractionSubtitle);

  const extractionActions = createElement("div", "multi-extraction-actions");
  const searchLabel = createElement("label", "search-box");
  const searchIcon = createElement("span", "", "⌕");
  const searchInput = document.createElement("input");
  searchInput.type = "search";
  searchInput.placeholder = "Cari dalam tabel...";
  searchInput.setAttribute("aria-label", `Cari tabel ${file.name}`);
  searchInput.disabled = true;
  searchLabel.append(searchIcon, searchInput);
  const copyButton = makeButton("Salin halaman", "", `Salin tabel halaman aktif dari ${file.name}`);
  const exportMenu = createElement("details", "table-export-menu");
  const exportMenuSummary = createElement("summary", "button button-small", "⇩ Ekspor");
  exportMenuSummary.title = "Pilih format unduhan";
  const exportMenuOptions = createElement("div", "table-export-menu-options");
  const csvButton = makeButton("CSV", "button button-small", `Unduh tabel halaman aktif dari ${file.name} sebagai CSV`);
  const xlsxButton = makeButton("Excel (.xlsx)", "button button-small", `Unduh tabel halaman aktif dari ${file.name} sebagai Excel bergaris`);
  exportMenuOptions.append(csvButton, xlsxButton);
  exportMenu.append(exportMenuSummary, exportMenuOptions);
  copyButton.disabled = true;
  csvButton.disabled = true;
  xlsxButton.disabled = true;
  extractionActions.append(searchLabel, copyButton, exportMenu);
  const tableWrap = createElement("div", "data-table-wrap multi-data-table-wrap");
  const footer = createElement("div", "table-footnote", `Sumber: ${file.name} · halaman 1`);
  extractionSection.append(extractionHeader, extractionActions, tableWrap, footer);

  article.append(header, toolbar, status, progressPanel, viewerContainer, extractionSection);

  showExtractionPending({
    container: tableWrap,
    subtitle: extractionSubtitle,
    footer,
    pageNumber: 1,
    sourceName: file.name,
    context: "halaman",
  });

  let currentPage = 1;
  let inspectionResult = null;
  let cardState = "pending";
  let abortController = null;
  let previewLoaded = false;
  let progressSnapshot = {
    phase: "waiting",
    status: "waiting",
    completed_pages: 0,
    total_pages: 0,
    current_page: null,
    failed_pages: 0,
  };
  const rowSelectionState = new Map();
  const pageResults = () => (Array.isArray(inspectionResult?.pages) ? inspectionResult.pages : []);
  const getPageResult = () => pageResults().find((page) => Number(page.page_number) === currentPage) ?? null;
  const hasTables = (page) => Array.isArray(page?.tables) && page.tables.some((table) =>
    table?.is_source_table !== false && Array.isArray(table?.rows) && table.rows.length > 0
  );
  const setCardState = (state, message) => {
    cardState = state;
    article.dataset.processingState = state;
    status.textContent = message;
    onState({ state, file, result: inspectionResult, progress: { ...progressSnapshot } });
  };
  const updateProgress = (next = {}) => {
    progressSnapshot = {
      ...progressSnapshot,
      ...next,
      total_pages: next.total_pages ?? progressSnapshot.total_pages,
    };
    const pageProgress = calculatePageProgress(progressSnapshot.completed_pages, progressSnapshot.total_pages);
    const total = pageProgress.total_pages;
    const percentage = getProgressPercentage(progressSnapshot);
    progressBar.value = percentage;
    progressPercent.textContent = total > 0 ? `${percentage}%` : "—";
    progressPanel.classList.toggle("is-active", progressSnapshot.status === "processing");
    const failedPages = Number(progressSnapshot.failed_pages) || 0;
    if (progressSnapshot.status === "failed" || progressSnapshot.status === "partial_failure") {
      progressPhase.textContent = failedPages > 0 ? `Gagal · ${failedPages} halaman bermasalah` : "Gagal";
    } else if (progressSnapshot.phase === "reading_pdf") {
      progressPhase.textContent = "Membaca PDF";
    } else if (progressSnapshot.phase === "classifying_page") {
      const current = Number(progressSnapshot.current_page);
      progressPhase.textContent = current > 0 && total > 0
        ? `Mengklasifikasikan halaman ${current}/${total}`
        : "Mengklasifikasikan halaman";
    } else if (progressSnapshot.phase === "preparing_ocr") {
      progressPhase.textContent = "Menyiapkan halaman scan untuk OCR";
    } else if (progressSnapshot.phase === "ocr_inference") {
      const current = Number(progressSnapshot.current_page);
      const ocrDone = Number(progressSnapshot.ocr_pages_processed) || 0;
      const ocrTotal = Number(progressSnapshot.ocr_pages_total) || 0;
      const pageText = current > 0 && total > 0 ? ` halaman ${current}/${total}` : "";
      const failureText = failedPages > 0 ? ` · ${failedPages} gagal` : "";
      progressPhase.textContent = `Membaca OCR${pageText}${ocrTotal ? ` · ${ocrDone}/${ocrTotal} scan diproses` : ""}${failureText}`;
    } else if (progressSnapshot.phase === "reconstructing_tables") {
      progressPhase.textContent = "Menyusun tabel hasil ekstraksi";
    } else if (progressSnapshot.phase === "extracting_page") {
      progressPhase.textContent = progressSnapshot.current_page && total
        ? `Mengekstrak halaman ${progressSnapshot.current_page}/${total}`
        : "Mengekstrak halaman";
    } else if (progressSnapshot.phase === "validating_tables") {
      progressPhase.textContent = "Memvalidasi tabel";
    } else if (progressSnapshot.status === "completed") {
      progressPhase.textContent = Number(progressSnapshot.review_pages) > 0 ? "Selesai · perlu ditinjau" : "Berhasil";
    } else {
      progressPhase.textContent = "Menunggu";
    }
    onState({ state: cardState, file, result: inspectionResult, progress: { ...progressSnapshot } });
  };
  const renderCurrentPage = () => {
    const page = getPageResult();
    const resultPages = pageResults();
    if (!inspectionResult || !page) {
      copyButton.disabled = true;
      csvButton.disabled = true;
      xlsxButton.disabled = true;
      searchInput.disabled = true;
      showExtractionPending({
        container: tableWrap,
        subtitle: extractionSubtitle,
        footer,
        pageNumber: currentPage,
        sourceName: file.name,
        context: "halaman",
      });
      return;
    }
    const scan = page.ocr_applied || ["scan_candidate", "scan_with_text_layer"].includes(page.source_candidate);
    classificationBadge.textContent = scan ? "Scan · OCR"
      : page.source_candidate === "mixed_candidate" ? "Campuran · native"
        : pageHasNativeText(page) ? "Digital · native" : "Belum diklasifikasikan";
    renderDetectedTables({
      container: tableWrap,
      tables: (page.tables ?? []).map((table) => ({ ...table, source_page: page.page_number })),
      metadataItems: page.metadata_items ?? [],
      summaryItems: page.summary_items ?? [],
      status: page.table_extraction_status,
      pageNumber: page.page_number,
      sourceName: file.name,
      searchText: searchInput.value,
      selectionState: rowSelectionState,
      subtitle: extractionSubtitle,
      footer,
    });
    copyButton.disabled = !hasTables(page);
    csvButton.disabled = !hasTables(page);
    xlsxButton.disabled = !hasTables(page);
    searchInput.disabled = !hasTables(page);
    fileMeta.textContent = `${formatBytes(file.size)} · ${resultPages.length} halaman · ${inspectionResult.ocr_status ?? "selesai"}`;
  };
  searchInput.addEventListener("input", renderCurrentPage);
  copyButton.addEventListener("click", async () => {
    const page = getPageResult();
    if (!hasTables(page)) return;
    try {
      await copyTablesToClipboard({ tables: page.tables, sourceName: file.name, pageNumber: currentPage });
      status.textContent = `Tabel halaman ${currentPage} disalin.`;
    } catch (error) {
      status.textContent = error?.message || "Tabel gagal disalin.";
    }
  });
  csvButton.addEventListener("click", () => {
    exportMenu.open = false;
    const page = getPageResult();
    if (!hasTables(page)) return;
    try {
      downloadTablesAsCsv({
        tables: page.tables,
        sourceName: file.name,
        pageNumber: currentPage,
        includeSourceColumns: false,
        filename: `${file.name.replace(/\.pdf$/i, "")}-halaman-${currentPage}.csv`,
      });
      status.textContent = `CSV halaman ${currentPage} diunduh.`;
    } catch (error) {
      status.textContent = error?.message || "CSV gagal dibuat.";
    }
  });
  xlsxButton.addEventListener("click", () => {
    exportMenu.open = false;
    const page = getPageResult();
    if (!hasTables(page)) return;
    try {
      downloadTablesAsXlsx({
        tables: page.tables,
        sourceName: file.name,
        pageNumber: currentPage,
        includeSourceColumns: false,
        filename: `${file.name.replace(/\.pdf$/i, "")}-halaman-${currentPage}.xlsx`,
      });
      status.textContent = `Excel halaman ${currentPage} diunduh.`;
    } catch (error) {
      status.textContent = error?.message || "Workbook Excel gagal dibuat.";
    }
  });
  const pageLabelProxy = {
    set textContent(value) {
      const match = String(value).match(/Page\s+(\d+)\s+of\s+(\d+)/i);
      pageLabel.textContent = match ? `Halaman ${match[1]} / ${match[2]}` : String(value);
    },
  };

  const viewer = createPdfViewer({
    canvas,
    container: viewerContainer,
    pageLabel: pageLabelProxy,
    prevButton: previousButton,
    nextButton,
    zoomInButton,
    zoomOutButton,
    onStatus: (message) => {
      status.textContent = message;
    },
    onPageChange: ({ page, totalPages }) => {
      currentPage = page;
      pageLabel.textContent = `Halaman ${page} / ${totalPages}`;
      fileMeta.textContent = `${formatBytes(file.size)} · ${totalPages} halaman`;
      renderCurrentPage();
    },
    onZoomChange: (zoom) => {
      zoomLabel.textContent = `${zoom}%`;
      zoomInButton.disabled = viewer.getPageCount() === 0 || zoom >= 250;
      zoomOutButton.disabled = viewer.getPageCount() === 0 || zoom <= 50;
    },
  });

  async function preparePreview({ isCurrent = () => true } = {}) {
    empty.hidden = true;
    canvas.hidden = false;
    status.textContent = `Membuka ${file.name}…`;
    updateProgress({ phase: "reading_pdf", status: "processing", completed_pages: 0, total_pages: 0 });
    try {
      const totalPages = await viewer.loadFile(file);
      if (!isCurrent()) return false;
      previewLoaded = true;
      classificationBadge.textContent = "Belum diklasifikasi";
      fileMeta.textContent = `${formatBytes(file.size)} · ${totalPages} halaman · menunggu analisis`;
      updateProgress({ phase: "waiting", status: "waiting", completed_pages: 0, total_pages: totalPages });
      setCardState("waiting", `Menunggu · ${file.name} siap diproses`);
      renderCurrentPage();
      return true;
    } catch (error) {
      if (!isCurrent()) return false;
      canvas.hidden = true;
      empty.hidden = false;
      empty.querySelector("strong").textContent = "PDF tidak dapat dibuka";
      empty.querySelector("p").textContent = "Pastikan file valid dan tidak dilindungi kata sandi.";
      updateProgress({ phase: "failed", status: "failed", total_pages: 0 });
      setCardState("failed", `Gagal membaca ${file.name}: ${error?.message ?? "kesalahan tidak diketahui"}`);
      classificationBadge.textContent = "Gagal membuka PDF";
      return false;
    }
  }

  async function processDocument({ isCurrent = () => true } = {}) {
    if (!previewLoaded || !isCurrent()) return;
    extractionTimer.reset();
    extractionTimer.start();
    updateProgress({ phase: "reading_pdf", status: "processing" });
    setCardState("processing", `Membaca PDF · ${file.name}`);
    renderCurrentPage();
    abortController = new AbortController();
    try {
      inspectionResult = await inspectPdfDocument(file, {
        ocr: true,
        signal: abortController.signal,
        onProgress: updateProgress,
      });
      if (!isCurrent()) return;
      const pages = pageResults();
      const failedStatuses = new Set(["ocr_failed", "detection_error", "extraction_error", "ocr_pending", "awaiting_ocr"]);
      const failedPages = pages.filter((page) => failedStatuses.has(page.table_extraction_status));
      const reviewPages = pages.filter((page) => (page.tables ?? []).some((table) => table.review_required));
      const scanned = pages.filter((page) => page.ocr_applied).length;
      const resultFailed = failedPages.length > 0;
      extractionTimer.stop();
      updateProgress({
        phase: resultFailed ? "failed" : "completed",
        status: resultFailed ? "partial_failure" : "completed",
        completed_pages: pages.length,
        total_pages: pages.length,
        failed_pages: failedPages.length,
        review_pages: reviewPages.length,
      });
      setCardState(
        resultFailed ? "failed" : reviewPages.length ? "needs_review" : "completed",
        failedPages.length
          ? `Tidak selesai pada ${failedPages.length} halaman · periksa hasil dan pesan halaman aktif`
          : reviewPages.length
            ? `Selesai · ${scanned} halaman scan dibaca · perlu ditinjau di ${reviewPages.length} halaman`
            : `Selesai · ${pages.length} halaman diperiksa · ${scanned} halaman scan dibaca OCR`,
      );
      renderCurrentPage();
    } catch (error) {
      if (!isCurrent()) return;
      extractionTimer.stop();
      updateProgress({ phase: "failed", status: "failed" });
      copyButton.disabled = true;
      csvButton.disabled = true;
      xlsxButton.disabled = true;
      searchInput.disabled = true;
      extractionSubtitle.textContent = `Halaman ${currentPage} · ekstraksi gagal`;
      tableWrap.replaceChildren(createElement(
        "div",
        "extraction-empty-state",
        `Hasil ekstraksi tidak tersedia: ${error?.message ?? "kesalahan tidak diketahui"}`,
      ));
      footer.textContent = `Sumber: ${file.name} · halaman ${currentPage}. Preview tetap mengacu pada PDF ini.`;
      setCardState("failed", `Gagal memproses ${file.name}: ${error?.message ?? "kesalahan tidak diketahui"}`);
      classificationBadge.textContent = "Ekstraksi gagal";
    }
  }

  return {
    element: article,
    preparePreview,
    processDocument,
    clear: async () => {
      abortController?.abort();
      extractionTimer.stop();
      await viewer.clear();
    },
    file,
    getResult: () => inspectionResult,
    getState: () => cardState,
    getProgress: () => ({ ...progressSnapshot }),
  };
}

export function createMultiDocumentWorkflow({
  container,
  emptyState,
  statusElement,
  combinedExportButton,
  combinedXlsxExportButton,
  overallProgressContainer,
  overallProgressBar,
  overallProgressLabel,
  overallElapsedLabel,
  onDocumentInfo = () => {},
} = {}) {
  let cards = [];
  let selectionToken = 0;
  const overallElapsedTimer = createElapsedTimer(overallElapsedLabel);

  function refreshOverallProgress() {
    if (!cards.length) {
      if (overallProgressContainer) {
        overallProgressContainer.hidden = true;
        overallProgressContainer.classList.remove("is-active");
      }
      if (overallProgressBar) overallProgressBar.value = 0;
      if (overallProgressLabel) overallProgressLabel.textContent = "Menunggu dokumen";
      return;
    }
    if (overallProgressContainer) overallProgressContainer.hidden = false;
    if (overallProgressContainer) overallProgressContainer.classList.toggle(
      "is-active",
      cards.some((card) => card.getProgress().status === "processing" || card.getState() === "waiting"),
    );
    const snapshots = cards.map((card) => card.getProgress());
    const aggregate = aggregatePageProgress(snapshots);
    const knownPageCounts = snapshots.filter((progress) => Number(progress.total_pages) > 0).length;
    const countsKnown = aggregate.counts_known;
    const failedDocuments = cards.filter((card) => card.getState() === "failed").length;
    const finishedDocuments = cards.filter((card) => ["completed", "needs_review", "failed"].includes(card.getState())).length;
    const terminal = finishedDocuments === cards.length;
    if (terminal) overallElapsedTimer.stop();
    if (!countsKnown) {
      if (overallProgressBar) overallProgressBar.hidden = true;
      if (overallProgressLabel) {
        overallProgressLabel.textContent = `Membaca PDF · ${knownPageCounts}/${cards.length} dokumen siap${failedDocuments ? ` · ${failedDocuments} gagal` : ""}`;
      }
      statusElement.textContent = `Membaca PDF · ${knownPageCounts}/${cards.length} dokumen siap${failedDocuments ? ` · ${failedDocuments} gagal` : ""}`;
      statusElement.className = failedDocuments ? "status-pill status-error" : "status-pill status-pending";
      return;
    }
    const totalPages = aggregate.total_pages;
    const completedPages = aggregate.completed_pages;
    const percentage = aggregate.percentage ?? 0;
    if (overallProgressBar) {
      overallProgressBar.hidden = false;
      overallProgressBar.value = percentage;
    }
    const active = cards.some((card) => card.getState() === "processing");
    const stage = snapshots.find((progress) => progress.status === "processing")?.phase;
    const stageLabel = stage === "validating_tables" ? "Memvalidasi tabel"
      : stage === "extracting_page" ? "Mengekstrak halaman"
        : stage === "ocr_inference" ? "Membaca OCR"
          : stage === "classifying_page" ? "Mengklasifikasikan halaman"
        : active ? "Membaca PDF" : "Menunggu";
    const reviewDocuments = cards.filter((card) => card.getState() === "needs_review").length;
    const suffix = failedDocuments
      ? ` · ${failedDocuments} gagal`
      : reviewDocuments
        ? ` · ${reviewDocuments} perlu ditinjau`
        : terminal ? " · Berhasil" : ` · ${stageLabel}`;
    const label = aggregate.progress_mode === "processing_work"
      ? `${percentage}% proses · ${completedPages}/${totalPages} halaman selesai${suffix}`
      : `${completedPages}/${totalPages} halaman · ${percentage}%${suffix}`;
    if (overallProgressLabel) overallProgressLabel.textContent = label;
    statusElement.textContent = `${finishedDocuments}/${cards.length} dokumen · ${label}`;
    statusElement.className = failedDocuments
      ? "status-pill status-error"
      : terminal ? "status-pill status-success" : "status-pill status-pending";
  }

  function refreshWorkflowStatus() {
    refreshCombinedExport();
    refreshOverallProgress();
  }

  function refreshCombinedExport() {
    const terminal = cards.length > 0 && cards.every((card) =>
      ["completed", "needs_review", "failed"].includes(card.getState())
    );
    const ready = terminal && cards.some((card) => card.getResult()?.pages?.some((page) =>
      (page.tables ?? []).some((table) => table?.is_source_table !== false && table?.rows?.length)
    ));
    if (combinedExportButton) combinedExportButton.disabled = !ready;
    if (combinedXlsxExportButton) combinedXlsxExportButton.disabled = !ready;
  }

  function getCombinedTables() {
    const tables = [];
    cards.forEach((card) => {
      const result = card.getResult();
      (result?.pages ?? []).forEach((page) => {
        const pageType = page.ocr_applied || ["scan_candidate", "scan_with_text_layer"].includes(page.source_candidate)
          ? "Scan/OCR"
          : page.source_candidate === "mixed_candidate" ? "Campuran/native"
            : pageHasNativeText(page) ? "Digital/native" : "Belum diklasifikasikan";
        (page.tables ?? []).forEach((table) => {
          if (table?.is_source_table === false || !Array.isArray(table?.rows)) return;
          tables.push({
            ...table,
            source_page: page.page_number,
            export_source_name: card.file.name,
            export_page_type: pageType,
            export_document_status: card.getState(),
          });
        });
      });
    });
    return tables;
  }

  combinedExportButton?.addEventListener("click", () => {
    combinedExportButton.closest("details")?.removeAttribute("open");
    const tables = getCombinedTables();
    if (!tables.length) return;
    try {
      downloadTablesAsCsv({
        tables,
        sourceName: "multi-document",
        filename: "docuflow-hasil-gabungan.csv",
        extraColumns: ["Jenis dokumen", "Jenis halaman", "Status ekstraksi"],
        extraValues: (table) => ["Belum diklasifikasikan", table.export_page_type, table.export_document_status],
      });
      const failedWithoutResults = cards.filter((card) => card.getState() === "failed" && !card.getResult());
      statusElement.textContent = failedWithoutResults.length
        ? `CSV hasil tersedia diunduh · ${failedWithoutResults.length} dokumen gagal tanpa hasil tabel`
        : "CSV gabungan seluruh hasil yang tersedia diunduh.";
    } catch (error) {
      statusElement.textContent = error?.message || "CSV gabungan gagal dibuat.";
    }
  });

  combinedXlsxExportButton?.addEventListener("click", () => {
    combinedXlsxExportButton.closest("details")?.removeAttribute("open");
    const tables = getCombinedTables();
    if (!tables.length) return;
    try {
      downloadTablesAsXlsx({
        tables,
        sourceName: "multi-document",
        filename: "docuflow-hasil-gabungan.xlsx",
        extraColumns: ["Jenis dokumen", "Jenis halaman", "Status ekstraksi"],
        extraValues: (table) => ["Belum diklasifikasikan", table.export_page_type, table.export_document_status],
      });
      statusElement.textContent = "Workbook Excel gabungan seluruh hasil yang tersedia diunduh.";
    } catch (error) {
      statusElement.textContent = error?.message || "Workbook Excel gabungan gagal dibuat.";
    }
  });

  async function disposeCards() {
    overallElapsedTimer.stop();
    overallElapsedTimer.reset();
    const oldCards = cards;
    cards = [];
    await Promise.all(oldCards.map((card) => card.clear().catch(() => {})));
    container.replaceChildren();
  }

  async function clear() {
    selectionToken++;
    await disposeCards();
    emptyState.hidden = false;
    statusElement.textContent = "Belum ada dokumen";
    statusElement.className = "status-pill status-pending";
    refreshWorkflowStatus();
    onDocumentInfo("Belum ada dokumen", "Pilih beberapa PDF untuk memulai · file diproses lokal");
  }

  async function handleFiles(fileList) {
    const token = ++selectionToken;
    const incoming = [...(fileList ?? [])];
    const pdfs = incoming.filter((file) =>
      file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")
    );

    await disposeCards();
    if (token !== selectionToken) return;
    refreshWorkflowStatus();

    if (!pdfs.length) {
      emptyState.hidden = false;
      statusElement.textContent = "Tidak ada PDF terpilih";
      statusElement.className = "status-pill status-pending";
      refreshWorkflowStatus();
      onDocumentInfo("Belum ada dokumen", "Pilih file PDF yang valid");
      return;
    }

    emptyState.hidden = true;
    statusElement.textContent = `${pdfs.length} dokumen dipilih`;
    statusElement.className = "status-pill status-pending";
    onDocumentInfo(`${pdfs.length} dokumen dipilih`, `${pdfs.length} kartu · diproses lokal`);

    cards = pdfs.map((file, index) => createDocumentCard(file, index, {
      onState: () => refreshWorkflowStatus(),
    }));
    overallElapsedTimer.start();
    refreshWorkflowStatus();
    const fragment = document.createDocumentFragment();
    cards.forEach((card) => fragment.append(card.element));
    container.append(fragment);

    // Prepare each browser preview first so the overall page denominator is
    // known before backend extraction starts. Run at most two independent
    // document requests at once; the backend still serializes shared OCR-model
    // inference, avoiding duplicate model memory while allowing native work
    // and per-request setup to overlap.
    await Promise.all(cards.map((card) => card.preparePreview({
      isCurrent: () => token === selectionToken,
    })));
    if (token !== selectionToken) return;

    await runBounded(cards, 2, async (card) => {
      if (token !== selectionToken) return;
      await card.processDocument({ isCurrent: () => token === selectionToken });
    });
  }

  return { handleFiles, clear };
}
