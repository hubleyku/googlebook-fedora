# Attribution and third-party notices

## Foundation projects

This Fedora adaptation builds on two MIT-licensed projects:

- [asus-googlebook-omarchy](https://github.com/jmklex/asus-googlebook-omarchy)
  by Jonathan Kleiman: the original Lapis native Linux boot tooling, hardware
  workarounds, staged installer, and recovery foundation.
- [setup-cachyos-on-asus-googlebook-14-2026-unofficial](https://github.com/wendelltron/setup-cachyos-on-asus-googlebook-14-2026-unofficial)
  by wendelltron: the CachyOS adaptation of that project, from which this Fedora
  work was adapted.

The upstream `Copyright (c) 2026 Jonathan Kleiman` notice and MIT license text
are retained in [LICENSE](LICENSE). Fedora-specific changes include live-media
integration, provisioning, installer preferences, USB backup handling, and
kernel-update integration. Existing copyright and license headers remain in
derived files.

## Boot tools

Public dependencies are fetched at the revisions in `tools/fetch_sources.py`
into the ignored `build/sources/` directory. They are not committed here.
Generated installer and installed-system kits carry only the runtime files
listed and SHA-256 checked by `assets/vendor-runtime.json`, plus the compiled
loader harness and license notices. Those bundled tools support offline
boot-image rebuilding; installation itself still needs Fedora package access.

| Component | Source and license |
| --- | --- |
| Android mkbootimg | [Android Open Source Project](https://android.googlesource.com/platform/system/tools/mkbootimg/); Apache 2.0. The packaged Python files retain their copyright headers; the full license is in [tools/LICENSE.apache-2.0](tools/LICENSE.apache-2.0). |
| Android Verified Boot | [Android Open Source Project](https://android.googlesource.com/platform/external/avb/); per-file licenses. `avbtool.py` retains its MIT header; generated kits include `vendor/avb/LICENSE`. |
| ChromiumOS vboot_reference and loader harness | [ChromiumOS](https://chromium.googlesource.com/chromiumos/platform/vboot_reference/); BSD-style license in [tools/LICENSE.chromium](tools/LICENSE.chromium). `tools/loader_harness.c` is a modified ChromiumOS test utility. The harness is linked with vboot and AVB code. |

Fedora images, packages, and firmware retain their respective upstream licenses.
No Fedora image, private signing key, network profile, or device backup is
included in the source project. This notice does not imply endorsement by any
upstream project or vendor.
