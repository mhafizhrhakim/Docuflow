
/**
 * DocuFlow API client.
 * Handles backend health checks and PDF page inspection.
 */

const configuredApiBaseUrl = import.meta.env?.VITE_API_BASE_URL?.trim();
const API_BASE_URL = configuredApiBaseUrl
  ? configuredApiBaseUrl.replace(/\/+$/, "")
  : "/api";

async function readResponse(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function getErrorMessage(payload, status) {
  const detail = payload?.detail;

  if (typeof detail === "string") {
    return detail;
  }

  if (Array.isArray(detail)) {
    return detail
      .map((item) => item.msg || "Request tidak valid")
      .join("; ");
  }

  return `API mengembalikan HTTP ${status}.`;
}

/**
 * Check whether the DocuFlow backend is available.
 */
export async function checkApiHealth() {
  const response = await fetch(`${API_BASE_URL}/health`, {
    headers: {
      Accept: "application/json",
    },
  });

  const payload = await readResponse(response);

  if (!response.ok) {
    throw new Error(getErrorMessage(payload, response.status));
  }

  return payload;
}

/**
 * Send one PDF to the backend for page inspection.
 *
 * Returns page text, source candidates, and detected tables.
 * ocr=false: digital pages only (fast); scan pages come back as "ocr_pending".
 * ocr=true: scan pages are also read with OCR (slower, a few seconds per page).
 */
export async function inspectPdfDocument(file, { signal, ocr = true, onProgress } = {}) {
  if (!file) {
    throw new Error("Pilih PDF terlebih dahulu.");
  }

  const isPdf =
    file.type === "application/pdf" ||
    file.name.toLowerCase().endsWith(".pdf");

  if (!isPdf) {
    throw new Error("File harus berformat PDF.");
  }

  const formData = new FormData();
  formData.append("file", file, file.name);

  const progressId = typeof onProgress === "function" ? crypto.randomUUID() : null;
  const progressController = progressId ? new AbortController() : null;
  let pollActive = Boolean(progressId);
  let latestProgress = null;
  const report = (value) => {
    if (!onProgress) return;
    latestProgress = value;
    try { onProgress(value); } catch { /* UI callbacks must not change extraction. */ }
  };
  const waitForNextPoll = () => new Promise((resolve) => {
    if (!progressController || progressController.signal.aborted) {
      resolve();
      return;
    }
    const signal = progressController.signal;
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, 450);
    signal.addEventListener("abort", finish, { once: true });
  });

  const pollProgress = async () => {
    while (pollActive && progressId) {
      try {
        const progressResponse = await fetch(
          `${API_BASE_URL}/documents/inspect/progress/${encodeURIComponent(progressId)}`,
          { headers: { Accept: "application/json" }, signal: progressController.signal },
        );
        if (progressResponse.ok) {
          const progress = await readResponse(progressResponse);
          report(progress);
          if (["completed", "partial_failure", "failed"].includes(progress.status)) return;
        }
      } catch (error) {
        if (error?.name === "AbortError") return;
      }
      await waitForNextPoll();
    }
  };
  const polling = progressId ? pollProgress() : Promise.resolve();

  try {
    const query = new URLSearchParams({ ocr: ocr ? "true" : "false" });
    if (progressId) query.set("progress_id", progressId);
    const response = await fetch(`${API_BASE_URL}/documents/inspect?${query}`, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: formData,
      signal,
    });

    const payload = await readResponse(response);
    if (!response.ok) throw new Error(getErrorMessage(payload, response.status));
    if (!Array.isArray(payload.pages)) throw new Error("Response API tidak memiliki daftar halaman yang valid.");
    return payload;
  } catch (error) {
    if (progressId) {
      report({
        ...(latestProgress ?? {}),
        phase: "failed",
        status: "failed",
      });
    }
    throw error;
  } finally {
    pollActive = false;
    progressController?.abort();
    await polling;
  }
}
