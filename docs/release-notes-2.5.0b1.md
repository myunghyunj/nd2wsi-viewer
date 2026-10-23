# nd2wsi-viewer 2.5 Beta 1

This prerelease adds a translucent tracing window to the standard desktop viewer, on top of the 2.4.1 release.

- **Collapsible top tools:** expand with the Icons8 **angle-left** icon to show cloud, Astromapper, Shading Correction, Compare slides and Slide Info; collapse with **angle-right**. Channels uses **timeline**, and Compare slides uses **share**.
- **New window:** use **⌘N** on macOS or **Ctrl+N** on Windows to open a separate viewer window with its own tabs.
- **Cloud button:** open the current fluorescence image above other apps, using the same channels, LUTs, selected plane, rotation and scale. The main viewer can be minimized while the tracing window remains visible.
- **Fixed scale when resizing:** changing the window size changes only the field of view. Drag the image to pan; wheel, trackpad or pinch to zoom. Drag the empty top edge or Option/Alt-drag to move the window.
- **Adjustable opacity:** a slim slider adjusts opacity from 10% to 100%, starting at 55%.
- **Minimal controls:** white, backgroundless lock and close icons. Lock freezes the image, opacity, ROI navigation, movement and resizing; unlock and close remain available.
- **Optional ROI navigation:** imported AstroMapper regions add `‹ number ›`. A matching small whole-image JPG and TXT can also be dropped into the tracing window, or selected with right-click / Cmd/Ctrl+O. Arrows center the next ROI without changing scale. Without coordinates, the arrows and number are hidden.
- **Existing data preserved:** navigation reuses the current AstroMapper coordinate mapping, including inclusive end pixels. The reference JPG is used for dimensions; the current viewer's image stays on screen. No source image, saved ROI or annotation is overwritten.

The tracing window uses a snapshot of display settings taken when the cloud button is pressed. Reopen it to copy later channel, LUT or plane changes. Keep the source tab open. The standard viewer supports fluorescence ND2 and small JPG/PNG tabs; the experimental Metal window and ordinary browser tabs do not expose this native mode.

Use the corresponding uncropped JPG in the same orientation. Matching aspect ratios do not prove that unrelated images are registered, and a crop cannot be corrected from dimensions alone.

On macOS the slider changes the image layer's opacity while white controls keep their normal appearance. Windows uses native whole-window opacity, so its controls are translucent too. The beta is distributed as a GitHub prerelease and is not added to the stable update feed.
