# Contributing

Include the commit, exact Googlebook model, storage capacity, firmware version,
chosen Workstation ISO, desktop choice, failing stage, and visible result when
reporting a problem. State whether the result came from physical hardware or a
simulation. Review logs before posting them: redact passwords, password hashes,
Wi-Fi names and secrets, serial numbers, addresses, and disk identifiers. Do not
attach device backups, installed-system images, or private signing keys.

Keep changes scoped and describe how they were checked. Run the tests in
[docs/development.md](docs/development.md). Changes to boot images, storage
layout, provisioning, or hardware startup also need appropriate physical-device
validation; describe any validation that remains outstanding. Preserve the
hardware, backup, identity, and read-back guards when adding support.

Retain upstream attribution and license notices. Use public pinned dependencies
and synthetic test fixtures rather than private logs or device snapshots. See
[docs/publishing.md](docs/publishing.md) before preparing a release.
