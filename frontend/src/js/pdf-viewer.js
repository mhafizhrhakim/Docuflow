
import * as pdfjsLib from "pdfjs-dist";
import PdfWorker from "pdfjs-dist/build/pdf.worker.mjs?worker";

export function createPdfViewer({
  canvas,
  container,
  pageLabel,
  prevButton,
  nextButton,
  zoomInButton,
  zoomOutButton,
  onStatus = () => {},
  onPageChange = () => {},
  onZoomChange = () => {},
}) {
  let pdfDocument = null;
  let loadingTask = null;
  let renderTask = null;
  let pdfWorkerPort = null;
  let pdfWorker = null;

  let currentPage = 1;
  let zoomLevel = 1;
  let renderToken = 0;

  // null means all PDF pages; an array restricts navigation to that sequence.
  let navigationPages = null;

  function getAvailablePages() {
    if (!pdfDocument) return [];

    if (navigationPages === null) {
      return Array.from(
        { length: pdfDocument.numPages },
        (_, index) => index + 1,
      );
    }

    return navigationPages.filter(
      (page) => Number.isInteger(page) &&
        page >= 1 &&
        page <= pdfDocument.numPages,
    );
  }

  function getNavigationIndex() {
    return getAvailablePages().indexOf(currentPage);
  }

  function notifyPageChange() {
    if (!pdfDocument) return;

    const availablePages = getAvailablePages();
    const navigationIndex = getNavigationIndex();

    if (pageLabel) {
      pageLabel.textContent =
        `Page ${currentPage} of ${pdfDocument.numPages}`;
    }

    prevButton.disabled =
      navigationIndex <= 0 || availablePages.length === 0;

    nextButton.disabled =
      navigationIndex < 0 ||
      navigationIndex >= availablePages.length - 1;

    onPageChange({
      page: currentPage,
      totalPages: pdfDocument.numPages,
      navigationIndex,
      navigationCount: availablePages.length,
      sequencePage: navigationIndex >= 0 ? navigationIndex + 1 : null,
    });
  }

  function notifyZoomChange() {
    onZoomChange(Math.round(zoomLevel * 100));
  }

  async function renderPage() {
    if (!pdfDocument) return;

    const token = ++renderToken;

    if (renderTask) {
      try {
        renderTask.cancel();
      } catch {
        // The previous render may have finished already.
      }
      renderTask = null;
    }

    try {
      const page = await pdfDocument.getPage(currentPage);

      if (token !== renderToken) return;

      const baseViewport = page.getViewport({ scale: 1 });

      const availableWidth = Math.max(
        container.clientWidth - 28,
        280,
      );

      const fitScale = Math.min(
        1.6,
        availableWidth / baseViewport.width,
      );

      const scale = Math.max(
        0.35,
        Math.min(3, fitScale * zoomLevel),
      );

      const viewport = page.getViewport({ scale });
      const outputScale = Math.min(
        window.devicePixelRatio || 1,
        2,
      );

      const context = canvas.getContext("2d", { alpha: false });

      if (!context) {
        throw new Error(
          "Browser tidak dapat membuat canvas PDF.",
        );
      }

      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);

      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;

      const task = page.render({
        canvasContext: context,
        viewport,
        transform: outputScale === 1
          ? null
          : [outputScale, 0, 0, outputScale, 0, 0],
        background: "#ffffff",
      });

      renderTask = task;
      await task.promise;

      if (token !== renderToken) return;

      notifyPageChange();
      notifyZoomChange();

      onStatus(
        `Pratinjau siap · halaman ${currentPage} dari ${pdfDocument.numPages}`,
      );
    } catch (error) {
      if (
        error?.name !== "RenderingCancelledException" &&
        token === renderToken
      ) {
        onStatus(
          `Gagal menampilkan halaman: ${
            error?.message ?? "kesalahan tidak diketahui"
          }`,
        );
      }
    }
  }

  async function releaseDocument() {
    if (renderTask) {
      try {
        renderTask.cancel();
      } catch {
        // No active render task.
      }
      renderTask = null;
    }

    if (pdfDocument) {
      try {
        await pdfDocument.destroy();
      } catch {
        // The document may already be closed.
      }
    } else if (loadingTask) {
      try {
        await loadingTask.destroy();
      } catch {
        // The loading task may already be closed.
      }
    }

    pdfDocument = null;
    loadingTask = null;
    pdfWorkerPort?.terminate();
    pdfWorkerPort = null;
    pdfWorker = null;
  }

  async function loadFile(file) {
    if (!file || !file.name.toLowerCase().endsWith(".pdf")) {
      throw new Error("Pilih file dengan format PDF.");
    }

    onStatus(`Membuka ${file.name}...`);

    renderToken++;
    await releaseDocument();

    const bytes = new Uint8Array(await file.arrayBuffer());

    pdfWorkerPort = new PdfWorker();
    pdfWorker = new pdfjsLib.PDFWorker({ port: pdfWorkerPort });
    loadingTask = pdfjsLib.getDocument({ data: bytes, worker: pdfWorker });
    pdfDocument = await loadingTask.promise;

    const availablePages = getAvailablePages();

    currentPage = availablePages[0] ?? 1;
    zoomLevel = 1;

    canvas.hidden = false;
    zoomInButton.disabled = false;
    zoomOutButton.disabled = false;

    await renderPage();

    return pdfDocument.numPages;
  }

  async function goToPage(pageNumber) {
    if (!pdfDocument) return;

    const page = Number(pageNumber);

    if (
      !Number.isInteger(page) ||
      page < 1 ||
      page > pdfDocument.numPages
    ) {
      return;
    }

    const availablePages = getAvailablePages();

    if (!availablePages.includes(page)) {
      onStatus("Halaman tersebut tidak termasuk kelompok halaman ini.");
      return;
    }

    if (currentPage === page) return;

    currentPage = page;
    await renderPage();
  }

  async function goPrevious() {
    const pages = getAvailablePages();
    const index = pages.indexOf(currentPage);

    if (index > 0) {
      await goToPage(pages[index - 1]);
    }
  }

  async function goNext() {
    const pages = getAvailablePages();
    const index = pages.indexOf(currentPage);

    if (index >= 0 && index < pages.length - 1) {
      await goToPage(pages[index + 1]);
    }
  }

  async function setNavigationPages(pages) {
    navigationPages = pages === null
      ? null
      : [...new Set(
          pages
            .map(Number)
            .filter((page) => Number.isInteger(page) && page >= 1),
        )];

    if (!pdfDocument) return;

    const availablePages = getAvailablePages();

    if (availablePages.length === 0) {
      onPageChange({
        page: null,
        totalPages: pdfDocument.numPages,
        navigationIndex: -1,
        navigationCount: 0,
        sequencePage: null,
      });

      prevButton.disabled = true;
      nextButton.disabled = true;
      return;
    }

    if (!availablePages.includes(currentPage)) {
      currentPage = availablePages[0];
      await renderPage();
    } else {
      notifyPageChange();
    }
  }

  async function setZoom(value) {
    if (!pdfDocument) return;

    zoomLevel = Math.max(0.5, Math.min(2.5, value));
    await renderPage();
  }

  async function zoomAtPoint(value, clientX, clientY) {
    if (!pdfDocument || !canvas.width || !canvas.height) return;

    const before = canvas.getBoundingClientRect();
    const anchorX = Math.max(0, Math.min(1, (clientX - before.left) / before.width));
    const anchorY = Math.max(0, Math.min(1, (clientY - before.top) / before.height));
    await setZoom(value);

    const after = canvas.getBoundingClientRect();
    container.scrollLeft += after.left + anchorX * after.width - clientX;
    container.scrollTop += after.top + anchorY * after.height - clientY;
  }

  async function clear() {
    renderToken++;
    await releaseDocument();

    const context = canvas.getContext("2d");
    context?.clearRect(0, 0, canvas.width, canvas.height);

    canvas.width = 0;
    canvas.height = 0;
    canvas.style.width = "0px";
    canvas.style.height = "0px";
    canvas.hidden = true;

    currentPage = 1;
    zoomLevel = 1;

    if (pageLabel) {
      pageLabel.textContent = "Page 1 of 1";
    }

    prevButton.disabled = true;
    nextButton.disabled = true;
    zoomInButton.disabled = true;
    zoomOutButton.disabled = true;

    notifyZoomChange();
  }

  prevButton.addEventListener("click", () => {
    void goPrevious();
  });

  nextButton.addEventListener("click", () => {
    void goNext();
  });

  zoomInButton.addEventListener("click", () => {
    void setZoom(zoomLevel * 1.2);
  });

  zoomOutButton.addEventListener("click", () => {
    void setZoom(zoomLevel / 1.2);
  });

  // Chromium-based browsers report supported touchpad pinch gestures as
  // Ctrl+wheel. Ordinary two-finger scrolling remains native on this
  // overflow container, including horizontal scrolling.
  container.addEventListener("wheel", (event) => {
    if (!event.ctrlKey || event.deltaY === 0 || !pdfDocument) return;

    const nextZoom = zoomLevel * Math.exp(-event.deltaY * 0.002);
    const boundedZoom = Math.max(0.5, Math.min(2.5, nextZoom));
    if (boundedZoom === zoomLevel) return;

    event.preventDefault();
    void zoomAtPoint(boundedZoom, event.clientX, event.clientY);
  }, { passive: false });

  return {
    loadFile,
    goToPage,
    goPrevious,
    goNext,
    clear,
    setZoom,
    setNavigationPages,
    getPageCount: () => pdfDocument?.numPages ?? 0,
    getCurrentPage: () => currentPage,
    getNavigationPages: () => getAvailablePages(),
    getZoom: () => Math.round(zoomLevel * 100),
  };
}
