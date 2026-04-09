import XCTest
@testable import LumiverbKit

final class LeadingDebounceTests: XCTestCase {

    // MARK: - Single-thread state machine

    func testInitialStateIsNotPending() {
        let d = LeadingDebounce()
        XCTAssertFalse(d.isPending)
    }

    func testFirstArmSucceedsAndMarksPending() {
        let d = LeadingDebounce()
        XCTAssertTrue(d.tryArm())
        XCTAssertTrue(d.isPending)
    }

    func testSubsequentArmsBeforeReleaseFail() {
        let d = LeadingDebounce()
        XCTAssertTrue(d.tryArm())
        // Many arms while a fire is already pending — all should fail.
        // This is the property that prevents pathological writers from
        // starving the queue: each event past the first is silently
        // dropped instead of resetting a timer.
        for _ in 0..<100 {
            XCTAssertFalse(d.tryArm())
        }
    }

    func testReleaseClearsPendingAndAllowsNextArm() {
        let d = LeadingDebounce()
        XCTAssertTrue(d.tryArm())
        d.release()
        XCTAssertFalse(d.isPending)
        XCTAssertTrue(d.tryArm())
    }

    func testCancelClearsPendingWithoutFiring() {
        let d = LeadingDebounce()
        XCTAssertTrue(d.tryArm())
        d.cancel()
        XCTAssertFalse(d.isPending)
        // After cancel the debouncer is fully reset — the next arm
        // succeeds, same as after a successful release. This is the
        // shutdown path: stop watching, drop any pending fire on the
        // floor, and don't leave the next watcher start-up wedged.
        XCTAssertTrue(d.tryArm())
    }

    func testReleaseOnUnarmedDebouncerIsHarmless() {
        // Calling release() before tryArm() should not crash or leave
        // state in a weird place — release is idempotent on an already
        // -unset flag.
        let d = LeadingDebounce()
        d.release()
        XCTAssertFalse(d.isPending)
        XCTAssertTrue(d.tryArm())
    }

    // MARK: - Concurrency

    func testConcurrentArmsExactlyOneSucceeds() {
        // Hammer tryArm from many threads simultaneously and verify the
        // lock guarantees exactly one wins. Without the lock, the
        // pending-flag check is a TOCTOU race and multiple callers can
        // observe `pending == false` and both proceed to set it true.
        let d = LeadingDebounce()
        let armCount = 1_000
        let successCount = NSCountedSet()
        let lock = NSLock()

        DispatchQueue.concurrentPerform(iterations: armCount) { _ in
            if d.tryArm() {
                lock.lock()
                successCount.add("ok")
                lock.unlock()
            }
        }

        XCTAssertEqual(successCount.count(for: "ok"), 1,
                       "Exactly one tryArm() should succeed before release()")
        XCTAssertTrue(d.isPending)
    }

    func testReleaseWhileOtherCallerIsArmingDoesNotDeadlock() {
        // Smoke test for the lock acquisition order: release() and
        // tryArm() both take the same lock, so calling them from
        // different threads should never deadlock.
        let d = LeadingDebounce()
        let iterations = 5_000

        DispatchQueue.concurrentPerform(iterations: iterations) { i in
            if i.isMultiple(of: 2) {
                _ = d.tryArm()
            } else {
                d.release()
            }
        }
        // No assertion on final state — the test is "didn't deadlock or
        // crash". Final state depends on schedule order, which is
        // intentionally non-deterministic for this test.
    }

    // MARK: - Realistic flow

    func testTypicalEventBurstFiresOnce() {
        // Simulate the LibraryWatcher flow: many events arrive, only the
        // first arms; the consumer "fires" by calling release() then
        // running its callback. After release, a new event arms again.
        let d = LeadingDebounce()
        var fireCount = 0

        func handleEvent() {
            guard d.tryArm() else { return }
            // ...consumer would Task.sleep here in reality...
            d.release()
            fireCount += 1
        }

        for _ in 0..<10 { handleEvent() }

        // Each event ran release() before the next handleEvent() so all
        // 10 fire — but in production the release() happens after a
        // sleep, so only the first event of each quiet window fires.
        XCTAssertEqual(fireCount, 10)
    }

    func testEventBurstWithoutReleaseFiresOnlyOnce() {
        // The realistic case: events arrive faster than the consumer
        // can release. Only the first event arms; subsequent events are
        // dropped until the eventual release.
        let d = LeadingDebounce()
        var armSucceeded = 0

        for _ in 0..<10 {
            if d.tryArm() { armSucceeded += 1 }
        }

        XCTAssertEqual(armSucceeded, 1)
        XCTAssertTrue(d.isPending)

        // Now the consumer releases (simulating the timer firing) and
        // the next event arms again.
        d.release()
        XCTAssertTrue(d.tryArm())
    }
}
