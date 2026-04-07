import Foundation

/// Watches library root paths for file changes using FSEvents.
///
/// Debounces changes by 5 seconds before notifying the delegate.
/// Handles unmounted volumes gracefully.
final class LibraryWatcher: @unchecked Sendable {
    private var stream: FSEventStreamRef?
    private var watchedPaths: [String] = []
    private var debounceTask: Task<Void, Never>?
    private let onChange: @Sendable () -> Void

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
        debounceTask?.cancel()
        debounceTask = nil

        if let stream {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
        }
        stream = nil
        watchedPaths = []
    }

    /// Called by the FSEvents callback — debounces before forwarding.
    fileprivate func handleEvent() {
        debounceTask?.cancel()
        debounceTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .seconds(self.debounceInterval))
            guard !Task.isCancelled else { return }
            self.onChange()
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
