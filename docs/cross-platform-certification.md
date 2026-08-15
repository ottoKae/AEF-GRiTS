# Cross-platform certification

AEF-GRiTS targets Windows, Linux and macOS with the same scientific and
storage contracts. GitHub Actions runs Python 3.11 and 3.12 on all three
operating systems. Each job installs the downloader extras, runs the complete
unit/synthetic suite, verifies installed commands, and resolves a low-memory
MGRS plan without contacting Earth Engine.

Authenticated Earth Engine downloads remain a manual or secret-gated
acceptance step. A platform is considered fully certified only when both its CI
job and one authenticated two-point plus 256 x 256 reference-grid acceptance
run have passed.

## Native filesystem guidance

- Linux: keep state and staging on ext4/XFS. NTFS3 is final-delivery-only.
- macOS: keep state and staging on APFS. External exFAT/NTFS volumes should be
  treated as final-delivery targets, not active Zarr stores.
- Windows: keep active state/staging on a local NTFS volume managed by Windows;
  the Linux NTFS3 restriction does not apply to the Windows kernel driver.

The CI workflow itself does not use credentials and therefore cannot consume
Earth Engine quota.
