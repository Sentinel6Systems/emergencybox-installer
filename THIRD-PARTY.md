# Third-party code

The installer itself is GPL-3.0 (see `LICENSE`). These files are other projects' code, included
unchanged so the Books reader works offline:

| File | Project | License |
|---|---|---|
| `files/www/box/assets/vendor/jszip.min.js` | JSZip 3.10.1 | MIT or GPL-3.0 (dual; its header has the notice) |
| `files/www/box/assets/vendor/epub.min.js` | epub.js 0.3.93 (futurepress/epub.js) | BSD-2-Clause |

The installer downloads everything else from its own source when it runs (Debian packages,
Jellyfin, Calibre-Web, Kiwix, EmulatorJS, fieldmap-server); each keeps its own license.
