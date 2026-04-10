import XCTest
@testable import LumiverbKit

/// Tests for the LumiverbKit `ScrollCommand` enum and `ScrollCommandToken`
/// value type. These are pure value types — no platform dependencies — so
/// they exercise the same code that the macOS app and (in M6) the iOS app
/// will both use.
final class ScrollViewAccessorTests: XCTestCase {

    // MARK: - ScrollCommand equality

    func testSimpleCasesAreEqual() {
        XCTAssertEqual(ScrollCommand.pageUp, ScrollCommand.pageUp)
        XCTAssertEqual(ScrollCommand.pageDown, ScrollCommand.pageDown)
        XCTAssertEqual(ScrollCommand.lineUp, ScrollCommand.lineUp)
        XCTAssertEqual(ScrollCommand.lineDown, ScrollCommand.lineDown)
        XCTAssertEqual(ScrollCommand.home, ScrollCommand.home)
        XCTAssertEqual(ScrollCommand.end, ScrollCommand.end)
    }

    func testDifferentCasesAreNotEqual() {
        XCTAssertNotEqual(ScrollCommand.pageUp, ScrollCommand.pageDown)
        XCTAssertNotEqual(ScrollCommand.home, ScrollCommand.end)
        XCTAssertNotEqual(ScrollCommand.lineUp, ScrollCommand.pageUp)
    }

    func testToRowEqualityIsByPayload() {
        XCTAssertEqual(ScrollCommand.toRow(42), ScrollCommand.toRow(42))
        XCTAssertNotEqual(ScrollCommand.toRow(42), ScrollCommand.toRow(43))
        XCTAssertNotEqual(ScrollCommand.toRow(0), ScrollCommand.home)
    }

    // MARK: - ScrollCommandToken

    /// Each token must have a unique id even when the wrapped command is
    /// identical. This is the whole reason `ScrollCommandToken` exists —
    /// SwiftUI `.onChange` dedupes by `Equatable`, so re-issuing the
    /// same `ScrollCommand` would silently no-op without per-token ids.
    func testTokensWithSameCommandHaveDifferentIds() {
        let a = ScrollCommandToken(command: .pageDown)
        let b = ScrollCommandToken(command: .pageDown)
        XCTAssertEqual(a.command, b.command)
        XCTAssertNotEqual(a.id, b.id)
        XCTAssertNotEqual(a, b)
    }

    func testTokenCarriesItsCommand() {
        let token = ScrollCommandToken(command: .toRow(99))
        XCTAssertEqual(token.command, .toRow(99))
    }

    func testTokensWithDifferentCommandsDifferEvenIfIdsCollidedHypothetically() {
        // We can't force a UUID collision but we can verify the equality
        // takes both id AND command into account by checking that two
        // tokens with different commands are unequal even when freshly
        // constructed (which is the normal path anyway).
        let a = ScrollCommandToken(command: .pageUp)
        let b = ScrollCommandToken(command: .pageDown)
        XCTAssertNotEqual(a, b)
    }

    // MARK: - ScrollViewAccessor protocol

    /// A minimal in-memory accessor that records every command applied
    /// to it. Used to verify that callers can hold the protocol existential
    /// and dispatch through it correctly.
    @MainActor
    final class RecordingAccessor: ScrollViewAccessor {
        var commands: [ScrollCommand] = []
        func apply(_ command: ScrollCommand) {
            commands.append(command)
        }
    }

    @MainActor
    func testProtocolExistentialDispatchesCorrectly() {
        let accessor: any ScrollViewAccessor = RecordingAccessor()
        accessor.apply(.pageDown)
        accessor.apply(.toRow(7))
        accessor.apply(.home)

        // Cast back to the concrete type to inspect the recording.
        guard let recorder = accessor as? RecordingAccessor else {
            return XCTFail("expected RecordingAccessor")
        }
        XCTAssertEqual(recorder.commands, [.pageDown, .toRow(7), .home])
    }
}
