import Foundation

/// Thread-safe leading-schedule debouncer.
///
/// Captures the "schedule once, ignore subsequent events until the
/// scheduled fire runs" semantic. Designed to replace the more common
/// reset-on-every-event debounce in cases where a perpetually-active
/// event source could otherwise starve the consumer.
///
/// The classic reset-on-every-event debounce works like this:
///
/// ```text
/// event → cancel pending fire → schedule fire at T+5
/// event → cancel pending fire → schedule fire at T+5  (loop forever)
/// ```
///
/// If events arrive faster than the debounce interval, the fire is
/// indefinitely deferred. For a `LibraryWatcher` watching a directory
/// where one file is being rewritten every two seconds, the rest of the
/// directory's changes would never be picked up.
///
/// Leading-schedule fixes this:
///
/// ```text
/// event → tryArm()=true  → schedule fire at T+5
/// event → tryArm()=false (still pending — caller drops it)
/// fire   → release()     → ready for next event
/// event → tryArm()=true  → schedule fire at T+10
/// ```
///
/// The cost is one extra debounce-window of latency in the worst case
/// (a fresh event arriving 1 ms after a fire has to wait the full
/// interval), but the benefit is that no event source can starve the
/// fire indefinitely.
///
/// `LeadingDebounce` itself is timer-agnostic — it owns the *bookkeeping*
/// (the pending flag and its lock), not the schedule. Callers wire it to
/// `Task.sleep`, `DispatchQueue.asyncAfter`, or whatever fits.
public final class LeadingDebounce: @unchecked Sendable {
    private let lock = NSLock()
    private var pending = false

    public init() {}

    /// Try to claim the slot. Returns `true` if this caller is the first
    /// event in the current quiet window and should schedule the delayed
    /// fire. Returns `false` if a fire is already pending — caller should
    /// drop the event.
    public func tryArm() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if pending { return false }
        pending = true
        return true
    }

    /// Mark the debouncer ready to accept the next event. Call this just
    /// before invoking the delayed callback so that any new events that
    /// arrive *during* the callback can already schedule the next fire,
    /// instead of being dropped.
    public func release() {
        lock.lock()
        pending = false
        lock.unlock()
    }

    /// Cancel a pending fire without invoking it. Useful on shutdown.
    /// After cancel, the next `tryArm()` will succeed.
    public func cancel() {
        lock.lock()
        pending = false
        lock.unlock()
    }

    /// Whether a fire is currently pending. Exposed for tests and
    /// debug-only assertions; production code should use `tryArm` /
    /// `release` and let those return values drive control flow.
    public var isPending: Bool {
        lock.lock()
        defer { lock.unlock() }
        return pending
    }
}
