# Vendored browser code

The board has no build step, so third-party browser code is copied here as released and never edited.

## DOMPurify

| | |
|---|---|
| Version | 3.4.17 (the version string is in the first line of the file) |
| File | `purify.min.js`, the standalone `dist/purify.min.js` of the npm package `dompurify@3.4.17` |
| Source | https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz (the same bytes as https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js) |
| SHA-256 | `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31` |
| License | Apache-2.0 OR MPL-2.0 (Cure53 and other contributors); the Apache-2.0 text is in `DOMPURIFY-LICENSE` |

The board uses it as a second pass over `cleanFragment` output, the HTML of decision mocks, which is rendered in the board
itself and not in a sandboxed frame. An artifact block's `title`, `text` and `caption` are plain text (`textContent`), so
they never reach it. HTML inside an artifact's sandboxed page is not sanitised either; it has to run script.

To update: download the new version, check the version string and the SHA-256 against the npm registry, replace the file,
and change this table and `tests/test_board_artifact.py`, which pins the hash.

## SnapDOM

| | |
|---|---|
| Version | 3.7.4 (the version string is in the first line of the file) |
| File | `snapdom.min.js`, the IIFE build `dist/snapdom.js` of `@zumer/snapdom@3.7.4` (global `window.snapdom`) |
| Source | https://registry.npmjs.org/@zumer/snapdom/-/snapdom-3.7.4.tgz |
| SHA-256 | `3274620b04a65f42f972dc5de97530b8929d7a6e3bbe922130af6263b8da5ab7` |
| License | MIT (Zumer); the text is in `SNAPDOM-LICENSE` |

Only the PNG capture page (`/r/<id>/export/capture`) loads it. The board itself does not.

## Turndown

| | |
|---|---|
| Version | 7.2.5 (the UMD browser build) |
| File | `turndown.min.js`, the browser UMD build `lib/turndown.browser.umd.js` of `turndown@7.2.5` (global `TurndownService`, no `domino`) |
| Source | https://registry.npmjs.org/turndown/-/turndown-7.2.5.tgz |
| SHA-256 | `ead805af2012a8434b58f1dab07b452370a7dcd3e9665ff64fc1d61dd2e38e9a` |
| License | MIT (Dom Christie); the text is in `TURNDOWN-LICENSE` |

The board loads it as a classic script (global `TurndownService`) before `board.js` and converts the fetched
export document to Markdown with no build step.
