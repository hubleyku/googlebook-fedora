# Install Fedora

Installation replaces `userdata` with **unencrypted Btrfs**. Save personal files
elsewhere and prepare separate factory recovery media first. The automatic USB
backup covers boot metadata and images, not personal files or a complete factory
OS. Preserving the inactive slot does not make it a working factory-OS fallback
after `userdata` has been formatted. Keep the bootloader unlocked for these
custom boot images. See [boot prerequisites](build-usb.md#boot-prerequisites).

1. Boot the generated Lapis Fedora live USB and connect AC power.
2. Connect the live environment to Wi-Fi or Ethernet.
3. Open a terminal as `liveuser` and run `install-fedora`.

The launcher supplies the live environment's passwordless sudo authorization;
there is no separate root password question. It is installed in
`/usr/local/bin`, which is in the live user's normal PATH.

## Choices before installation

The installer collects everything before mounting storage as a fallback,
querying package sources, creating backup directories, or changing the target:

- **Timezone:** a zoneinfo name such as `America/Halifax` or `UTC`.
- **Locale:** a UTF-8 locale, defaulting to `en_US.UTF-8`. For example,
  `fr_CA` selects `fr_CA.UTF-8`; the installer adds its Fedora language data.
- **Keymap:** an installed console keymap such as `us`.
- **Username:** a valid lowercase non-system account name.
- **Password:** entered twice, without echoing it.
- **Desktop:** None, GNOME, KDE Plasma, Xfce, Cinnamon, MATE, LXQt, Budgie, or
  COSMIC. None is the default and boots to a standard agetty console, without
  kmscon, ready for you to install your own desktop environment.

Enter `?` at the timezone or keymap question to see available values, or at the
locale question for suggestions from the live environment. Other UTF-8 locales
can be entered directly; Fedora package metadata must provide the selected
locale before erasure is offered. Invalid
values prompt again at the same question; a password mismatch repeats only the
password step. Ctrl-C or end-of-input cancels cleanly. The hostname defaults to
`googlebook`.

The installer detects the supported internal NVMe disk and preferred successful
boot slot, checks the installer USB's identity and free backup space, snapshots
the live Wi-Fi profiles in memory, and checks Fedora package metadata. It then
shows the choices and target. A brief red warning explains that replacing user
data cannot be undone. **Only uppercase `YES` starts the installation.**

## Automatic work after YES

1. Record the disk plan and create a new timestamped backup on the installer USB.
2. Read back and verify the backup before formatting anything internally.
3. Format `userdata` as Btrfs and mount `@` directly on `/mnt`, with `@home` on
   `/mnt/home`. All provisioning checks these exact subvolumes.
4. Install the common base packages and the chosen Fedora desktop environment
   package group. Desktop installation runs through `dnf5` **inside the chroot**.
5. Initialize and check Fedora's local PAM profile with `authselect`. Create
   the wheel user and set its salted yescrypt password using the live system's
   `chpasswd --prefix /mnt --encrypted`, then verify the stored hash and lock
   root password login. The offline password update does not use the target's
   PAM password-change stack. The login password is not written to USB state
   or command-line arguments. `/dev`, `/sys`, and `/proc` stay mounted in the
   target through account creation.
6. Transfer Wi-Fi profiles and locally referenced certificates with root-only
   file permissions. Liveuser-only profile restrictions are removed, and
   available agent-held WPA/enterprise secrets are saved for the installed
   system. The active connection must be transferable before erasure is offered.
7. Configure time, locale, keyboard, Btrfs mounts, graphics/audio workarounds, networking,
   clock synchronization, firewall, and automatic kernel boot-image updates.
8. Set `graphical.target` as the default for all choices. Enable the desktop's
   display manager, or standard getty for None. Console-only installs exclude kmscon
   packages and override Fedora's kmscon service presets. Build, loader-check,
   install, and read back the selected
   slot's boot images. Archive the installation state on the USB and unmount
   the target.

There are no further account, package-manager, or boot-write confirmation
questions. When completion is reported, shut down, remove the USB, and boot
internally.

## Backup storage

`lapis-usb-backup.service` mounts the dedicated partition at `/run/lapis-usb`
during live boot. It identifies the disk and both installer partitions using
the UUIDs embedded when the image was built, and checks that the ISO partition
supplied the running live environment. It does not select an arbitrary drive
with a matching label. `install-fedora` retries the mount automatically if the
boot service has not completed.

Backups are stored at:

```text
/run/lapis-usb/backups/<UTC timestamp>-<disk GUID prefix>/
```

The same physical USB is intentionally used for both live boot and backup. No
second removable drive is required. The initial backup holds the active slot's
four boot images, `misc`, both GPT tables, and the disk plan, about 112 MiB
before metadata. Successful installation saves a separate `linux-bootstrap/`
snapshot with the new Linux boot images and post-installation GPT. Installation
records are retained without duplicating these images. No Fedora filesystem,
home directory, large Googlebook OS partition, or inactive boot slot is backed
up. See [recovery](recovery.md) for preparing a blank replacement drive.

## Interrupted installations

The installer stops on an error instead of continuing with incomplete state.
If a backup directory exists, it saves `failure.json` and an `installer-state`
snapshot there. The current session's working state is `/run/lapis-install`.
Keep the USB and use the [recovery guide](recovery.md) to inspect boot-write
failures. The installer does not automatically reboot or resume an interrupted
attempt.

A new attempt in the same live session is refused while that state directory
exists. A fresh live boot starts a new session and keeps previous USB backups;
another install requires the same upfront choices and a new `YES`, and erases
the target's user data again. If the boot partition table was damaged, repair
it before starting another install.

## Inspection options

```sh
install-fedora --dry-run
```

This collects and validates preferences and inspects the supported target. It
does not mount backup storage, create backups, or install anything. The boot
service may already have mounted the USB before the command was launched.

If more than one supported internal disk is present, use
`install-fedora --device /dev/nvmeNnN` to select the intended whole disk.

## Add greetd after a console-only install

After installing and configuring greetd and your desktop or compositor, enable
its display-manager alias and ensure the graphical boot target is selected:

```sh
sudo systemctl enable greetd.service
sudo systemctl set-default graphical.target
```

Then reboot when ready. The target command also corrects older console-only
installations that defaulted to `multi-user.target`. That target does not pull
in `display-manager.service`, so greetd could be enabled without starting at
boot. `graphical.target` includes the console services and starts the enabled
display manager. New console-only installs already select it and still use
agetty until a display manager is installed and enabled.
