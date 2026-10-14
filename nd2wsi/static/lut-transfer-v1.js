/* Display-only LUT clipboard interchange. Channel identity is never inferred by index. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2LutTransfer = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const FORMAT = "nd2wsi-lut/1";
  const MAX_BYTES = 128 * 1024;
  const MAX_CHANNELS = 512;
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const labelOf = channel => typeof channel?.label === "string" ? channel.label.trim() : "";
  const dtypeOf = dtype => typeof dtype === "string" ? dtype.trim() : "";

  function currentLut(channel, lut) {
    const value = lut ?? { lo: channel?.window?.start, hi: channel?.window?.end, gamma: 1 };
    return { lo: value.lo, hi: value.hi, gamma: value.gamma };
  }

  function validateLut(lut, { dtype } = {}) {
    // Even integer images can have fractional metadata defaults. Preserve exact
    // values here; the existing LUT widget owns interactive range constraints.
    return record(lut) && [lut.lo, lut.hi, lut.gamma].every(Number.isFinite) &&
      lut.hi > lut.lo && Number.isFinite(lut.hi - lut.lo) &&
      lut.gamma >= 0.25 && lut.gamma <= 4;
  }

  function fitsChannel(channel, lut, dtype) {
    if (!validateLut(lut)) return false;
    const discrete = /^(?:u?int\d+|bool|[<>=|]?[iu]\d+)$/.test(dtypeOf(dtype));
    if (!discrete || lut.hi - lut.lo >= 1) return true;
    // setLut preserves an exact fractional default, but expands other integer
    // ranges to one intensity unit. Skip those rather than silently changing it.
    return lut.lo === channel?.window?.start && lut.hi === channel?.window?.end && lut.gamma === 1;
  }

  function byteLength(text) {
    // Count UTF-8 bytes without relying on browser clipboard or Node APIs.
    let bytes = 0;
    for (const character of text) {
      const code = character.codePointAt(0);
      bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
      if (bytes > MAX_BYTES) break;
    }
    return bytes;
  }

  function checkSize(text) {
    if (text.length > MAX_BYTES || byteLength(text) > MAX_BYTES)
      throw new Error("LUT clipboard data is too large (maximum 128 KiB).");
  }

  function encode(channels, luts, dtype) {
    if (!Array.isArray(channels) || channels.length > MAX_CHANNELS)
      throw new Error("LUT clipboard requires at most 512 channels.");
    if (!Array.isArray(luts)) throw new Error("LUT values must be a channel array.");
    if (dtype != null && typeof dtype !== "string") throw new Error("Invalid LUT pixel type.");
    const payload = { format: FORMAT, dtype: dtypeOf(dtype) || null,
      channels: channels.map((channel, index) => {
        const lut = currentLut(channel, luts[index]);
        if (!validateLut(lut)) throw new Error("Channel " + (index + 1) + " has invalid LUT values.");
        return { label: labelOf(channel), ...lut };
      }) };
    const text = JSON.stringify(payload);
    checkSize(text);
    return text;
  }

  function labelCounts(channels) {
    const counts = new Map();
    channels.forEach(channel => {
      const label = labelOf(channel);
      if (label) counts.set(label, (counts.get(label) || 0) + 1);
    });
    return counts;
  }

  function planPaste(text, targetChannels, targetDtype) {
    if (typeof text !== "string") throw new Error("LUT clipboard data must be text.");
    checkSize(text);
    let payload;
    try { payload = JSON.parse(text); }
    catch (_) { throw new Error("Clipboard does not contain valid LUT JSON."); }
    if (!record(payload) || payload.format !== FORMAT)
      throw new Error("Clipboard is not a supported ND2 LUT format.");
    if (!Array.isArray(payload.channels) || payload.channels.length > MAX_CHANNELS)
      throw new Error("LUT clipboard requires at most 512 channels.");
    if (payload.dtype != null && typeof payload.dtype !== "string")
      throw new Error("Invalid LUT clipboard pixel type.");
    if (!Array.isArray(targetChannels)) throw new Error("Target channels are unavailable.");

    const updates = [];
    const sourceDtype = dtypeOf(payload.dtype), destinationDtype = dtypeOf(targetDtype);
    if (sourceDtype && destinationDtype && sourceDtype !== destinationDtype)
      return { updates, skipped: payload.channels.length };

    const sourceCounts = labelCounts(payload.channels), targetCounts = labelCounts(targetChannels);
    const targetIndices = new Map();
    targetChannels.forEach((channel, index) => {
      const label = labelOf(channel);
      if (label && targetCounts.get(label) === 1) targetIndices.set(label, index);
    });
    payload.channels.forEach(channel => {
      const label = labelOf(channel);
      if (!label || sourceCounts.get(label) !== 1 || !targetIndices.has(label)) return;
      const index = targetIndices.get(label);
      if (!fitsChannel(targetChannels[index], channel, destinationDtype)) return;
      updates.push({ index, lut: currentLut(null, channel) });
    });
    return { updates, skipped: payload.channels.length - updates.length };
  }

  return { currentLut, validateLut, fitsChannel, encode, planPaste };
});
