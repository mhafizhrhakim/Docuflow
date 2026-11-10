import "../css/styles.css";
import { checkApiHealth } from "./api.js";
import { createSinglePdfWorkflow } from "./single-pdf.js";
import { createMultiDocumentWorkflow } from "./multi-document.js";

const $ = (selector) => document.querySelector(selector);
const modeButtons = [...document.querySelectorAll("[data-mode]")];
const fileInput = $("#pdf-input");
const chooseButton = $("#choose-pdf-button");
const replaceButton = $("#replace-document-button");
const singleWorkflowElement = $("#single-workflow");
const multiWorkflowElement = $("#multi-workflow");
const documentName = $("#document-name");
const documentMeta = $("#document-meta");
const apiStatus = $("#api-status");
const apiDetail = $("#api-detail");
const statusDot = $(".api-status-wrap .status-dot");

let currentMode = "single";

function updateDocumentIdentity(name, meta) {
  documentName.textContent = name;
  documentMeta.textContent = meta;
}

const singleWorkflow = createSinglePdfWorkflow({
  onDocumentInfo: updateDocumentIdentity,
});

const multiWorkflow = createMultiDocumentWorkflow({
  container: $("#multi-document-grid"),
  emptyState: $("#multi-empty"),
  statusElement: $("#multi-status"),
  combinedExportButton: $("#multi-export-button"),
  combinedXlsxExportButton: $("#multi-xlsx-export-button"),
  overallProgressContainer: $("#multi-overall-progress"),
  overallProgressBar: $("#multi-overall-progress-bar"),
  overallProgressLabel: $("#multi-overall-progress-label"),
  overallElapsedLabel: $("#multi-overall-progress-elapsed"),
  onDocumentInfo: updateDocumentIdentity,
});

async function setMode(mode) {
  currentMode = mode;

  modeButtons.forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  singleWorkflowElement.hidden = mode !== "single";
  multiWorkflowElement.hidden = mode !== "multi";
  fileInput.multiple = mode === "multi";
  fileInput.value = "";

  chooseButton.textContent = mode === "multi" ? "＋ Pilih beberapa PDF" : "＋ Pilih PDF";
  replaceButton.textContent = mode === "multi" ? "▤ Ganti dokumen" : "▤ Ganti dokumen";
  await Promise.all([singleWorkflow.clear(), multiWorkflow.clear()]);
  if (mode === "multi") {
    updateDocumentIdentity("Belum ada dokumen", "Pilih beberapa PDF untuk memulai · file diproses lokal");
  } else {
    updateDocumentIdentity("Belum ada dokumen", "Pilih PDF untuk memulai · file diproses lokal");
  }
}

chooseButton.addEventListener("click", () => fileInput.click());
replaceButton.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  const files = fileInput.files;
  if (currentMode === "multi") {
    void multiWorkflow.handleFiles(files);
  } else {
    void singleWorkflow.handleFiles(files);
  }
});

modeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.mode !== currentMode) {
      void setMode(button.dataset.mode);
    }
  });
});

async function updateApiStatus() {
  try {
    const health = await checkApiHealth();
    apiStatus.textContent = "Backend terhubung";
    statusDot?.classList.add("online");
    statusDot?.classList.remove("offline");
    apiDetail.textContent = `· ${health.app_name ?? "DocuFlow API"}`;
  } catch {
    apiStatus.textContent = "Backend tidak tersedia";
    statusDot?.classList.add("offline");
    statusDot?.classList.remove("online");
    apiDetail.textContent = "· jalankan backend untuk mengaktifkan API";
  }
}

void setMode("single");
void updateApiStatus();
