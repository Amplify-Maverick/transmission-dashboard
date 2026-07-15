# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two channels track this file:
- **Beta** users are on the `main` branch and get `[Unreleased]` changes as they land.
- **Stable** users are on the `stable` branch and get changes when a version below is tagged.

See [docs/RELEASING.md](docs/RELEASING.md) for how releases are cut.

## [Unreleased]

_Changes on `main` that haven't been promoted to a stable release yet._

## [1.0.0] - 2026-07-15

First stable release.

### Added
- Torrent dashboard: live list, per-torrent and global up/down speeds.
- Disk and system stats.
- Event / history log backed by SQLite.
- Media copy of finished downloads to a media server over SSH/rsync, with
  Plex/Jellyfin library refresh.
- Optional WireGuard tunnel-health indicator in the topbar.
- Mullvad WireGuard binding guide in Settings.
- Mobile (iPhone Safari) layout: bottom tabs, compact cards, action sheet,
  fitted modals.
- `update.sh` for safe in-place updates (`git pull --ff-only`, reinstall deps,
  restart service). Config, database, and runtime state are gitignored and
  untouched by updates.

<!--
Template for future entries — keep newest at the top, under [Unreleased] while
in progress, then move to a versioned section when tagged:

## [X.Y.Z] - YYYY-MM-DD

### Breaking / Action required
- Describe the manual step a user must take. Forces a MAJOR bump.

### Added
### Changed
### Fixed
### Removed
-->
