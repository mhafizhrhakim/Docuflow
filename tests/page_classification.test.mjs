import assert from "node:assert/strict";
import test from "node:test";

import { isScanPage, pageHasNativeText, pageType } from "../frontend/src/js/page-classification.mjs";

test("mixed native page with a stale false flag remains native in the UI", () => {
  const page = {
    source_candidate: "mixed_candidate",
    has_extractable_text: false,
    native_text_char_count: 2348,
    image_count: 2,
    ocr_applied: false,
  };

  assert.equal(pageHasNativeText(page), true);
  assert.equal(isScanPage(page), false);
  assert.equal(pageType(page), "digital");
});

test("full-page scan with hidden text is still routed to OCR", () => {
  const page = {
    source_candidate: "scan_with_text_layer",
    has_extractable_text: true,
    native_text_char_count: 120,
    image_count: 1,
    ocr_applied: false,
  };

  assert.equal(pageHasNativeText(page), true);
  assert.equal(isScanPage(page), true);
  assert.equal(pageType(page), "scan");
});

test("image-only scan remains a scan when no text evidence exists", () => {
  const page = {
    source_candidate: "scan_candidate",
    has_extractable_text: false,
    native_text_char_count: 0,
    image_count: 1,
    ocr_applied: true,
  };

  assert.equal(pageHasNativeText(page), false);
  assert.equal(isScanPage(page), true);
  assert.equal(pageType(page), "scan");
});
