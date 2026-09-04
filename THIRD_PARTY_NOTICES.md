# Third-party notices

## Runtime dependencies: none

This project has **zero third-party runtime dependencies**. Every module is
Python standard library only.

There is therefore nothing to reproduce in this file — no bundled code, no
vendored libraries, no binaries, and no accompanying licence texts required.

That is a deliberate design constraint rather than an accident of scope. See
[`LICENCE-NOTES.md`](LICENCE-NOTES.md) for the reasoning, which is mostly about
not inheriting obligations you did not plan for.

## External tools invoked, not redistributed

The harness shells out to the following. None is bundled, vendored or shipped
with this repository; each must already be present on the user's system, under
whatever terms the user obtained it.

| Tool | Used for | Obtained by |
|---|---|---|
| `ffmpeg` | loudness analysis, frame extraction | user's own installation, discovered on `PATH` |
| `ffprobe` | container and stream inspection | user's own installation, discovered on `PATH` |
| agent runtime | driving the skill under test | user-configured; see the adapter block in `README.md` |
| judge model endpoint | LLM scoring passes | user-configured HTTPS endpoint |

Invoking a program as a subprocess is not distributing it, so none of these
places a licence obligation on this repository. If you later bundle any of them
into a container image or an installer, that calculus changes and the
obligations of the bundled build apply — read `LICENCE-NOTES.md` §2 first,
because two commonly used FFmpeg npm packages are traps.

## If you add a dependency

Add a row below with the package, version, licence, and a one-line reason it
was necessary. Reproduce its licence text where its terms require it.

Before adding, check the two failure modes documented in `LICENCE-NOTES.md`:

1. A wrapper package whose **declared licence differs from the licence of the
   binaries it ships**.
2. A package containing a build configured `--enable-nonfree`, which is not
   redistributable at all.

<!-- Add entries here. Keep the table sorted by package name.

| Package | Version | Licence | Why needed |
|---|---|---|---|

-->
