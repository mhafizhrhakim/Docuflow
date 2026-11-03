import fs from "node:fs";
import { getDocument, OPS } from "pdfjs-dist/legacy/build/pdf.mjs";

let data;
if (process.argv[2]) {
  data = new Uint8Array(fs.readFileSync(process.argv[2]));
} else {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  data = new Uint8Array(Buffer.concat(chunks));
}
const pdf = await getDocument({ data, verbosity: 0 }).promise;
const pages = [];
try {
  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    const page = await pdf.getPage(pageNumber);
    const height = page.view[3];
    const [content, operators] = await Promise.all([
      page.getTextContent(),
      page.getOperatorList(),
    ]);
    const textItems = content.items
      .filter((item) => item.str?.trim())
      .map((item) => {
        const bottom = height - item.transform[5];
        const itemHeight = Math.max(item.height, 1);
        return {
          text: item.str,
          x0: item.transform[4],
          y0: bottom - itemHeight,
          x1: item.transform[4] + item.width,
          y1: bottom,
        };
      });
    const rectangles = [];
    for (let index = 0; index < operators.fnArray.length; index += 1) {
      if (operators.fnArray[index] !== OPS.constructPath) continue;
      const [pathOps, coordinates] = operators.argsArray[index] ?? [];
      if (pathOps?.length !== 1 || pathOps[0] !== OPS.rectangle || coordinates?.length !== 4) continue;
      const [x, y, width, rectHeight] = coordinates;
      const x1 = x + width;
      // PDF.js operator-list path coordinates already use the page viewport
      // origin (top-left); text transforms use PDF's baseline origin below.
      const y0 = y;
      const y1 = y + rectHeight;
      if (x > 5 && x1 < page.view[2] - 5 && width > 4 && width < page.view[2] * 0.95 && rectHeight > 5 && rectHeight < 45 && y0 > 5 && y1 < height - 5) {
        rectangles.push([x, y0, x1, y1]);
      }
    }
    pages.push({ page_number: pageNumber, width: page.view[2], height, text_items: textItems, rectangles });
  }
} finally {
  await pdf.destroy();
}
process.stdout.write(JSON.stringify({ pages }));
