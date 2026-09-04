/* Plate-label helpers with no DOM dependency. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2PlateUI = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function parseWellName(name) {
    if (typeof name !== "string") return null;
    const match = /^([A-Za-z]+)0*([1-9]\d*)$/.exec(name.trim());
    if (!match) return null;
    return { row: match[1].toUpperCase(), col: match[2] };
  }

  function headersFor(placed, rowCount, colCount, rowPart, colPart) {
    const rowHeaders = Array(rowCount).fill(null);
    const colHeaders = Array(colCount).fill(null);
    const occupied = new Set();
    const namedWells = new Set();

    for (const site of placed) {
      if (!site || typeof site !== "object") return null;
      const row = Number(site.row);
      const col = Number(site.col);
      if (!Number.isInteger(row) || row < 0 || row >= rowCount) return null;
      if (!Number.isInteger(col) || col < 0 || col >= colCount) return null;

      const cellKey = row + "/" + col;
      if (occupied.has(cellKey)) return null;
      occupied.add(cellKey);

      const well = parseWellName(site.name);
      if (!well) return null;
      const wellKey = well.row + "/" + well.col;
      if (namedWells.has(wellKey)) return null;
      namedWells.add(wellKey);

      const rowLabel = well[rowPart];
      const colLabel = well[colPart];
      if (rowHeaders[row] !== null && rowHeaders[row] !== rowLabel) return null;
      if (colHeaders[col] !== null && colHeaders[col] !== colLabel) return null;
      rowHeaders[row] = rowLabel;
      colHeaders[col] = colLabel;
    }

    // Do not invent headings for a visual track that has no site from which
    // to derive one, and reject repeated headings that would be ambiguous.
    if (rowHeaders.some((label) => label === null)) return null;
    if (colHeaders.some((label) => label === null)) return null;
    if (new Set(rowHeaders).size !== rowHeaders.length) return null;
    if (new Set(colHeaders).size !== colHeaders.length) return null;
    return { rows: rowHeaders, cols: colHeaders };
  }

  function wellHeaders(placed, rows, cols) {
    const rowCount = Number(rows);
    const colCount = Number(cols);
    if (!Array.isArray(placed) || placed.length === 0) return null;
    if (!Number.isInteger(rowCount) || rowCount < 1) return null;
    if (!Number.isInteger(colCount) || colCount < 1) return null;

    // Stage coordinates may be transposed, reversed, or rotated relative to
    // the well names. Accept whichever parsed component is constant along
    // each visual axis instead of assuming that X always means well column.
    return headersFor(placed, rowCount, colCount, "row", "col")
      || headersFor(placed, rowCount, colCount, "col", "row");
  }

  function focusReady(map, t, p) {
    return map?.complete?.[t]?.[p] === true;
  }

  function displayedZ(map, t, p, manual, totalZ, auto) {
    const best = map?.best?.[t]?.[p];
    return auto && focusReady(map, t, p) && Number.isInteger(best)
      && best >= 0 && best < totalZ ? best : manual;
  }

  function focusSummary(map, t, sites, manual, totalZ, auto) {
    const planes = sites.map((p) => displayedZ(map, t, p, manual, totalZ, auto));
    return {
      ready: sites.filter((p) => focusReady(map, t, p)).length,
      total: sites.length,
      min: planes.length ? Math.min(...planes) : manual,
      max: planes.length ? Math.max(...planes) : manual,
    };
  }

  function nextGridSite(placed, current, key) {
    const site = placed.find((s) => s.i === current) || placed[0];
    if (!site) return null;
    if (key === "Home") return placed.find((s) => s.row === site.row)?.i ?? site.i;
    if (key === "End") return placed.filter((s) => s.row === site.row).at(-1)?.i ?? site.i;
    const delta = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] }[key];
    if (!delta) return null;
    const candidates = placed.filter((s) => delta[0]
      ? s.col === site.col && (s.row - site.row) * delta[0] > 0
      : s.row === site.row && (s.col - site.col) * delta[1] > 0);
    candidates.sort((a, b) => Math.abs(a.row - site.row) + Math.abs(a.col - site.col)
      - Math.abs(b.row - site.row) - Math.abs(b.col - site.col));
    return candidates[0]?.i ?? site.i;
  }

  return { parseWellName, wellHeaders, focusReady, displayedZ, focusSummary, nextGridSite };
});
