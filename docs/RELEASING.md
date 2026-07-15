# Releasing

This project ships on two channels, distinguished only by **which git branch a
user has checked out**. Both channels update the same way the user always
has — by running `./update.sh` (which does `git pull --ff-only`, reinstalls
deps, and restarts the service).

| Channel    | Branch   | Who it's for                                            |
|------------|----------|---------------------------------------------------------|
| **Beta**   | `main`   | People who want the latest changes and can tolerate the occasional rough edge. Updated continuously. |
| **Stable** | `stable` | People who want a tested, deliberately promoted build. Updated only when a release is cut. |

There is no fixed calendar. Releases are cut **when a coherent, tested set of
changes is ready** — not on a schedule.

## How a user picks a channel

Nothing about their workflow changes except a one-time branch switch:

```bash
# Switch an existing checkout to stable (do this once):
git fetch origin
git checkout stable

# From then on, updates are the same as always:
./update.sh
```

To move back to beta: `git checkout main && ./update.sh`.

## Cutting a stable release (maintainer)

1. **Make sure `main` is in a good state.** Everything you intend to release is
   merged to `main` and pushed.

2. **Smoke-test the actual app.** Config/DB/state are gitignored, so this is
   safe to do on a real checkout. At minimum:
   - App starts and the dashboard loads.
   - Transmission connection works (torrent list + live speeds update).
   - Anything you changed this cycle actually works end-to-end.

3. **Update the changelog.** Move the `## [Unreleased]` entries into a new
   `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`. Flag anything that
   requires manual action under a **Breaking / Action required** heading (new
   env var, config-format change, on-disk/schema change). Commit it to `main`.

4. **Tag `main` with the version:**
   ```bash
   git checkout main
   git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

5. **Promote `stable` to the tag** (fast-forward, so stable users' `--ff-only`
   pull stays clean):
   ```bash
   git checkout stable      # create it the first time: git checkout -b stable vX.Y.Z
   git merge --ff-only vX.Y.Z
   git push origin stable
   git checkout main
   ```

6. **Tell users**, if you have a channel to (release note, chat). Point them at
   the changelog. Call out breaking changes loudly.

## Versioning (SemVer)

Given a version `MAJOR.MINOR.PATCH`:

- **MAJOR** — a user must do something manual to update (set a new env var,
  migrate config, change how they run it). Anything under "Breaking / Action
  required" forces a major bump.
- **MINOR** — new feature, no manual action needed.
- **PATCH** — bug fix or internal change, no manual action needed.

When in doubt between minor and patch, it doesn't matter much — but **never**
hide a breaking change in a minor/patch bump. The whole point of the stable
channel is that `./update.sh` is safe unless the major version changed.

## First release

There are no tags yet. The first stable release is `v1.0.0`:

```bash
git checkout main && git pull --ff-only
git tag -a v1.0.0 -m "v1.0.0"
git push origin v1.0.0
git checkout -b stable v1.0.0
git push -u origin stable
git checkout main
```
