#!/bin/sh
# dracut sources pre-pivot hooks after constructing the live overlay.
# This hook only changes the live overlay and only on the intended board.
if [ "$(cat /sys/class/dmi/id/product_name 2>/dev/null)" = Lapis ] && [ -d /sysroot/etc ]; then
    # livesys creates liveuser with an empty password. Explicitly authorize
    # sudo instead of relying on PAM accepting an empty password. This file
    # exists only in the live overlay; installed accounts keep normal sudo.
    mkdir -p /sysroot/etc/sudoers.d
    printf '%s\n' 'liveuser ALL=(ALL:ALL) NOPASSWD: ALL' > /sysroot/etc/sudoers.d/90-lapis-live
    chown 0:0 /sysroot/etc/sudoers.d/90-lapis-live
    chmod 0440 /sysroot/etc/sudoers.d/90-lapis-live
    if [ -d /lapis-kit ]; then
        mkdir -p /sysroot/opt/lapis-kit
        cp -a /lapis-kit/. /sysroot/opt/lapis-kit/
        mkdir -p /sysroot/usr/local/bin /sysroot/etc/lapis \
            /sysroot/etc/systemd/system/multi-user.target.wants
        cp /lapis-live/install-fedora /sysroot/usr/local/bin/install-fedora
        chmod 755 /sysroot/usr/local/bin/install-fedora
        cp /lapis-live/usb.json /sysroot/etc/lapis/usb.json
        cp /lapis-live/lapis-usb-backup.service /sysroot/etc/systemd/system/lapis-usb-backup.service
        ln -sf ../lapis-usb-backup.service /sysroot/etc/systemd/system/multi-user.target.wants/lapis-usb-backup.service
    fi
    mkdir -p /sysroot/usr/local/libexec /sysroot/etc/systemd/system/gdm.service.d /sysroot/etc/modprobe.d \
        /sysroot/etc/systemd/system/livesys.service.d /sysroot/etc/systemd/system-preset \
        /sysroot/etc/systemd/system/sysinit.target.wants
    cp /lapis-live/gpu-prepare.py /sysroot/usr/local/libexec/lapis-gpu-prepare
    chmod 755 /sysroot/usr/local/libexec/lapis-gpu-prepare
    cp /lapis-live/lapis-gpu-prepare.service /sysroot/etc/systemd/system/lapis-gpu-prepare.service
    cp /lapis-live/lapis-live-labels.service /sysroot/etc/systemd/system/lapis-live-labels.service
    ln -sf ../lapis-live-labels.service /sysroot/etc/systemd/system/sysinit.target.wants/lapis-live-labels.service
    printf '%s\n' 'enable lapis-live-labels.service' 'enable lapis-usb-backup.service' > /sysroot/etc/systemd/system-preset/00-lapis-live.preset
    printf '[Unit]\nRequires=lapis-live-labels.service\nAfter=lapis-live-labels.service\n' > /sysroot/etc/systemd/system/livesys.service.d/50-lapis-live.conf
    printf '[Unit]\nRequires=lapis-gpu-prepare.service\nAfter=lapis-gpu-prepare.service\n' > /sysroot/etc/systemd/system/gdm.service.d/50-lapis-gpu.conf
    printf 'blacklist xe\nblacklist snd_sof_pci_intel_ptl\noptions snd_soc_sof_sdw quirk=0x288000\noptions snd_sof_intel_hda_generic dmic_num=0\n' > /sysroot/etc/modprobe.d/lapis-live.conf
    # The early service labels these files after the live SELinux policy loads.
fi
