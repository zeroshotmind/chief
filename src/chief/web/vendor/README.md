# vendor/

Third-party code Chief serves as static files rather than fetching at runtime — REQ-21
(self-hosted, one file, no daemon) applies to the browser side too: nothing here reaches
out to a CDN once the page has loaded.

- `mermaid.min.js` — Mermaid 11.17.0, the official single-file UMD build (unmodified). It
  sets `window.mermaid`. Fetched from
  `https://cdn.jsdelivr.net/npm/mermaid@11.17.0/dist/mermaid.min.js`. See
  `mermaid.LICENSE.txt` (MIT).

To update: download the new version's `dist/mermaid.min.js` from jsdelivr or npm, overwrite
this file, and bump the version named here and in `app.js`'s loader.
