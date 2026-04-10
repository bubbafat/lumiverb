import Foundation

/// A scroll command sent from `BrowseState` (or any other state holder) to
/// the active grid view's scroll view. The protocol implementation translates
/// these into platform-native scroll operations.
///
/// Why this exists at all: SwiftUI's `ScrollView` doesn't expose pixel-level
/// scroll control, and `ScrollViewReader.scrollTo` silently no-ops when the
/// target is a disposed `LazyVStack` cell. Both macOS and iOS reach into
/// their underlying scroll view (`NSScrollView` / `UIScrollView`) instead.
/// This protocol is the abstraction over that platform-specific reach.
///
/// Case names match the existing macOS surface (`home`/`end` rather than
/// `top`/`bottom`) so the migration to LumiverbKit is mechanical.
public enum ScrollCommand: Sendable, Equatable {
    case pageUp
    case pageDown
    case lineUp
    case lineDown
    case home
    case end
    /// Best-effort jump to a target row index. Implementations are free to
    /// approximate (e.g. iOS uses `row * averageRowHeight`).
    case toRow(Int)
}

/// Wraps a `ScrollCommand` with a unique id so re-issuing the same command
/// still trips SwiftUI `.onChange` (which dedupes by `Equatable`).
public struct ScrollCommandToken: Sendable, Equatable {
    public let id: UUID
    public let command: ScrollCommand

    public init(command: ScrollCommand) {
        self.id = UUID()
        self.command = command
    }
}

/// Platform-neutral handle that browse views use to dispatch scroll commands
/// without importing AppKit or UIKit. macOS provides a `MacScrollAccessor`
/// (in `Sources/macOS/AppKitScrollIntrospector.swift`) that wraps an
/// `NSScrollView`; iOS provides an `IOSScrollAccessor` (in
/// `Sources/iOS/UIKitScrollAccessor.swift`) that wraps a `UIScrollView`.
///
/// Views read the accessor from `@Environment(\.scrollAccessor)` and call
/// `apply(_:)` from a `.onChange(of: pendingScrollCommand)` modifier.
@MainActor
public protocol ScrollViewAccessor: AnyObject {
    func apply(_ command: ScrollCommand)
}
