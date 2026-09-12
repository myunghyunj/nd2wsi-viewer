/* Display-only independent-window handoff. No annotations or role are copied. */
(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.Nd2ViewState = api;
})(typeof window === "undefined" ? null : window, function() {
  function capture(state) {
    const info = state.info, viewport = state.viewer?.viewport;
    if (!info || !viewport || info.rgb || info.kind === "plate") throw new Error("This slide requires the standard viewer");
    if (viewport.getRotation() !== 0 || viewport.getFlip()) throw new Error("Reset rotation and flip before opening in Metal");
    const bounds = viewport.viewportToImageRectangle(viewport.getBounds(true));
    const width = viewport.getContainerSize().x;
    return {version:1, source_dimensions:[info.width, info.height],
      center:[bounds.x + bounds.width / 2, bounds.y + bounds.height / 2], zoom:width / bounds.width,
      channels:info.channels.map((channel, i) => {
        const lut = state.luts[i] || {lo:channel.window.start, hi:channel.window.end, gamma:1};
        const color = channel.color.replace(/^#/, "");
        return {window:[lut.lo, lut.hi], gamma:lut.gamma,
          color:[0,2,4].map(offset => parseInt(color.slice(offset, offset+2), 16)),
          visible:state.channels.includes(i)};
      })};
  }
  function applyDisplay(state, display) {
    if (!display || display.version !== 1 || display.source_dimensions[0] !== state.info.width ||
        display.source_dimensions[1] !== state.info.height || display.channels.length !== state.info.channels.length)
      throw new Error("Display handoff does not match the opened slide");
    state.channels = [];
    state.luts = display.channels.map((channel, i) => {
      if (channel.visible) state.channels.push(i);
      state.info.channels[i].color = channel.color.map(v => v.toString(16).padStart(2,"0")).join("");
      return {lo:channel.window[0], hi:channel.window[1], gamma:channel.gamma};
    });
  }
  function applyCamera(viewer, display) {
    const size = viewer.viewport.getContainerSize();
    const width = size.x / display.zoom, height = size.y / display.zoom;
    const bounds = viewer.viewport.imageToViewportRectangle(
      display.center[0] - width/2, display.center[1] - height/2, width, height);
    viewer.viewport.fitBounds(bounds, true);
    viewer.forceRedraw();
  }
  return {capture, applyDisplay, applyCamera};
});
