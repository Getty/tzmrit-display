---
name: tzmrit-display-packaging
description: The two release pipelines for tzmrit-display — the Linux path (setuptools-scm tag → sdist/wheel, systemd unit, udev rule, dialout) and the Windows path (PyInstaller two-exe freeze → NSIS per-user installer, the .bat scripts, CI). Use when auditing, building or debugging packaging/release on either platform.
---

# tzmrit-display — packaging & release

Two independent release pipelines share one repo and **one version source**. The
runtime code they package is described in skill `tzmrit-display-core`; this skill
is only the packaging chain around it.

## The single version source — both platforms

The version comes from the **git tag** via `setuptools-scm` (`v0.2.0` → `0.2.0`;
between tags `0.2.1.devN+g<hash>`; git-less tarball → `fallback_version = 0.0.0`).
Nothing declares a version by hand. Each pipeline re-derives the same number:

- **Wheel/sdist** — setuptools-scm writes it at build time from the tag.
- **Windows installer** — `packaging/build.bat` reads it back with
  `importlib.metadata.version("tzmrit-display")` from the freshly `pip install -e`'d
  package and passes it to NSIS as `/DVERSION`; `installer.nsi` **hard-errors**
  (`!error`) if `VERSION` is undefined, so the installer can never build with a
  drifted or missing version.

> **Trap — build from a real tag.** An untagged or dirty checkout yields
> `0.0.0` or `X.Y.Z.devN+g<hash>`. That string flows straight into the installer
> filename `tzmrit-display-setup-<version>.exe` and the NSIS `OutFile`/registry
> `DisplayVersion`. A clean release artifact requires HEAD to be exactly on a
> `v*` tag. `git describe --tags` before building; `+local`/`.dev` in the name
> means you are not on a tag.

## Releasing = pushing a `v*` tag — never without permission

There is **no manual publish step and no PyPI upload job**. `.github/workflows/ci.yml`:

- `test` — pytest on `ubuntu-latest` + `windows-latest`, Python 3.13.
- `installer` — `needs: test`, runs `packaging\build.bat` on Windows, uploads the
  `.exe` artifact always, and on a `refs/tags/v*` push **attaches it to the GitHub
  release** (`softprops/action-gh-release`, `contents: write`).

So pushing a `v*` tag *is* the release: it builds and publishes the installer asset
under the maintainer's account. `fetch-depth: 0` in both jobs is load-bearing —
without full history setuptools-scm cannot see the tag. Auditors: **never create or
push a tag, never trigger the release**; report readiness only.

---

## Linux pipeline

Source install + optional system integration; there is no distro package.

- **Install** — `pip install -e .` into a venv (`README` walks it). Runtime access
  to `/dev/ttyACM*` needs the `dialout` group.
- **`systemd/tzmrit-display.service`** — a user-level `WantedBy=default.target` unit.
  Paths are hardcoded to `%h/dev/display/.venv` and `%h/dev/display` (the local clone
  directory, not the package name); the header comment says to adjust both if the
  checkout moves. `StartLimitIntervalSec=0` + `Restart=on-failure` is deliberate: the
  USB port may not exist at boot, so retries must never give up. `ExecStart` runs
  `-m tzmrit_display run ...`.
- **`systemd/99-hongtai-panel.rules`** — udev fallback for `dialout` not being
  enough. **Must list every supported product id** (`7791` and `7792` today);
  a new panel id added to the code without a matching rule line is a gap.
- **sdist/wheel** — `python -m build`. Must carry `tzmrit_display/fonts/` (theme.py
  loads them at runtime) via `[tool.setuptools.package-data]`;
  `[tool.setuptools] packages = ["tzmrit_display"]`.

Audit points: unit paths still valid, udev ids match the code's supported set,
`dialout`/access story in README current, `python -m build` clean with fonts present,
version strategy untouched.

## Windows pipeline

Frozen app + NSIS installer; no Python needed on the target.

- **`packaging/tzmrit-display.spec`** — one PyInstaller `Analysis` → **two exes**:
  `tzmrit-display.exe` (console, `console=True`) and `tzmrit-displayw.exe` (windowed,
  `console=False`, what autostart/shortcuts run). Entry point `packaging/launcher.py`
  (= the `tzmrit_display.cli:main` console script). `excludes=["tkinter"]`,
  `hiddenimports=[]`.
- **Frozen fonts coupling** — `datas=[("../tzmrit_display/fonts","tzmrit_display/fonts")]`
  lands the fonts in `_internal/tzmrit_display/fonts`, mirroring the checkout's
  package-relative `parent/"fonts"` layout so `theme.py` needs no frozen-vs-source
  branch. **If theme.py's font path resolution changes, this `datas` mapping must
  change with it** — the classic freeze break is fonts present in dev, missing
  (black screen) in the installed app.
- **`packaging/build.bat`** — the whole Windows build: venv, `pip install -e . pyinstaller`,
  read version, `pyinstaller --noconfirm --clean tzmrit-display.spec`, then
  `makensis /DVERSION=... installer.nsi`. Needs NSIS (`winget install NSIS.NSIS`).
- **`packaging/installer.nsi`** — per-user (`RequestExecutionLevel user`), installs to
  `$LOCALAPPDATA\Programs`, registers an uninstaller in Apps & Features, Start-menu
  shortcuts, and a one-of-many "Start at logon" radio group (defaults to the Claude
  view). Uninstall runs `tzmrit-display.exe clear` to blank the panel. `APPNAME` and
  the installer `OutFile` are `tzmrit-display`.
- **`install.bat` / `uninstall.bat`** (repo root) — the *no-installer* path: run from
  source, make a venv, create a Startup-folder shortcut to `pythonw.exe -m tzmrit_display run`.
  `install.bat` calls `tzmrit-display.exe info`; `uninstall.bat` calls `clear`.

**CLI-surface coupling:** the scripts and NSIS depend on the CLI subcommands
`run`, `run --claude`, `info`, `clear` existing and keeping their meaning. Renaming
or removing one silently breaks a shortcut, the installer, or the uninstaller — grep
`packaging/`, `install.bat`, `uninstall.bat` before touching the argparse surface in
`cli.py`.

Audit points: spec `datas` still matches theme.py's font resolution, the two-exe
split intact, `hiddenimports` covers any new dynamic import a dep introduced, the
`info`/`clear`/`run` references still resolve, `build.bat`/CI still green, version
plumbing (`importlib.metadata` → `/DVERSION` → `!error` guard) untouched.

## Verification without the target platform

Neither a panel nor a second OS is attached here. `pytest` and `python -m build` run
anywhere; `packaging\build.bat` and `makensis` are Windows-only — read them for drift,
do not claim a Windows installer builds unless it built on Windows (CI's `installer`
job is the real proof).
