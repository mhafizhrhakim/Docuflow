/** Shared page routing rules for server responses and older cached results. */
export function pageHasNativeText(page) {
  if (!page) return false;
  if (page.has_extractable_text === true) return true;

  const characterCount = Number(page.native_text_char_count);
  return (
    (Number.isFinite(characterCount) && characterCount > 0)
    || (typeof page.native_text === "string" && page.native_text.trim().length > 0)
  );
}

export function isScanPage(page) {
  if (!page) return false;

  if (["scan_candidate", "scan_with_text_layer"].includes(page.source_candidate)) {
    return true;
  }
  if (page.ocr_applied === true) return true;

  // A stale/older API response can have a false boolean while its per-page
  // native text count is positive. Do not turn that mixed/native page into a
  // scan solely because it also contains embedded images.
  return !pageHasNativeText(page) && Number(page.image_count ?? 0) > 0;
}

export function pageType(page) {
  if (!page) return "unknown";
  if (isScanPage(page)) return "scan";
  if (pageHasNativeText(page)) return "digital";
  return "other";
}
