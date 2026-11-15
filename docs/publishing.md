# Publishing

## Source repository

Publish the maintained source, assets, tests, guides, `LICENSE`, and `NOTICE.md`.
The root `.gitignore` excludes downloaded dependencies, build output, Python
caches, signing keys, network profiles, and local backup directories. Keep
private files outside the source tree; ignore rules are not a secret scanner
and do not remove files already tracked by Git.

Before publishing, review `git status --short`, `git ls-files`, and reachable
Git history for credentials, personal paths, network profiles, device backups,
and generated binaries. Git commit author names and email addresses are also
part of public history. Removing a file from the current tree does not remove
it from earlier commits. Rotate an exposed credential before addressing history.

The initial publication review on 15 November 2026 checked the working source
and all three then-reachable commits for common credential patterns and personal
paths. It found no matching credentials or personal paths. Three tracked Python
bytecode files and the bundled Android source trees were removed from the
current tree; those files remain in earlier commits. The Android sources are
public upstream code. No history rewrite was performed.

The loader harness was compiled from the pinned sources during the same review.
All 69 automated tests passed with no skips, including real signed boot-image
validation through the ChromiumOS loader for both A/B slots, file-based commit,
interrupted-write rollback, and recovery. These checks used temporary disk
images; they do not establish a physical-device boot or installation result.

## Generated artifacts

Build from a clean checkout using [the build guide](build-usb.md). The build
fetches pinned Android and ChromiumOS sources into `build/sources/`. Runtime
files are checked against `assets/vendor-runtime.json` before being packaged
under `vendor/` inside generated kits. Do not publish the whole build directory:
boot-image staging directories contain freshly generated private signing keys.

For an installer-image release, distribute the completed `.img`, its
`.manifest.json`, and its `.sha256` together. The USB writer uses the manifest
to create backup storage. Retain the included license notices. Review any
additional obligations for Fedora packages and firmware before distributing
images containing them.

Publish freshly built installer images rather than a dump of a used USB. A used
USB contains device-specific backups and installation records. Installed-system
images may also contain account hashes, Wi-Fi credentials, and private files.
Keep recovery snapshots and personal backups private.

Release notes should identify the source commit, target Fedora release and
kernel, supplied live ISO checksum, hardware and desktop actually tested, and
known limitations. Distinguish automated checks from physical installation,
first boot, kernel-update, and recovery tests. The current documentation does
not claim a physical-device validation record for the exact published tree.
