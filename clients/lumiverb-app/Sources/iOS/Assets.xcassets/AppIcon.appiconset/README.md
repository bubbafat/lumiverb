# App Icon

Drop a single 1024×1024 PNG named `AppIcon.png` into this directory.

iOS 17+ uses a single image (Xcode generates all required sizes at
build time). The companion `Contents.json` already references
`AppIcon.png` — Xcode will fail the build with a clear error if the
file is missing.

To replace the icon: drag a new master PNG into Xcode's asset
catalog editor for `AppIcon`, or just overwrite `AppIcon.png` here
and rebuild.
