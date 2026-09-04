# Licence notes

Two things this repository does deliberately, and one thing that must be
settled before it is published.

---

## 1. Confirm the copyright holder before publishing

`LICENSE` names **Google LLC** as a placeholder. That has **not** been
verified. Before this repository is made public or handed to anyone outside
the team, someone must establish in writing that copyright vests where the
licence says it does.

This is not pedantry. A repository with no licence, or a licence naming a
holder nobody confirmed, cannot be relied on by a recipient: MIT's operative
condition is that the copyright notice travels with every copy, and a
recipient cannot comply with a notice that is wrong.

**Who settles it:** whoever handles open-source releases (an OSPO or
equivalent), together with the employment terms of the contributors. It is
usually a quick determination, but it has to actually happen.

Until it does, treat this repository as internal-use-only.

---

## 2. We bundle no media binaries, on purpose

This harness measures video, so the obvious move would be to depend on
`ffmpeg-static` and `ffprobe-static` and get hermetic binaries for free. We
deliberately do not, for two specific reasons.

### `ffprobe-static` declares MIT and ships GPLv3 binaries

The package's only licence text is MIT. The binaries inside it are built
`--enable-gpl --enable-version3`. Those are different licences with very
different obligations, and the mismatch is the package's, not the consumer's
— but the obligations still land on whoever redistributes it.

Worse, several of those binaries came from a distribution point that shut down
in 2020. GPLv3 section 6 puts a three-year corresponding-source obligation on
the conveyor, and "corresponding source" for a static FFmpeg build means FFmpeg
plus every statically linked library at its exact revision. For a build whose
origin no longer exists, that obligation ranges from expensive to impossible.

### `ffmpeg-static` ships a non-redistributable macOS binary

Its macOS arm64 asset is built `--enable-nonfree`. FFmpeg's own licence
documentation states plainly that this makes the resulting binary
unredistributable. There is no cure: no licence text, no source offer and no
fee changes it, because FFmpeg is not available under alternative terms.

### What we do instead

We discover `ffmpeg` and `ffprobe` on `PATH` at runtime via `shutil.which`. If
they are absent, the affected measurements report **unverified** — never a pass
— with a clear message naming what to install.

The consequences are all good ones: nothing is redistributed, so no FFmpeg
obligation attaches to this repository at all; the user's own system FFmpeg is
governed by whatever terms they already accepted; and `--mock` mode requires
neither binary, so CI runs with no media tooling installed.

The cost is that a live measurement run needs FFmpeg present. That is a
documented prerequisite, not a defect.

---

## 3. Runtime dependencies

**There are none.** Every module is Python standard library only — no numpy, no
requests, no test framework beyond `unittest`. This is a deliberate constraint,
not an accident of scope.

The reasoning is the same as above: a dependency you do not have cannot create
an obligation you did not plan for, cannot break your build when it is yanked,
and cannot fail an audit. For a harness whose whole job is to produce
trustworthy measurements, a dependency tree of one is worth some inconvenience
in the implementation.

External *tools* are invoked, not redistributed:

| Tool | Role | Relationship |
|---|---|---|
| `ffmpeg` / `ffprobe` | measurement | invoked from PATH; not bundled |
| agent runtime (configurable) | generation | invoked from PATH; not bundled |
| judge model endpoint | LLM scoring | called over HTTPS; not bundled |

Invoking a program is not distributing it. None of these place a licence
obligation on this repository.

---

## 4. If you add a dependency

Record it in `THIRD_PARTY_NOTICES.md` with its licence and the reason it was
necessary, and check it is not one of the two traps above: a wrapper whose
declared licence differs from the licence of the binaries it ships, or a
package containing a `--enable-nonfree` build.
