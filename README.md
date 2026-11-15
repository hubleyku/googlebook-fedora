# Fedora on ASUS Googlebook 14 (Lapis)

An experimental community installer for **Fedora 45 on ASUS Googlebook 14
(Google Lapis)**, using the factory Depthcharge firmware. This project builds
on the Omarchy and CachyOS projects credited [below](#acknowledgments).
It is unaffiliated with ASUS, Google, Fedora, Omarchy, or CachyOS.

**Installation erases the internal `userdata` partition. The installed system
uses unencrypted Btrfs.** Back up personal files separately and prepare factory
recovery media before changing the bootloader or installing. The USB's automatic
boot backup does not preserve your files or a complete factory OS.

Read the [hardware requirements](#supported-hardware) and
[build guide](docs/build-usb.md), build and boot the installer USB, connect to
the network, and run:

```sh
install-fedora
```

The installer asks for **timezone, locale, keymap, username, password, and desktop
environment** before starting any work. Invalid entries are explained and asked
again. It shows the completed choices and a brief red erase warning; type
**YES** once to authorize installation. No later installation step asks for
another confirmation.

The USB writer creates a dedicated **2 GiB writable backup partition** after
copying the compact boot image. Empty backup space is not copied or read back.
The partition mounts automatically in the live environment. The installer backs up and verifies the
internal drive's GPT, active boot images, and boot-control partition there before formatting the
Linux partition. It copies live Wi-Fi profiles, creates the login account,
installs the selected desktop inside the chroot, configures the hardware
workarounds, and installs automatic kernel boot-image updates. A completed
installation also saves a minimal Linux bootstrap bundle for reconstructing
the layout and boot chain on a blank supported-size drive without Googlebook OS.

Desktop choices are None, GNOME, KDE Plasma, Xfce, Cinnamon, MATE, LXQt, Budgie,
and COSMIC. None creates a standard agetty console system without kmscon,
ready for installing your own desktop. The other choices use Fedora's
official desktop environment package groups, with the appropriate display
manager and graphical boot target.
All choices use `graphical.target` as the default boot target. With None, it
starts console logins; enabling a display manager such as greetd later also
works without changing the boot target.

## Documentation

- [Build and write the installer USB](docs/build-usb.md)
- [Install Fedora and handle interrupted installs](docs/install.md)
- [Backups and boot-image recovery](docs/recovery.md)
- [Project structure, implementation, and tests](docs/development.md)
- [Contributing and reporting problems](CONTRIBUTING.md)
- [Publishing source and release artifacts](docs/publishing.md)

## Supported hardware

The scripts support the **Google Lapis / ASUS Googlebook 14 with the tested
512 GB NVMe partition layout**, 512-byte logical sectors, and the existing
Depthcharge firmware configured to permit the project's custom boot images.
They support either active boot slot, A or B. They do not replace the machine's
firmware or repartition the internal disk into a conventional UEFI layout.

Use a Fedora Workstation x86-64 live ISO; its release, compose, and live kernel
are not pinned. The builder checks its Workstation volume label, supported
boot-file layout, and x86-64 kernel, and records its SHA-256 in the manifest.
The installed system still targets Fedora 45. The installer requires network
access and AC power. Existing user data in `userdata` is replaced by unencrypted Btrfs
subvolumes `@` and `@home`; other partition positions are preserved.

The automated flow has file-based and isolated provisioning tests. A full
installation, USB automount, and first boot still require validation on the
physical Googlebook. See the test details in the development guide.

## Acknowledgments

Two repositories formed the foundation of this work:

- [Jonathan Kleiman's asus-googlebook-omarchy](https://github.com/jmklex/asus-googlebook-omarchy)
  established the native Linux boot path, Android/AVB boot-image tooling,
  hardware workarounds, staged installation, and recovery approach for Lapis.
- [wendelltron's setup-cachyos-on-asus-googlebook-14-2026-unofficial](https://github.com/wendelltron/setup-cachyos-on-asus-googlebook-14-2026-unofficial)
  adapted that foundation to CachyOS and provided the starting point for this
  Fedora adaptation.

This project adds the Fedora live-media integration, target provisioning,
interactive installer, USB backup workflow, and kernel-update integration.
Upstream validation results describe those projects' builds; they do not
establish validation of this Fedora installer.

## License

Project code and documentation are distributed under the [MIT License](LICENSE),
with the upstream copyright notice retained. Android and ChromiumOS components
retain their own licenses; see [NOTICE.md](NOTICE.md). Public upstream sources
are fetched at pinned revisions during the build and are not vendored in this
repository. Generated installer and installed-system kits include the verified
runtime tools needed for subsequent boot-image updates.
