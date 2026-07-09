# AGENTS.md

This file guides an agent tasked with rectifying patch-apply failures in this repository. Read it fully before acting.

## Context: how this repo is built

This repository hosts personal patches on top of upstream [VSCode](https://github.com/microsoft/vscode). The patches add remote-development enhancements (session persistence, instant reconnection, multiple transports, server daemon, REH connectors, etc.) and quality-of-life changes that cannot ship as extensions. The build has two relevant stages:

1. `./build-repo.sh` — clones upstream VSCode into `work/vscode` (if needed), fetches updates, deletes any existing `patched` branch, then applies our patches with `spm/spm.py patches work/vscode`. This is the stage where patch-apply happens. After applying, it performs extra non-spm steps: copies a custom icon, runs `scripts/updateelectron.py`, runs `scripts/doctor-product-info.py`, and refreshes submodules.
2. Packaging — `scripts/njgen.py` (generates a ninja script under `work/packaging`), `ninja` (builds the macOS app/REH), and `scripts/derivedreh.py` (cross-builds Linux REH packages). It does NOT apply patches. (Out of scope for this file — see Scope below.)

**Important:** Patch-apply errors surface only from the `spm/spm.py patches work/vscode` step inside `build-repo.sh`, not from the icon/electron/doctor steps and not from packaging. To reproduce an apply failure, run `spm/spm.py -vp patches work/vscode` (or `./build-repo.sh`) directly — do not waste time in the packaging scripts. If a failure occurs in the icon/electron/doctor steps, that is a separate problem outside patch-apply and should be reported separately.

**Scope:** This file covers diagnosing and fixing `spm` patch-apply failures only. Building/compiling VSCode and packaging (ninja/`derivedreh.py`) are out of scope except as a sanity check noted in Guardrails.

## How spm applies patches (mechanics the agent must know)

- `patches/patches.list` is the manifest. It contains:
  - `base-commit: <tag>` — the upstream **tag** (e.g. `1.123.0`) spm checks out before applying. Note this is a release tag, not a raw commit hash.
  - `final-commit: <hash|ignore>` — expected resulting commit hash (usually `ignore`).
  - an ordered, categorised list of `.patch` files (relative paths under `patches/`, must end in `.patch`).
- `spm/spm.py` does: `git checkout -b patched <base-commit>`, then for each patch: `git apply --index` (reading the patch from stdin) followed by a `git commit` whose author/committer name, email, and date are taken from the patch's `From:`/`Date:` headers.
- Failure symptom: spm prints `Error applying patch '<name>' cleanly.` (to stderr) and aborts with a non-zero exit. Because it fails mid-loop, the `patched` branch is typically left partially created (some patches applied and committed, the failing one not).
- `spm` is a git **submodule** (`spm/`, see `.gitmodules`). Ensure it is initialised (`git submodule update --init`) so `spm/spm.py` exists.

Because `base-commit` is pinned to a fixed upstream tag, a patch generally fails to apply for one of these reasons:
- The patch file itself is stale/broken (e.g., manually edited incorrectly).
- `base-commit` in `patches.list` was bumped to a newer VSCode release (together with `version.json`) without regenerating the patch — this is the normal reason patches break over time.
- The patch targets a feature that upstream has changed, renamed, removed, or already merged.

## Step 1 — Reproduce and diagnose

1. Run `spm/spm.py -vp patches work/vscode` from the repo root (the `-vp` flag makes `git apply` verbose so you can see exactly which hunk fails).
2. Identify the failing patch file and the feature it targets. Read its `Subject:` line and its diff to understand what behavior it adds/changes.
3. Clean up the half-applied state before retrying: the `patched` branch may already exist. `build-repo.sh` deletes it automatically, but if you run spm directly, first delete it yourself:
    - `cd work/vscode && git checkout main 2>/dev/null; git branch -D patched; cd ..`
    - Note: VSCode's default branch is `main` (not `master`).
    - If the working tree is dirty or has leftover debris (e.g. `.rej` files from a `git apply --reject` experiment, or the extra icon/electron/doctor commits from a prior `build-repo.sh` run), deleting the branch is not enough. Use `./clean-repo.sh` to fully reset `work/vscode` to a pristine checkout of the base tag (`git reset --hard HEAD` + `git clean -fd`). It is interactive — it prints a warning and requires you to type `yes` before proceeding. Passing `deep` additionally removes `.gitignore`'d files (build artifacts, `node_modules`); only use `deep` when you intend to discard those too, since recovering them requires a rebuild.
4. Note the upstream version context: because `work/vscode` is cloned with `--depth 1` and only the pinned tag is fetched, the working tree is at that tag after a fresh spm run. When investigating "did upstream change this?", you normally only have the pinned tag available locally. To compare against other upstream versions you must fetch them explicitly (e.g. `cd work/vscode && git fetch --depth 1 origin <tag>` or a deeper fetch). Do not assume you can diff against current upstream `main` without fetching first.

## Step 2 — Investigation decision tree (the core of the task)

For the failing patch, determine WHY it fails. Work through these cases in order. Be rigorous: do not jump to conclusions.

**(A) Context drift only.** The feature and all symbols the patch touches still exist, but line numbers / surrounding whitespace / nearby code changed, so the hunk context no longer matches. This is the most common failure when `base-commit` is bumped to a newer release.
- Fix: regenerate the patch against the new base so it applies cleanly. (See Resolution below.) → **AUTO-FIX.**

**(B) Feature already exists upstream (change merged).** Upstream has adopted the same behavior, so the lines the patch wants to add are already present (or functionally equivalent), causing `git apply` to fail (e.g., "already exists" / context mismatch on an insertion).
- Do NOT try to force the patch in. Report to the user that the change is now obsolete/upstreamed. → **REPORT ONLY.**

**(C) Feature renamed / refactored / moved.** The functionality the patch wants still exists, but under a different symbol name, different file, or different structure.
- **Critical caution:** Before concluding a feature was removed, you MUST scrutinize the code thoroughly. Search the `work/vscode` tree (grep across `src/`, `extensions/`, `build/`, `product.json`, `package.json`, etc.) for the patched identifiers, option names, function names, and surrounding logic. Upstream frequently renames options, moves code between files, or refactors interfaces — especially across major releases. A patch failing does NOT mean the feature is gone.
- If you find the target was renamed/refactored/moved: update the `.patch` so it points at the new symbol/location/structure. → **AUTO-FIX.**
- If after thorough search you genuinely cannot find any trace of the feature or its equivalents, only then treat it as case (D).

**(D) Feature genuinely removed.** The targeted functionality no longer exists anywhere in upstream and there is no renamed/refactored/moved equivalent.
- The patch is no longer applicable. Report to the user that the patch targets a feature that has been removed from upstream. → **REPORT ONLY.**

**When still unsure: ASK.** After exhausting the search in (C) and working through (A)–(D), if you are still genuinely unsure whether the patch should be auto-fixed, reported as obsolete/removed, or adapted, STOP and ask the user what to do with that specific patch. Never silently drop, force, or guess at a patch when uncertain — asking is always preferable to losing or breaking functionality.

General caution: never declare a feature "removed" (case D) without first exhausting case (C) — i.e. grep for renamed/refactored/moved variants. Prematurely dropping a patch loses functionality.

## Step 3 — Resolution actions

For **(A)** and **(C)** (auto-fix):
- Edit or regenerate the failing `.patch` file under `patches/` so it applies cleanly against the current `base-commit` (or against the newly bumped base-commit if that is the intent). Preferred approach:
  - Apply the patch with `git apply --reject` to get a `.rej` showing what failed, then manually incorporate the intended change at the correct new location, then regenerate the patch via `git diff` (or by committing and using `git format-patch`). Keep the patch in the same `git format-patch`-style format spm expects (proper `From:`/`Date:`/`Subject:` headers and `a/` `b/` paths).
  - **Refresh the date metadata:** when you modify a patch, update its `Date:` header to the current date/time. spm reads that `Date:` line to set `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` on the resulting commit, so refreshing it keeps the patch current. Keep the `From:` author and `Subject:` intact; only the timestamp should change.
- Re-run `spm/spm.py -vp patches work/vscode` (and clean up the `patched` branch first) to confirm all patches now apply cleanly.

For **(B)** and **(D)** (report only):
- Tell the user clearly: (B) the change is already upstreamed/obsolete, or (D) the targeted feature has been removed from upstream.
- Recommend removing the corresponding entry from `patches/patches.list` (and the `.patch` file if appropriate). **Do NOT delete the patch or edit `patches.list` without the user's confirmation** — only report and propose.

After fixing:
- Re-run `./build-repo.sh` to confirm the patched branch applies cleanly end-to-end (including the icon/electron/doctor steps). A patch that "applies" but breaks the subsequent steps is not a real fix — report those separately.

## Guardrails

- Never silently delete patches or remove entries from `patches.list`. Report removal cases (B/D) and obtain user confirmation.
- Do not change `base-commit` casually. Bumping `base-commit` means moving to a newer VSCode release and is done together with `version.json` (`productVersion`/`ipcVersion`/`electronVersion`). If a bump is what triggered the failure, that is a deliberate action — confirm with the user and expect to fix/regenerate affected patches accordingly.
- Preserve patch metadata integrity: keep `From:` author and `Subject:` when regenerating; refresh `Date:` to current time for modified patches (preferred).
- Verify the end result actually applies: re-run `./build-repo.sh` to confirm a clean `patched` branch before declaring success. Building/compiling VSCode or running packaging is normally out of scope, but a patch that fails the later build/reh steps is not a real fix — surface it rather than claiming success.
- If a failed investigation leaves `work/vscode` in a messy state (dirty tree, leftover `.rej` files, partial `patched` commits), use `./clean-repo.sh` to hard-reset it to the base tag before re-running spm. Remember `deep` additionally wipes `.gitignore`'d files such as `node_modules` and build output.
- When in doubt about whether a feature was removed vs. moved, search more. If still unsure after full scrutiny, **ask the user** what to do with the patch. Incorrectly dropping a working patch is worse than asking.
