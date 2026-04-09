import Foundation
import LumiverbKit

/// Watches library root paths for file changes using FSEvents.
///
/// Coalesces bursts of events into a single delegate notification with a
/// **trailing-debounce-with-leading-schedule** pattern: the first event in
/// a quiet window schedules a fire `debounceInterval` seconds later;
/// subsequent events that arrive *before* the fire are ignored (not used
/// to reset the timer). This is intentional — the previous design reset
/// the timer on every event, which let a single pathological writer
/// (e.g. a video render rewriting one file every 2 seconds) starve the
/// scan queue indefinitely. With this design, the watcher fires at most
/// once per `debounceInterval` regardless of how busy the filesystem is.
///
/// Mid-scan changes are NOT lost: `ScanState` sets `pendingRescan` if the
/// watcher fires while a scan is already running, and runs another pass
/// after the current scan completes.
///
/// Half-written files (the "video being rendered" case) are filtered out
/// by `ScanPipeline.discoverFiles()` via an mtime quarantine — files
/// modified within the last `mtimeQuarantineSeconds` are skipped on the
/// current pass and picked up on a later one.
///
/// Handles unmounted volumes gracefully.
final class LibraryWatcher: @unchecked Sendable {
    private var stream: FSEventStreamRef?
    private var watchedPaths: [String] = []
    private let onChange: @Sendable () -> Void

    /// Leading-schedule debouncer — the first event of a quiet window
    /// schedules a fire; subsequent events while pending are dropped.
    /// Lives in `LumiverbKit` so its state-machine semantics can be
    /// unit-tested independently of FSEvents (`LeadingDebounceTests`).
    private let debouncer = LeadingDebounce()

    /// Debounce interval in seconds.
    private let debounceInterval: TimeInterval = 5.0

    init(onChange: @escaping @Sendable () -> Void) {
        self.onChange = onChange
    }

    deinit {
        stop()
    }

    /// Start watching the given directory paths.
    func watch(paths: [String]) {
        stop()

        // Filter to paths that actually exist (skip unmounted volumes)
        let validPaths = paths.filter { FileManager.default.fileExists(atPath: $0) }
        guard !validPaths.isEmpty else { return }

        watchedPaths = validPaths

        var context = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )

        let flags: FSEventStreamCreateFlags =
            UInt32(kFSEventStreamCreateFlagUseCFTypes) |
            UInt32(kFSEventStreamCreateFlagFileEvents) |
            UInt32(kFSEventStreamCreateFlagNoDefer)

        guard let stream = FSEventStreamCreate(
            nil,
            fsEventCallback,
            &context,
            validPaths as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            1.0, // Latency — FSEvents coalesces within this window
            flags
        ) else { return }

        self.stream = stream
        FSEventStreamSetDispatchQueue(stream, DispatchQueue.global(qos: .utility))
        FSEventStreamStart(stream)
    }

    /// Stop watching.
    func stop() {
        debouncer.cancel()

        if let stream {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
        }
        stream = nil
        watchedPaths = []
    }

    /// Called by the FSEvents callback. Leading-schedule debounce: the
    /// first event in a quiet window schedules a fire; subsequent events
    /// arriving while a fire is pending are dropped (no reset). See
    /// `LeadingDebounce` for the rationale and tests.
    fileprivate func handleEvent() {
        guard debouncer.tryArm() else { return }

        let interval = debounceInterval
        let onChange = self.onChange
        let debouncer = self.debouncer
        Task.detached {
            try? await Task.sleep(for: .seconds(interval))
            // Release before firing so any new events during onChange()
            // can already schedule the next pass. ScanState's
            // pendingRescan covers events that arrive while a scan is
            // in flight; this release covers events that arrive while
            // onChange is in the middle of dispatching.
            debouncer.release()
            onChange()
        }
    }
}

// MARK: - FSEvents callback

private func fsEventCallback(
    _ streamRef: ConstFSEventStreamRef,
    _ clientCallBackInfo: UnsafeMutableRawPointer?,
    _ numEvents: Int,
    _ eventPaths: UnsafeMutableRawPointer,
    _ eventFlags: UnsafePointer<FSEventStreamEventFlags>,
    _ eventIds: UnsafePointer<FSEventStreamEventId>
) {
    guard let info = clientCallBackInfo else { return }
    let watcher = Unmanaged<LibraryWatcher>.fromOpaque(info).takeUnretainedValue()
    watcher.handleEvent()
}
