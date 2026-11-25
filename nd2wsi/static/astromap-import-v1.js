/* AstroMapper TSV import. Pixel coordinates refer to the matching whole JPG,
   not to the native slide. No image pixels are decoded in this module. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2AstromapImport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_ROIS = 2000;
  const MAX_NOTE = 4096;
  const MAX_TEXT = MAX_ROIS * (MAX_NOTE + 128) + 64;
  const MAX_REFERENCE_HEADER_PIXELS = 2_000_000_000;
  const HEADER = ["INDEX", "X1", "Y1", "X2", "Y2", "NOTE"];

  function integer(value, label, minimum = 0) {
    if (!Number.isSafeInteger(value) || value < minimum)
      throw new Error(`${label} must be a ${minimum ? "positive" : "non-negative"} integer.`);
    return value;
  }

  function validateSize(size, label = "Image") {
    if (!size || typeof size !== "object") throw new Error(`${label} dimensions are required.`);
    return {
      width: integer(size.width, `${label} width`, 1),
      height: integer(size.height, `${label} height`, 1),
    };
  }

  function validateRow(row, label) {
    if (!row || typeof row !== "object") throw new Error(`${label} is not a region.`);
    integer(row.index, `${label} INDEX`, 1);
    for (const key of ["x1", "y1", "x2", "y2"]) integer(row[key], `${label} ${key.toUpperCase()}`);
    if (row.x2 < row.x1 || row.y2 < row.y1) {
      const width = row.x2 - row.x1 + 1, height = row.y2 - row.y1 + 1;
      const issue = width >= 0 && height >= 0 && (!width || !height) ? "has zero area" : "has reversed endpoints";
      throw new Error(`${label}: INDEX ${row.index} ${issue} (${row.x1}, ${row.y1}) → (${row.x2}, ${row.y2}); X2/Y2 must include the last pixel.`);
    }
    if (typeof row.note !== "string" || row.note.length > MAX_NOTE)
      throw new Error(`${label} NOTE must be text of at most ${MAX_NOTE} characters.`);
  }

  function outsideMessage(rows, grid) {
    const outside = rows.filter(row => row.x2 >= grid.width || row.y2 >= grid.height);
    if (!outside.length) return "";
    return `${outside.length} region${outside.length === 1 ? " is" : "s are"} outside the coordinate JPG ` +
      `${grid.width} × ${grid.height} px (first INDEX ${outside[0].index}). ` +
      "Choose the matching coordinate JPG or enter the confirmed TXT coordinate dimensions.";
  }

  function parse(text, coordinateSize = null) {
    if (typeof text !== "string") throw new Error("Choose an AstroMapper TXT file.");
    if (text.length > MAX_TEXT) throw new Error("The TXT file is too large for 2000 regions.");
    const grid = coordinateSize === null ? null : validateSize(coordinateSize, "Coordinate JPG");
    const lines = text.replace(/^\uFEFF/, "").split(/\r\n|\n|\r/);
    const rows = [], indices = new Set(), errors = [];
    let header = false, count = 0;
    for (let line = 0; line < lines.length; line++) {
      const value = lines[line];
      if (!value.trim()) continue;
      const fields = value.split("\t");
      if (!header) {
        if (fields.length !== HEADER.length ||
            fields.some((field, i) => field.trim().toUpperCase() !== HEADER[i]))
          throw new Error("Expected tab-separated INDEX, X1, Y1, X2, Y2, NOTE header.");
        header = true;
        continue;
      }
      const label = `Line ${line + 1}`;
      if (++count > MAX_ROIS) throw new Error(`Import at most ${MAX_ROIS} regions at once.`);
      try {
        if (fields.length !== HEADER.length)
          throw new Error(`${label} must contain exactly six tab-separated fields.`);
        const numbers = fields.slice(0, 5).map((field, i) => {
          if (!/^\d+$/.test(field.trim())) throw new Error(`${label} ${HEADER[i]} must be an integer.`);
          return integer(Number(field.trim()), `${label} ${HEADER[i]}`, i === 0 ? 1 : 0);
        });
        const row = { index: numbers[0], x1: numbers[1], y1: numbers[2],
          x2: numbers[3], y2: numbers[4], note: fields[5] };
        validateRow(row, label);
        if (indices.has(row.index)) throw new Error(`${label} repeats INDEX ${row.index}.`);
        indices.add(row.index);
        rows.push(row);
      } catch (error) { errors.push(error.message); }
    }
    const outside = grid ? outsideMessage(rows, grid) : "";
    if (errors.length || outside) throw new Error([
      ...errors.slice(0, 3), errors.length > 3 ? `${errors.length - 3} more invalid rows.` : "", outside,
    ].filter(Boolean).join(" "));
    if (!rows.length) throw new Error("The TXT file contains no regions.");
    return { rows, count: rows.length };
  }

  // Review imports without silently altering the file or losing failed rows.
  // The strict parser remains available for callers that need atomic validation.
  function review(text, coordinateSize = null, { repairZeroArea = false } = {}) {
    if (typeof text !== "string") throw new Error("Choose an AstroMapper TXT file.");
    if (text.length > MAX_TEXT) throw new Error("The TXT file is too large for 2000 regions.");
    const grid = coordinateSize === null ? null : validateSize(coordinateSize, "Coordinate JPG");
    const lines = text.replace(/^\uFEFF/, "").split(/\r\n|\n|\r/);
    const entries = [], occurrences = new Map();
    let header = false;
    for (let line = 0; line < lines.length; line++) {
      const raw = lines[line];
      if (!raw.trim()) continue;
      const fields = raw.split("\t");
      if (!header) {
        if (fields.length !== HEADER.length ||
            fields.some((field, i) => field.trim().toUpperCase() !== HEADER[i]))
          throw new Error("Expected tab-separated INDEX, X1, Y1, X2, Y2, NOTE header.");
        header = true;
        continue;
      }
      if (entries.length >= MAX_ROIS) throw new Error(`Import at most ${MAX_ROIS} regions at once.`);
      const value = /^\d+$/.test(fields[0].trim()) ? Number(fields[0].trim()) : NaN;
      const index = Number.isSafeInteger(value) && value > 0 ? value : null;
      // Even a malformed record reserves its INDEX. Never choose one of two
      // contradictory records based on which one happens to be valid or first.
      if (index !== null) occurrences.set(index, (occurrences.get(index) || 0) + 1);
      entries.push({ line: line + 1, index, raw, fields });
    }
    if (!entries.length) throw new Error("The TXT file contains no regions.");
    const rows = [], issues = [];
    let repairedCount = 0;
    for (const entry of entries) {
      const { line, index, raw, fields } = entry;
      const label = `Line ${line}`;
      const issue = { line, index, raw, message: "", repairable: false, repaired: false, original: null };
      try {
        if (fields.length !== HEADER.length)
          throw new Error(`${label} must contain exactly six tab-separated fields.`);
        const numbers = fields.slice(0, 5).map((field, i) => {
          if (!/^\d+$/.test(field.trim())) throw new Error(`${label} ${HEADER[i]} must be an integer.`);
          return integer(Number(field.trim()), `${label} ${HEADER[i]}`, i === 0 ? 1 : 0);
        });
        const row = { index: numbers[0], x1: numbers[1], y1: numbers[2],
          x2: numbers[3], y2: numbers[4], note: fields[5] };
        issue.original = { ...row };
        if (occurrences.get(index) > 1) throw new Error(`${label} repeats INDEX ${index}; all rows with this INDEX are withheld.`);
        // A one-pixel endpoint gap can represent a collapsed legacy rectangle.
        // Only an explicit choice may expand that empty dimension to one pixel.
        const width = row.x2 - row.x1 + 1, height = row.y2 - row.y1 + 1;
        const zeroArea = width >= 0 && height >= 0 && (!width || !height);
        const candidate = zeroArea ? { ...row, x2: Math.max(row.x1, row.x2), y2: Math.max(row.y1, row.y2) } : row;
        validateRow(candidate, label);
        const zeroMessage = zeroArea ? `${label}: INDEX ${index} has zero area (${row.x1}, ${row.y1}) → (${row.x2}, ${row.y2}).` : "";
        const outside = grid ? outsideMessage([candidate], grid) : "";
        if (outside) throw new Error([zeroMessage, `${label}: ${outside}`].filter(Boolean).join(" "));
        if (zeroArea) {
          issue.repairable = true;
          issue.message = zeroMessage;
          if (repairZeroArea !== true) {
            issue.message += " Withheld; the empty dimension can be repaired to one pixel.";
            issues.push(issue);
            continue;
          }
          issue.repaired = true;
          issue.message += ` Repaired to (${candidate.x1}, ${candidate.y1}) → (${candidate.x2}, ${candidate.y2}).`;
          issues.push(issue);
          repairedCount++;
        }
        rows.push(candidate);
      } catch (error) {
        issue.message = error.message;
        if (index !== null && occurrences.get(index) > 1 && !issue.message.includes("all rows with this INDEX"))
          issue.message += ` INDEX ${index} is repeated; all rows with this INDEX are withheld.`;
        issues.push(issue);
      }
    }
    return { rows, count: rows.length, issues, total: entries.length,
      repairedCount, skippedCount: entries.length - rows.length };
  }

  // BigInt keeps multiplication exact for large whole-slide dimensions.
  const ceilRatio = (value, to, from) => Number(
    (BigInt(value) * BigInt(to) + BigInt(from) - 1n) / BigInt(from)
  );
  const floorRatio = (value, to, from) => Number(BigInt(value) * BigInt(to) / BigInt(from));

  function pixelRange(first, last, coordinateLength, sourceLength) {
    const start = Math.min(sourceLength - 1, ceilRatio(first, sourceLength, coordinateLength));
    const end = Math.min(sourceLength, ceilRatio(last + 1, sourceLength, coordinateLength));
    if (end > start) return [start, end];
    // A legacy JPG can be larger than its native image. Subpixel regions
    // still cover one native pixel; this direction is necessarily lossy.
    const pixel = Math.min(sourceLength - 1, floorRatio(first, sourceLength, coordinateLength));
    return [pixel, pixel + 1];
  }

  function checkAspectRatio(coordinateSize, sourceSize) {
    const grid = validateSize(coordinateSize, "Coordinate JPG");
    const source = validateSize(sourceSize, "Current image");
    // A 1-step resize can round either edge by 1 px; a 2-step whole-slide
    // integer floor downscale can drift up to 2 px on the minor axis when the
    // relative aspect-ratio difference stays within 0.5%.
    const p1 = BigInt(grid.width) * BigInt(source.height);
    const p2 = BigInt(grid.height) * BigInt(source.width);
    const cross = p1 >= p2 ? p1 - p2 : p2 - p1;
    const maxDim = BigInt(Math.max(grid.width, grid.height, source.width, source.height));
    const minProd = p1 < p2 ? p1 : p2;
    if (!(cross <= maxDim || (cross <= 2n * maxDim && cross * 200n <= minProd))) {
      throw new Error(
        `JPG/PNG (${grid.width} × ${grid.height}) and current image (${source.width} × ${source.height}) ` +
        "have different aspect ratios. Use the matching uncropped reference image."
      );
    }
    return { grid, source };
  }

  function mapToSource(rows, coordinateSize, sourceSize) {
    const grid = validateSize(coordinateSize, "Coordinate JPG");
    const source = validateSize(sourceSize, "Current image");
    if (!Array.isArray(rows) || !rows.length || rows.length > MAX_ROIS)
      throw new Error(`Import between 1 and ${MAX_ROIS} regions.`);
    checkAspectRatio(grid, source);
    const indices = new Set();
    rows.forEach((row, i) => {
      const label = `Region ${i + 1}`;
      validateRow(row, label);
      if (indices.has(row.index)) throw new Error(`${label} repeats INDEX ${row.index}.`);
      indices.add(row.index);
    });
    const outside = outsideMessage(rows, grid);
    if (outside) throw new Error(outside);
    return rows.map((row) => {
      const [x, right] = pixelRange(row.x1, row.x2, grid.width, source.width);
      const [y, bottom] = pixelRange(row.y1, row.y2, grid.height, source.height);
      return { type: "box", x, y, w: right - x, h: bottom - y, text: row.note,
        astromap: true, astromapEnabled: true, astromapImportIndex: row.index };
    });
  }

  async function readJpegSize(file) {
    if (!file || typeof file.slice !== "function" || !Number.isSafeInteger(file.size) || file.size < 4)
      throw new Error("Choose the matching JPG image.");
    const read = async (offset, length) => {
      if (offset + length > file.size) throw new Error("The JPG header is truncated.");
      const bytes = new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
      if (bytes.length !== length) throw new Error("The JPG header is truncated.");
      return bytes;
    };
    const start = await read(0, 2);
    if (start[0] !== 0xff || start[1] !== 0xd8) throw new Error("The selected file is not a JPG image.");
    let offset = 2, padding = 0;
    for (let segment = 0; segment < 4096; segment++) {
      let marker = await read(offset, 2);
      if (marker[0] !== 0xff) throw new Error("The JPG header contains an invalid marker.");
      offset += 2;
      while (marker[1] === 0xff) {
        if (++padding > 4096) throw new Error("The JPG header contains too much marker padding.");
        marker = [0xff, (await read(offset++, 1))[0]];
      }
      const kind = marker[1];
      if (kind === 0xda || kind === 0xd9) throw new Error("The JPG header has no image dimensions.");
      if (kind === 0x00 || kind === 0xd8) throw new Error("The JPG header contains an invalid marker.");
      if (kind === 0x01 || (kind >= 0xd0 && kind <= 0xd7)) continue;
      const lengthBytes = await read(offset, 2);
      const length = lengthBytes[0] * 256 + lengthBytes[1];
      if (length < 2 || offset + length > file.size) throw new Error("The JPG header contains a truncated segment.");
      if (kind >= 0xc0 && kind <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(kind)) {
        if (length < 8) throw new Error("The JPG frame header is invalid.");
        const frame = await read(offset + 2, 6);
        if (length < 8 + 3 * frame[5]) throw new Error("The JPG frame header is truncated.");
        const size = validateSize({ width: frame[3] * 256 + frame[4], height: frame[1] * 256 + frame[2] }, "JPG");
        if (size.width * size.height > MAX_REFERENCE_HEADER_PIXELS)
          throw new Error("The reference image dimensions exceed safety limits.");
        return size;
      }
      offset += length;
    }
    throw new Error("The JPG header contains too many metadata segments.");
  }

  async function readImageSize(file) {
    if (!file || typeof file.slice !== "function" || !Number.isSafeInteger(file.size) || file.size < 4)
      throw new Error("Choose the matching JPG or PNG image.");
    const header = new Uint8Array(await file.slice(0, Math.min(file.size, 24)).arrayBuffer());
    const png = [137, 80, 78, 71, 13, 10, 26, 10];
    if (!png.every((byte, i) => header[i] === byte)) return readJpegSize(file);
    if (file.size < 33 || header.length < 24 ||
        header[8] !== 0 || header[9] !== 0 || header[10] !== 0 || header[11] !== 13 ||
        String.fromCharCode(...header.slice(12, 16)) !== "IHDR")
      throw new Error("The PNG image header is invalid or truncated.");
    const u32 = (offset) => header[offset] * 16777216 + header[offset + 1] * 65536 +
      header[offset + 2] * 256 + header[offset + 3];
    const size = validateSize({ width: u32(16), height: u32(20) }, "PNG");
    if (size.width > 2147483647 || size.height > 2147483647) throw new Error("The PNG dimensions are invalid.");
    if (BigInt(size.width) * BigInt(size.height) > BigInt(MAX_REFERENCE_HEADER_PIXELS))
      throw new Error("The reference image dimensions exceed safety limits.");
    return size;
  }

  return { MAX_ROIS, MAX_REFERENCE_HEADER_PIXELS, parse, review, validateSize, checkAspectRatio, mapToSource, readJpegSize, readImageSize };
});
