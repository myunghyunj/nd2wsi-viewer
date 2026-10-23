# Icons8 Windows 10 overlay controls

The overlay uses the Windows 10 icon family requested for the viewer. Assets are
bundled locally so opening and operating the overlay does not contact a CDN.

## Lock, unlock, close and navigation

`lock-2.svg`, `unlock-2.svg`, `chevron-left.svg` and
`chevron-right.svg` are unmodified upstream SVGs from
[Icons8 Windows 10 Icons](https://github.com/icons8/windows-10-icons), revision
`bb4b59b3cf8fd21b76ea21a45d4a23b497ef3df0`:

- `svg/production/lock_2.svg` → `lock-2.svg`
- `svg/production/unlock_2.svg` → `unlock-2.svg`
- `svg/production/chevron_left.svg` → `chevron-left.svg`
- `svg/production/chevron_right.svg` → `chevron-right.svg`

These correspond to the `icons8-lock-2`, `icons8-unlock-2`,
`icons8-chevron-left` and `icons8-chevron-right`
glyphs in the [requested font preview](https://icons8.github.io/windows-10-icons/font/).
They are used under the MIT option in the accompanying upstream `LICENSE.md`.
Copyright (c) 2025 Icons8.

`close.svg` is adapted from that same revision's `svg/production/cancel.svg`
(`icons8-cancel`). Its surrounding circle is removed, its original X-shaped
subpath is retained, and the viewBox is cropped to give the standalone X
appropriate padding. This modification is permitted by the MIT license.
The similarly named `cancel_2.svg` is a prohibition symbol, so it is not used.

## Cloud

The font snapshot does not contain a cloud glyph. `cloud.png` is the matching
[Windows 10 Cloud icon](https://icons8.com/icon/17122/cloud), downloaded unchanged
at 96 × 96 pixels from
<https://img.icons8.com/windows/96/000000/cloud.png> on 2026-10-23.

The cloud image is **not covered by the MIT license above**. It is used under
[Icons8's free attribution terms](https://icons8.com/license). Keep the visible
Icons8 link in the app's information/About area when distributing this asset.
Icons8 permits free PNG use with attribution; SVG for this additional icon
requires a separate license. See the
[renderer format guidance](https://developers.icons8.com/docs/icons-renderer).

## Rendering

Use these monochrome assets as CSS masks with `background: currentColor`.
The SVG lock and unlock paths have no background, and the PNG includes alpha.
The mask keeps the requested lock glyphs white without adding a button plate.
