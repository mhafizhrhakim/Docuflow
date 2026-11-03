
import { createPdfViewer } from "./pdf-viewer.js";
import { inspectPdfDocument } from "./api.js";
import { isScanPage, pageHasNativeText, pageType } from "./page-classification.mjs";
import { copyTablesToClipboard, downloadTablesAsCsv, renderDetectedTables } from "./extraction-table.js";

const $ = (selector) => document.querySelector(selector);

export function createSinglePdfWorkflow({ onDocumentInfo = () => {} } = {}) {
  const fileInput = $("#pdf-input");
  const documentName = $("#document-name");
  const documentMeta = $("#document-meta");
  const pageChips = $("#page-chips");
  const pageHomeButton = $("#page-home-button");
  const pagePreviousButton = $("#page-previous-button");
  const pageNextButton = $("#page-next-button");
  const pageEndButton = $("#page-end-button");
  const classificationStatus = $("#classification-status");
  const singleProgress = $("#single-progress");
  const singleProgressPhase = $("#single-progress-phase");
  const singleProgressPercent = $("#single-progress-percent");
  const singleProgressBar = $("#single-progress-bar");

  const digitalCount = $("#digital-count");
  const scanCount = $("#scan-count");
  const otherCount = $("#other-count");

  const digitalSubtitle = $("#digital-subtitle");
  const digitalPageBadge = $("#digital-page-badge");
  const digitalItemCount = $("#digital-item-count");
  const digitalEmpty = $("#digital-empty");
  const digitalCanvas = $("#pdf-canvas");
  const digitalTableContainer = $("#digital-table-container");
  const digitalTableSubtitle = $("#digital-table-subtitle");
  const digitalTableFooter = $("#digital-table-footer");

  const scanSubtitle = $("#scan-subtitle");
  const scanPageBadge = $("#scan-page-badge");
  const scanItemCount = $("#scan-item-count");
  const scanEmpty = $("#scan-empty");
  const scanCanvas = $("#scan-pdf-canvas");
  const scanTableContainer = $("#scan-table-container");
  const scanTableSubtitle = $("#scan-table-subtitle");
  const scanTableFooter = $("#scan-table-footer");
  const digitalCopyButton = $("#digital-copy-button");
  const digitalExportButton = $("#digital-export-button");
  const scanCopyButton = $("#scan-copy-button");
  const scanExportButton = $("#scan-export-button");
  const digitalSearch = $("#digital-search");
  const scanSearch = $("#scan-search");

  const digitalViewerContainer = $("#pdf-viewer-container");
  const scanViewerContainer = $("#scan-pdf-viewer-container");

  const digitalStatus = $("#pdf-viewer-status");
  const scanStatus = $("#scan-pdf-viewer-status");

  const digitalPageInput = $("#pdf-page-input");
  const digitalPageTotal = $("#pdf-page-total");
  const digitalZoomLabel = $("#pdf-zoom-label");
  const digitalFitButton = $("#pdf-fit-button");

  const scanPageInput = $("#scan-pdf-page-input");
  const scanPageTotal = $("#scan-pdf-page-total");
  const scanZoomLabel = $("#scan-pdf-zoom-label");
  const scanFitButton = $("#scan-pdf-fit-button");

  let selectedFile = null;
  let selectionToken = 0;
  let analysisAbortController = null;

  let analysisResult = null;
  let analysisState = "not_started";
  let analysisError = null;
  let latestSingleProgress = null;

  function updateProgress(progress, { active = false } = {}) {
    latestSingleProgress = progress;
    if (!progress) {
      singleProgress.hidden = true;
      singleProgress.classList.remove("is-active");
      singleProgressPhase.textContent = "Menunggu";
      singleProgressPercent.textContent = "—";
      singleProgressBar.value = 0;
      singleProgress.removeAttribute("data-state");
      return;
    }

    singleProgress.hidden = false;
    singleProgress.classList.toggle("is-active", active);
    const total = Number(progress.total_pages);
    const completed = Number(progress.completed_pages) || 0;
    const reportedPercentage = Number(progress.progress_percent);
    const percentage = Number.isFinite(reportedPercentage)
      ? Math.max(0, Math.min(100, Math.floor(reportedPercentage)))
      : Number.isFinite(total) && total > 0
        ? Math.min(100, Math.floor((completed / total) * 100))
        : 0;
    singleProgressBar.value = percentage;
    singleProgressPercent.textContent = `${percentage}%`;
    singleProgress.dataset.state = progress.status || "processing";

    const phase = progress.phase;
    if (phase === "reading_pdf") {
      singleProgressPhase.textContent = "Membaca PDF";
    } else if (phase === "classifying_page") {
      const page = Number(progress.current_page);
      singleProgressPhase.textContent = Number.isInteger(page) && total > 0
        ? `Mengklasifikasikan halaman ${page} dari ${total}`
        : "Mengklasifikasikan halaman";
    } else if (phase === "preparing_ocr") {
      singleProgressPhase.textContent = "Menyiapkan halaman scan untuk OCR";
    } else if (phase === "ocr_inference") {
      const page = Number(progress.current_page);
      const pageText = Number.isInteger(page) && total > 0 ? ` ${page} dari ${total}` : "";
      const ocrDone = Number(progress.ocr_pages_processed);
      const ocrTotal = Number(progress.ocr_pages_total);
      const ocrText = Number.isFinite(ocrDone) && Number.isFinite(ocrTotal) && ocrTotal > 0
        ? ` · OCR ${ocrDone}/${ocrTotal} halaman diproses`
        : "";
      const failedText = Number(progress.failed_pages) > 0
        ? ` · ${Number(progress.failed_pages)} gagal`
        : "";
      singleProgressPhase.textContent = `Membaca halaman OCR${pageText}${ocrText}${failedText}`;
    } else if (phase === "reconstructing_tables") {
      singleProgressPhase.textContent = "Menyusun tabel dari hasil halaman";
    } else if (phase === "extracting_page") {
      const page = Number(progress.current_page);
      const pageText = Number.isInteger(page) && total > 0
        ? ` · halaman ${page} dari ${total}`
        : "";
      singleProgressPhase.textContent = `Mengekstrak tabel${pageText}`;
    } else if (phase === "validating_tables") {
      singleProgressPhase.textContent = "Memvalidasi hasil";
    } else if (phase === "completed") {
      singleProgressBar.value = 100;
      singleProgressPercent.textContent = "100%";
      singleProgressPhase.textContent = "Selesai";
    } else if (phase === "partial_failure") {
      singleProgressBar.value = 100;
      singleProgressPercent.textContent = "100%";
      singleProgressPhase.textContent = `Selesai · ${progress.failed_pages || 0} halaman perlu diperiksa`;
    } else if (phase === "failed" && progress.status === "partial_failure") {
      singleProgressBar.value = 100;
      singleProgressPercent.textContent = "100%";
      singleProgressPhase.textContent = `Selesai · ${progress.failed_pages || 0} halaman perlu diperiksa`;
    } else if (phase === "failed") {
      singleProgressPhase.textContent = "Gagal · " + (progress.message || "analisis tidak selesai");
    }
  }

  let allPages = [];
  let digitalPages = [];
  let scanPages = [];
  let otherPages = [];

  let activeDigitalPage = 0;
  let activeScanPage = 0;
  let focusedPage = 0;
  const rowSelectionState = new Map();

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function getPageResult(pageNumber) {
    return allPages.find((page) => page.page_number === pageNumber) ?? null;
  }

  function pageTypeLabel(page) {
    const type = pageType(page);

    if (type === "digital") {
      return page.source_candidate === "mixed_candidate"
        ? "Teks tersedia · gambar juga ada"
        : "Teks dapat diekstrak";
    }

    if (type === "scan") {
      if (page.ocr_applied) return "Scan · hasil OCR";
      if (page.table_extraction_status === "ocr_failed") return "Scan · OCR gagal";
      if (page.source_candidate === "scan_with_text_layer") {
        return "Scan · teks bawaan diabaikan, dibaca ulang dengan OCR";
      }
      return "Scan · OCR sedang berjalan";
    }

    return "Belum dapat diklasifikasikan";
  }

  function setClassificationMessage(message, state = "pending") {
    classificationStatus.textContent = message;
    classificationStatus.className =
      state === "error"
        ? "status-pill status-error"
        : "status-pill status-pending";
  }

  function showPanelMessage(container, message) {
    if (!container) return;

    const element = document.createElement("div");
    element.className = "extraction-empty-state";
    element.setAttribute("role", "status");
    element.textContent = message;

    container.replaceChildren(element);
  }

  function clearTablePanel(container, subtitle, footer, message) {
    showPanelMessage(container, message);

    if (subtitle) subtitle.textContent = "Belum dianalisis";
    if (footer) footer.textContent = message;
  }

  function getTableSummary(page) {
    if (!page) return "Hasil ekstraksi belum tersedia";

    switch (page.table_extraction_status) {
      case "table_found": {
        const tables = Array.isArray(page.tables) ? page.tables : [];
        const sourceTables = tables.filter((table) =>
          table?.is_source_table !== false
          && table?.layout_type !== "document_metadata"
          && table?.table_role !== "document_metadata"
        );
        const itemTables = sourceTables.filter((table) =>
          table?.layout_type !== "document_form"
          && table?.table_role !== "document_header"
        );
        const sourceTableCount = Number.isFinite(Number(page.source_table_count))
          ? Number(page.source_table_count)
          : sourceTables.length;
        const rowCount = itemTables.reduce(
          (total, table) => total + (Number(table.row_count) || table.rows?.length || 0),
          0,
        );

        return `${sourceTableCount} tabel sumber · ${rowCount} baris item`;
      }

      case "no_table_detected":
        return "Tidak ada tabel terdeteksi";

      case "ocr_pending":
        return "OCR sedang berjalan";

      case "ocr_failed":
        return "OCR gagal";

      case "awaiting_ocr":
        return "Menunggu OCR";

      case "detection_error":
      case "extraction_error":
        return "Deteksi tabel gagal";

      case "not_processed":
      default:
        return "Belum dianalisis";
    }
  }

  function renderTableForPage({
    page,
    pageNumber,
    container,
    subtitle,
    footer,
    sourceName,
    searchInput,
  }) {
    if (analysisState === "analyzing") {
      showPanelMessage(container, "Analisis PDF sedang berjalan...");
      subtitle.textContent = `Halaman ${pageNumber} · sedang dianalisis`;
      footer.textContent = "Menunggu hasil analisis backend.";
      return;
    }

    if (analysisState === "failed") {
      showPanelMessage(
        container,
        analysisError || "Analisis PDF gagal.",
      );
      subtitle.textContent = `Halaman ${pageNumber} · analisis gagal`;
      footer.textContent = analysisError || "Analisis PDF gagal.";
      return;
    }

    if (analysisState !== "complete" || !page) {
      showPanelMessage(container, "Hasil ekstraksi belum tersedia.");
      subtitle.textContent = `Halaman ${pageNumber} · belum dianalisis`;
      footer.textContent = "Belum ada hasil analisis untuk halaman ini.";
      return;
    }

    renderDetectedTables({
      container,
      tables: page.tables ?? [],
      // Consume page-level data explicitly. Table-attached copies remain only
      // as a compatibility bridge for older callers, not as the source of truth.
      metadataItems: page.metadata_items ?? [],
      summaryItems: page.summary_items ?? [],
      status: page.table_extraction_status,
      pageNumber,
      sourceName,
      searchText: searchInput?.value ?? "",
      selectionState: rowSelectionState,
      subtitle: null,
      footer,
    });

    if (page.table_extraction_status === "ocr_failed" && page.ocr_error) {
      footer.textContent = `OCR gagal: ${page.ocr_error}`;
    }

    subtitle.textContent =
      `Halaman PDF ${pageNumber} · ${getTableSummary(page)}`;
  }

  function renderDigitalPanel() {
    if (!selectedFile || activeDigitalPage < 1) {
      digitalPageBadge.textContent = "Belum tersedia";
      digitalItemCount.textContent = "Tidak ada halaman digital";
      digitalCopyButton.disabled = true;
      digitalExportButton.disabled = true;
      digitalSearch.disabled = true;
      digitalSubtitle.textContent = "Belum ada halaman untuk ditampilkan";

      if (analysisState === "complete") {
        digitalEmpty.hidden = false;
        digitalCanvas.hidden = true;
        digitalEmpty.querySelector("strong").textContent =
          "Tidak ada halaman digital";
        digitalEmpty.querySelector("p").textContent =
          "Tidak ditemukan halaman dengan teks yang dapat diekstrak langsung.";

        clearTablePanel(
          digitalTableContainer,
          digitalTableSubtitle,
          digitalTableFooter,
          "Tidak ada halaman dalam kelompok digital.",
        );
      }

      return;
    }

    const page = getPageResult(activeDigitalPage);

    if (!page) return;

    digitalPageBadge.textContent = pageTypeLabel(page);
    digitalSubtitle.textContent =
      `Halaman PDF ${activeDigitalPage} dari ${allPages.length}`;

    digitalItemCount.textContent = getTableSummary(page);
    const digitalTables = (page.tables ?? []).filter((table) => table?.is_source_table !== false);
    digitalCopyButton.disabled = digitalTables.length === 0;
    digitalExportButton.disabled = digitalTables.length === 0;
    digitalSearch.disabled = analysisState !== "complete" || digitalTables.length === 0;

    renderTableForPage({
      page,
      pageNumber: activeDigitalPage,
      container: digitalTableContainer,
      subtitle: digitalTableSubtitle,
      footer: digitalTableFooter,
      sourceName: selectedFile.name,
      searchInput: digitalSearch,
    });
  }

  function renderScanPanel() {
    if (!selectedFile || activeScanPage < 1) {
      scanPageBadge.textContent = "Belum tersedia";
      scanItemCount.textContent = "Tidak ada halaman scan";
      scanCopyButton.disabled = true;
      scanExportButton.disabled = true;
      scanSearch.disabled = true;
      scanSubtitle.textContent = "Belum ada halaman scan untuk ditampilkan";

      if (analysisState === "complete") {
        scanEmpty.hidden = false;
        scanCanvas.hidden = true;

        scanEmpty.querySelector("strong").textContent =
          "Tidak ada halaman scan teridentifikasi";
        scanEmpty.querySelector("p").textContent =
          "Semua halaman yang memiliki teks tersedia di kelompok digital, atau tidak ada gambar scan yang terdeteksi.";

        clearTablePanel(
          scanTableContainer,
          scanTableSubtitle,
          scanTableFooter,
          "Tidak ada halaman scan dalam dokumen ini.",
        );

        scanStatus.textContent = "Tidak ada halaman scan yang perlu ditinjau.";
      }

      return;
    }

    const page = getPageResult(activeScanPage);

    if (!page) return;

    scanPageBadge.textContent = pageTypeLabel(page);
    scanSubtitle.textContent =
      `Halaman PDF ${activeScanPage} · urutan scan ${
        scanViewer.getNavigationPages().indexOf(activeScanPage) + 1
      } dari ${scanPages.length}`;

    scanItemCount.textContent = getTableSummary(page);
    const scanTables = (page.tables ?? []).filter((table) => table?.is_source_table !== false);
    scanCopyButton.disabled = scanTables.length === 0;
    scanExportButton.disabled = scanTables.length === 0;
    scanSearch.disabled = analysisState !== "complete" || scanTables.length === 0;

    renderTableForPage({
      page,
      pageNumber: activeScanPage,
      container: scanTableContainer,
      subtitle: scanTableSubtitle,
      footer: scanTableFooter,
      sourceName: selectedFile.name,
      searchInput: scanSearch,
    });

    if (
      page.table_extraction_status === "awaiting_ocr"
      || page.table_extraction_status === "ocr_pending"
    ) {
      scanStatus.textContent =
        `OCR halaman PDF ${activeScanPage} sedang berjalan...`;
    } else if (page.table_extraction_status === "ocr_failed") {
      scanStatus.textContent =
        `OCR gagal: ${page.ocr_error || "penyebab tidak diketahui"}`;
    } else {
      scanStatus.textContent =
        `Preview halaman scan · PDF halaman ${activeScanPage}`;
    }
  }

  function updateActivePageChip() {
    pageChips.querySelectorAll("[data-page]").forEach((button) => {
      const active = Number(button.dataset.page) === focusedPage;

      button.classList.toggle("active", active);

      if (active) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });
    updatePageShortcutButtons();
  }

  function updatePageShortcutButtons() {
    const currentIndex = allPages.findIndex((page) => page.page_number === focusedPage);
    const hasPages = allPages.length > 0;
    pageHomeButton.disabled = !hasPages || currentIndex === 0;
    pagePreviousButton.disabled = !hasPages || currentIndex <= 0;
    pageNextButton.disabled = !hasPages || currentIndex < 0 || currentIndex >= allPages.length - 1;
    pageEndButton.disabled = !hasPages || currentIndex === allPages.length - 1;
  }

  function navigateByPageIndex(index, { focusChip = false } = {}) {
    if (!allPages.length) return;
    const targetIndex = Math.max(0, Math.min(allPages.length - 1, index));
    const targetPage = allPages[targetIndex];
    if (!targetPage) return;
    navigateToSourcePage(targetPage.page_number);
    if (focusChip) {
      const chip = pageChips.querySelector(`[data-page="${targetPage.page_number}"]`);
      chip?.focus({ preventScroll: true });
      chip?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function navigateByOffset(offset) {
    if (!allPages.length) return;
    const currentIndex = allPages.findIndex((page) => page.page_number === focusedPage);
    const baseIndex = currentIndex < 0 ? (offset >= 0 ? -1 : allPages.length) : currentIndex;
    navigateByPageIndex(baseIndex + offset);
  }

  function navigateToSourcePage(pageNumber) {
    focusedPage = pageNumber;
    updateActivePageChip();

    if (digitalPages.includes(pageNumber)) {
      void digitalViewer.goToPage(pageNumber);
      return;
    }

    if (scanPages.includes(pageNumber)) {
      void scanViewer.goToPage(pageNumber);
      return;
    }

    digitalStatus.textContent =
      `Halaman PDF ${pageNumber} belum dapat dimasukkan ke kelompok digital atau scan.`;
  }

  function renderPageChips() {
    pageChips.replaceChildren();

    if (!allPages.length) {
      const placeholder = document.createElement("span");
      placeholder.className = "page-chip-placeholder";
      placeholder.textContent =
        "Nomor halaman akan muncul setelah PDF dianalisis";

      pageChips.append(placeholder);
      updatePageShortcutButtons();
      return;
    }

    allPages.forEach((page) => {
      const button = document.createElement("button");
      const label = pageTypeLabel(page);

      button.type = "button";
      button.className = "page-chip";
      button.textContent = String(page.page_number);
      button.dataset.page = String(page.page_number);
      button.title = `Halaman ${page.page_number} · ${label}`;
      button.setAttribute("aria-label", button.title);

      button.addEventListener("click", () => {
        navigateToSourcePage(page.page_number);
      });

      pageChips.append(button);
    });

    updateActivePageChip();
  }

  pageHomeButton.addEventListener("click", () => navigateByPageIndex(0));
  pagePreviousButton.addEventListener("click", () => navigateByOffset(-1));
  pageNextButton.addEventListener("click", () => navigateByOffset(1));
  pageEndButton.addEventListener("click", () => navigateByPageIndex(allPages.length - 1));

  pageChips.addEventListener("keydown", (event) => {
    if (!allPages.length) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      const index = allPages.findIndex((page) => page.page_number === focusedPage);
      navigateByPageIndex(Math.max(0, index - 1), { focusChip: true });
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      const index = allPages.findIndex((page) => page.page_number === focusedPage);
      navigateByPageIndex(Math.min(allPages.length - 1, index + 1), { focusChip: true });
    } else if (event.key === "Home") {
      event.preventDefault();
      navigateByPageIndex(0, { focusChip: true });
    } else if (event.key === "End") {
      event.preventDefault();
      navigateByPageIndex(allPages.length - 1, { focusChip: true });
    }
  });

  function updateClassificationCounts() {
    digitalCount.textContent = String(digitalPages.length);
    scanCount.textContent = String(scanPages.length);
    otherCount.textContent = String(otherPages.length);
  }

  function updateSequenceInput({
    page,
    sequencePage,
    navigationCount,
    input,
    totalLabel,
  }) {
    if (!page || !sequencePage || navigationCount < 1) {
      input.value = "1";
      input.max = "1";
      input.disabled = true;
      totalLabel.textContent = "/ —";
      return;
    }

    input.value = String(sequencePage);
    input.max = String(navigationCount);
    input.disabled = false;
    totalLabel.textContent = `/ ${navigationCount}`;
  }

  const digitalViewer = createPdfViewer({
    canvas: digitalCanvas,
    container: digitalViewerContainer,

    // Page numbering is set from the sequence of digital pages below.
    pageLabel: {
      set textContent(_value) {},
    },

    prevButton: $("#pdf-prev"),
    nextButton: $("#pdf-next"),
    zoomInButton: $("#pdf-zoom-in"),
    zoomOutButton: $("#pdf-zoom-out"),

    onStatus: (message) => {
      digitalStatus.textContent = message;
    },

    onPageChange: ({ page, sequencePage, navigationCount }) => {
      if (!page) {
        activeDigitalPage = 0;
        updateSequenceInput({
          page: null,
          sequencePage: null,
          navigationCount: 0,
          input: digitalPageInput,
          totalLabel: digitalPageTotal,
        });
        renderDigitalPanel();
        return;
      }

      activeDigitalPage = page;
      focusedPage = page;

      updateSequenceInput({
        page,
        sequencePage,
        navigationCount,
        input: digitalPageInput,
        totalLabel: digitalPageTotal,
      });

      updateActivePageChip();
      renderDigitalPanel();
    },

    onZoomChange: (zoom) => {
      digitalZoomLabel.textContent = `${zoom}%`;

      const hasPages = digitalViewer.getPageCount() > 0;
      $("#pdf-zoom-in").disabled = !hasPages || zoom >= 250;
      $("#pdf-zoom-out").disabled = !hasPages || zoom <= 50;
      digitalFitButton.disabled = !hasPages;
    },
  });

  const scanViewer = createPdfViewer({
    canvas: scanCanvas,
    container: scanViewerContainer,

    pageLabel: {
      set textContent(_value) {},
    },

    prevButton: $("#scan-pdf-prev"),
    nextButton: $("#scan-pdf-next"),
    zoomInButton: $("#scan-pdf-zoom-in"),
    zoomOutButton: $("#scan-pdf-zoom-out"),

    onStatus: (message) => {
      scanStatus.textContent = message;
    },

    onPageChange: ({ page, sequencePage, navigationCount }) => {
      if (!page) {
        activeScanPage = 0;
        updateSequenceInput({
          page: null,
          sequencePage: null,
          navigationCount: 0,
          input: scanPageInput,
          totalLabel: scanPageTotal,
        });
        renderScanPanel();
        return;
      }

      activeScanPage = page;
      focusedPage = page;

      updateSequenceInput({
        page,
        sequencePage,
        navigationCount,
        input: scanPageInput,
        totalLabel: scanPageTotal,
      });

      updateActivePageChip();
      renderScanPanel();
    },

    onZoomChange: (zoom) => {
      scanZoomLabel.textContent = `${zoom}%`;

      const hasPages = scanViewer.getPageCount() > 0;
      $("#scan-pdf-zoom-in").disabled = !hasPages || zoom >= 250;
      $("#scan-pdf-zoom-out").disabled = !hasPages || zoom <= 50;
      scanFitButton.disabled = !hasPages;
    },
  });

  function resetUi() {
    analysisAbortController?.abort();
    analysisAbortController = null;
    updateProgress(null);
    selectedFile = null;
    analysisResult = null;
    analysisState = "not_started";
    analysisError = null;

    allPages = [];
    digitalPages = [];
    scanPages = [];
    otherPages = [];

    activeDigitalPage = 0;
    activeScanPage = 0;
    focusedPage = 0;
    rowSelectionState.clear();
    digitalSearch.value = "";
    digitalSearch.disabled = true;
    scanSearch.value = "";
    scanSearch.disabled = true;

    documentName.textContent = "Belum ada dokumen";
    documentMeta.textContent =
      "Pilih PDF untuk memulai · file diproses lokal";

    setClassificationMessage("Klasifikasi belum dijalankan");

    digitalCount.textContent = "—";
    scanCount.textContent = "—";
    otherCount.textContent = "—";

    digitalSubtitle.textContent = "Menunggu dokumen";
    digitalPageBadge.textContent = "Belum diklasifikasi";
    digitalItemCount.textContent = "Ekstraksi belum dijalankan";
    digitalCopyButton.disabled = true;
    digitalExportButton.disabled = true;

    scanSubtitle.textContent = "Menunggu klasifikasi per halaman";
    scanPageBadge.textContent = "Belum diketahui";
    scanItemCount.textContent = "Ekstraksi belum dijalankan";
    scanCopyButton.disabled = true;
    scanExportButton.disabled = true;

    digitalEmpty.hidden = false;
    digitalEmpty.querySelector("strong").textContent =
      "Pratinjau halaman digital";
    digitalEmpty.querySelector("p").textContent =
      "Pilih PDF untuk memulai.";

    scanEmpty.hidden = false;
    scanEmpty.querySelector("strong").textContent =
      "Belum ada halaman scan teridentifikasi";
    scanEmpty.querySelector("p").textContent =
      "Halaman scan akan muncul di sini setelah analisis.";

    digitalCanvas.hidden = true;
    scanCanvas.hidden = true;

    digitalStatus.textContent = "Pilih PDF untuk melihat pratinjau.";
    scanStatus.textContent = "Halaman scan belum dianalisis.";

    digitalPageInput.value = "1";
    digitalPageInput.max = "1";
    digitalPageInput.disabled = true;
    digitalPageTotal.textContent = "/ —";
    digitalZoomLabel.textContent = "100%";
    digitalFitButton.disabled = true;

    scanPageInput.value = "1";
    scanPageInput.max = "1";
    scanPageInput.disabled = true;
    scanPageTotal.textContent = "/ —";
    scanZoomLabel.textContent = "100%";
    scanFitButton.disabled = true;

    clearTablePanel(
      digitalTableContainer,
      digitalTableSubtitle,
      digitalTableFooter,
      "Pilih PDF untuk memulai ekstraksi.",
    );

    clearTablePanel(
      scanTableContainer,
      scanTableSubtitle,
      scanTableFooter,
      "Pilih PDF untuk memulai ekstraksi.",
    );

    renderPageChips();

    onDocumentInfo(
      "Belum ada dokumen",
      "Pilih PDF untuk memulai · file diproses lokal",
    );
  }

  async function clear() {
    selectionToken++;
    analysisAbortController?.abort();
    analysisAbortController = null;

    await Promise.all([
      digitalViewer.clear(),
      scanViewer.clear(),
    ]);

    await Promise.all([
      digitalViewer.setNavigationPages(null),
      scanViewer.setNavigationPages(null),
    ]);

    resetUi();
  }

  async function handleFiles(fileList) {
    const token = ++selectionToken;
    analysisAbortController?.abort();
    analysisAbortController = new AbortController();
    updateProgress({ phase: "reading_pdf", status: "processing", total_pages: null }, { active: true });

    const incoming = [...(fileList ?? [])];
    const pdfs = incoming.filter((file) =>
      file.type === "application/pdf" ||
      file.name.toLowerCase().endsWith(".pdf"),
    );

    if (!pdfs.length) {
      analysisAbortController?.abort();
      analysisAbortController = null;
      updateProgress(null);
      digitalStatus.textContent = incoming.length
        ? "Pilih file PDF yang valid."
        : "Tidak ada file yang dipilih.";
      return;
    }

    selectedFile = pdfs[0];
    analysisResult = null;
    analysisState = "analyzing";
    analysisError = null;

    allPages = [];
    digitalPages = [];
    scanPages = [];
    otherPages = [];

    activeDigitalPage = 0;
    activeScanPage = 0;
    focusedPage = 0;
    rowSelectionState.clear();
    digitalSearch.value = "";
    scanSearch.value = "";
    digitalSearch.disabled = true;
    scanSearch.disabled = true;

    await Promise.all([
      digitalViewer.clear(),
      scanViewer.clear(),
    ]);

    await Promise.all([
      digitalViewer.setNavigationPages(null),
      scanViewer.setNavigationPages(null),
    ]);

    if (token !== selectionToken) return;

    documentName.textContent = selectedFile.name;
    documentMeta.textContent =
      `${formatBytes(selectedFile.size)} · sedang dianalisis`;

    digitalStatus.textContent = `Menganalisis ${selectedFile.name}...`;
    scanStatus.textContent = "Menunggu hasil klasifikasi halaman.";

    setClassificationMessage("Analisis halaman sedang berjalan");

    clearTablePanel(
      digitalTableContainer,
      digitalTableSubtitle,
      digitalTableFooter,
      "Analisis PDF sedang berjalan...",
    );

    clearTablePanel(
      scanTableContainer,
      scanTableSubtitle,
      scanTableFooter,
      "Menunggu klasifikasi halaman scan...",
    );

    onDocumentInfo(
      selectedFile.name,
      `${formatBytes(selectedFile.size)} · diproses lokal`,
    );

    try {
      // Show the source PDF first, even if backend analysis later fails.
      const totalPages = await digitalViewer.loadFile(selectedFile);

      if (token !== selectionToken) return;

      updateProgress({ phase: "reading_pdf", status: "processing", completed_pages: 0, total_pages: totalPages }, { active: true });

      digitalEmpty.hidden = true;
      digitalCanvas.hidden = false;

      // Inspect once; the API returns native digital results and OCR results
      // for classified scan pages in the same response.
      const result = await inspectPdfDocument(selectedFile, {
        ocr: true,
        signal: analysisAbortController.signal,
        onProgress: (progress) => {
          if (token === selectionToken) updateProgress(progress, { active: true });
        },
      });

      if (token !== selectionToken) return;

      analysisResult = result;
      allPages = Array.isArray(result.pages) ? result.pages : [];

      scanPages = allPages
        .filter((page) => isScanPage(page))
        .map((page) => page.page_number);

      digitalPages = allPages
        .filter(
          (page) =>
            pageHasNativeText(page)
            && !scanPages.includes(page.page_number),
        )
        .map((page) => page.page_number);

      otherPages = allPages
        .filter((page) => !digitalPages.includes(page.page_number))
        .filter((page) => !scanPages.includes(page.page_number))
        .map((page) => page.page_number);

      analysisState = "complete";

      updateClassificationCounts();
      renderPageChips();

      // Restrict the left viewer to pages with extractable native text.
      await digitalViewer.setNavigationPages(digitalPages);

      if (token !== selectionToken) return;

      if (digitalPages.length > 0) {
        digitalEmpty.hidden = true;
        digitalCanvas.hidden = false;
      } else {
        await digitalViewer.clear();
        await digitalViewer.setNavigationPages([]);

        digitalEmpty.hidden = false;
        digitalCanvas.hidden = true;

        digitalEmpty.querySelector("strong").textContent =
          "Tidak ada halaman dengan teks digital";
        digitalEmpty.querySelector("p").textContent =
          "Halaman dokumen mungkin memerlukan OCR untuk dibaca.";
      }

      // Load the same PDF into an independent viewer, restricted to scan pages.
      if (scanPages.length > 0) {
        await scanViewer.setNavigationPages(scanPages);
        await scanViewer.loadFile(selectedFile);

        if (token !== selectionToken) return;

        scanEmpty.hidden = true;
        scanCanvas.hidden = false;
      } else {
        await scanViewer.clear();
        await scanViewer.setNavigationPages([]);

        scanEmpty.hidden = false;
        scanCanvas.hidden = true;

        scanEmpty.querySelector("strong").textContent =
          "Tidak ada halaman scan teridentifikasi";
        scanEmpty.querySelector("p").textContent =
          "Tidak ada halaman tanpa teks dengan gambar yang terdeteksi.";
      }

      renderDigitalPanel();
      renderScanPanel();
      renderPageChips();

      const totalTables = result.total_native_tables_detected ?? 0;
      const scanResults = allPages.filter((page) => scanPages.includes(page.page_number));
      const failedScanPages = scanResults.filter((page) => page.table_extraction_status === "ocr_failed");
      const failedPages = allPages.filter((page) => ["ocr_failed", "detection_error", "extraction_error"].includes(page.table_extraction_status)).length;
      updateProgress({
        phase: failedPages ? "partial_failure" : "completed",
        status: failedPages ? "partial_failure" : "completed",
        completed_pages: totalPages,
        total_pages: totalPages,
        failed_pages: failedPages,
      });
      const ocrPages = scanResults.filter((page) => page.ocr_applied);
      const ocrSeconds = ocrPages.reduce((sum, page) => sum + (page.ocr_elapsed_seconds ?? 0), 0);

      setClassificationMessage(failedScanPages.length
        ? `Analisis selesai · OCR gagal di ${failedScanPages.length} halaman scan`
        : `Analisis selesai · ${digitalPages.length} halaman digital · ${ocrPages.length} halaman scan dibaca OCR`,
        failedScanPages.length ? "error" : "pending");

      documentMeta.textContent =
        `${formatBytes(selectedFile.size)} · ${totalPages} halaman · analisis selesai`;

      digitalStatus.textContent =
        `Analisis selesai · ${digitalPages.length} halaman digital · ${totalTables} tabel terdeteksi`;

      scanStatus.textContent = failedScanPages.length
        ? `OCR gagal pada ${failedScanPages.length} halaman scan.`
        : `OCR selesai · ${ocrPages.length} halaman · ${ocrSeconds.toFixed(1)} detik`;

      onDocumentInfo(
        selectedFile.name,
        `${totalPages} halaman · ${formatBytes(selectedFile.size)} · analisis selesai`,
      );

      if (pdfs.length > 1) {
        digitalStatus.textContent +=
          " · Mode Single PDF hanya menggunakan file pertama.";
      }

    } catch (error) {
      if (token !== selectionToken) return;

      console.error("PDF inspection failed:", error);

      analysisState = "failed";
      analysisError =
        error?.message || "Terjadi kesalahan saat menganalisis PDF.";

      updateProgress({
        phase: "failed",
        status: "failed",
        completed_pages: latestSingleProgress?.completed_pages || 0,
        total_pages: latestSingleProgress?.total_pages || digitalViewer.getPageCount() || null,
        message: analysisError,
      });

      setClassificationMessage("Analisis gagal", "error");

      digitalStatus.textContent = analysisError;
      scanStatus.textContent = "Analisis halaman belum berhasil.";

      clearTablePanel(
        digitalTableContainer,
        digitalTableSubtitle,
        digitalTableFooter,
        analysisError,
      );

      clearTablePanel(
        scanTableContainer,
        scanTableSubtitle,
        scanTableFooter,
        analysisError,
      );

      documentMeta.textContent =
        `${formatBytes(selectedFile.size)} · analisis gagal`;
    }
  }

  function handleSequenceInput({
    input,
    viewer,
    pages,
  }) {
    const sequence = Number(input.value);

    if (
      !Number.isInteger(sequence) ||
      sequence < 1 ||
      sequence > pages.length
    ) {
      const currentPage = viewer.getCurrentPage();
      const currentSequence = pages.indexOf(currentPage) + 1;
      input.value = String(Math.max(1, currentSequence));
      return;
    }

    void viewer.goToPage(pages[sequence - 1]);
  }

  function bindTableActions(copyButton, exportButton, getPageNumber, label) {
    copyButton.addEventListener("click", async () => {
      const pageNumber = getPageNumber();
      const page = getPageResult(pageNumber);
      try {
        await copyTablesToClipboard({ tables: page?.tables, sourceName: selectedFile?.name, pageNumber });
        (label === "digital" ? digitalStatus : scanStatus).textContent = `Tabel halaman ${pageNumber} disalin.`;
      } catch (error) {
        (label === "digital" ? digitalStatus : scanStatus).textContent = error?.message || "Tabel gagal disalin.";
      }
    });
    exportButton.addEventListener("click", () => {
      const pageNumber = getPageNumber();
      const page = getPageResult(pageNumber);
      try {
        downloadTablesAsCsv({
          tables: (page?.tables ?? []).map((table) => ({ ...table, source_page: pageNumber })),
          sourceName: selectedFile?.name,
          pageNumber,
          filename: `${(selectedFile?.name || "docuflow").replace(/\.pdf$/i, "")}-halaman-${pageNumber}.csv`,
        });
        (label === "digital" ? digitalStatus : scanStatus).textContent = `CSV halaman ${pageNumber} diunduh.`;
      } catch (error) {
        (label === "digital" ? digitalStatus : scanStatus).textContent = error?.message || "CSV gagal dibuat.";
      }
    });
  }

  bindTableActions(digitalCopyButton, digitalExportButton, () => activeDigitalPage, "digital");
  bindTableActions(scanCopyButton, scanExportButton, () => activeScanPage, "scan");

  digitalPageInput.addEventListener("change", () => {
    handleSequenceInput({
      input: digitalPageInput,
      viewer: digitalViewer,
      pages: digitalPages,
    });
  });

  digitalSearch.addEventListener("input", () => renderDigitalPanel());
  scanSearch.addEventListener("input", () => renderScanPanel());

  digitalPageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") digitalPageInput.blur();
  });

  scanPageInput.addEventListener("change", () => {
    handleSequenceInput({
      input: scanPageInput,
      viewer: scanViewer,
      pages: scanPages,
    });
  });

  scanPageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") scanPageInput.blur();
  });

  digitalFitButton.addEventListener("click", () => {
    void digitalViewer.setZoom(1);
  });

  scanFitButton.addEventListener("click", () => {
    void scanViewer.setZoom(1);
  });

  resetUi();

  return {
    handleFiles,
    clear,
    getPageCount: () => allPages.length,
  };
}
