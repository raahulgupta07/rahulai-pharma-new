# Release notes

Written for the people who use the console, not for engineers. One entry per
release, newest first. The top entry's version must match `VERSION` in
`app/version.py` — `tests/test_version.py` enforces it.

Format: `## <version> — <YYYY-MM-DD>`, then any of `### Fixed`, `### Added`,
`### Changed`, `### Security`. `app/release_notes.py` parses exactly that, and
the admin console renders it, so a heading it does not recognise is dropped.

## 1.0.0 — 2026-08-13

First numbered release. Everything before this shipped unversioned, so the
entries below cover the fixes made while adding versioning rather than a full
history.

### Fixed

- Answers to "do you have X" and "which branch has X" were being cut off
  mid-sentence, in both English and Burmese — the two questions asked most
  often. They now complete.
- Long answers — a full substitute list, or a price search — were cut off part
  way through the table.
- Answers arrive much faster: common stock questions now take about 5 seconds
  instead of 15, and long lists about 15 seconds instead of 35.
- The catalog page failed to open unless you typed a search term first.
- Refreshing the browser on the Users, Stores, Graph, Conversations or
  Learning page showed an error instead of the page.

### Security

- A staff account limited to one branch could see other branches' stock,
  prices and stock value on the inventory and stores pages. It now sees only
  its own branch.

### Changed

- The console now shows which version it is running, and these notes are
  readable in the console under Version.
