import SwiftUI
import AppKit
import LumiverbKit

/// Reaches into a SwiftUI `ScrollView`'s underlying `NSScrollView` and
/// hands it back to the caller. SwiftUI's `ScrollView` doesn't expose
/// pixel-level scroll control (no content offset, no built-in
/// page-down semantics), and `ScrollViewReader.scrollTo` has fatal
/// gotchas with `LazyVStack`'s render-window: scrolling backward to
/// a target that's been disposed silently no-ops. AppKit's
/// `NSScrollView` has had all of this since OS X 10.0.
///
/// **How it works:** drop this view into the scroll content via
/// `.background(NSScrollViewIntrospector { ... })`. On
/// `viewDidMoveToWindow`, it walks up the `superview` chain until it
/// finds an `NSScrollView` and calls the closure with it. The caller
/// stashes the reference and uses it to send AppKit scroll messages.
///
/// **Why this works on macOS 14:** SwiftUI's `ScrollView` is backed by
/// `NSScrollView` on macOS today. This is undocumented but stable —
/// the introspect-libraries community (`SwiftUIIntrospect` et al.)
/// has relied on it for years. If a future macOS reimplements
/// `ScrollView` on top of something else, this view will silently
/// fail (`onFound` never fires) and keyboard nav stops working — at
/// which point we'd write a full `NSViewRepresentable` wrapper around
/// `NSScrollView` directly. For now, the introspection trick keeps
/// the SwiftUI declarative tree intact.
struct NSScrollViewIntrospector: NSViewRepresentable {
    let onFound: (NSScrollView) -> Void

    func makeNSView(context: Context) -> Probe {
        let view = Probe()
        view.onFound = onFound
        return view
    }

    func updateNSView(_ nsView: Probe, context: Context) {
        nsView.onFound = onFound
    }

    final class Probe: NSView {
        var onFound: ((NSScrollView) -> Void)?
        private var hasReported = false

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            // Defer the walk by one runloop tick — at viewDidMoveToWindow
            // time the SwiftUI hosting tree may not yet have wired our
            // probe into its final position under the NSScrollView.
            DispatchQueue.main.async { [weak self] in
                self?.lookForScrollView()
            }
        }

        private func lookForScrollView() {
            guard !hasReported else { return }
            var current: NSView? = superview
            while let v = current {
                if let sv = v as? NSScrollView {
                    hasReported = true
                    onFound?(sv)
                    return
                }
                current = v.superview
            }
        }
    }
}

/// Apply a `ScrollCommand` to an `NSScrollView` using AppKit's
/// native scroll-by-page / scroll-by-line / scroll-to-edge semantics.
///
/// `pageUp` / `pageDown` use `NSResponder.pageUp(_:)` / `pageDown(_:)`,
/// which scroll by one viewport height with the standard one-line
/// overlap (so the user doesn't lose context across page boundaries).
///
/// `lineUp` / `lineDown` shift by `NSScrollView.verticalLineScroll`
/// (the same step the up/down arrow keys would trigger natively).
///
/// `home` / `end` jump to the absolute top or bottom of the document
/// view.
///
/// `toRow` is currently unused on macOS — keyboard nav doesn't dispatch
/// it, so the existing keyboard surface is unchanged. When iOS lands
/// search-hit jumping (M6) we may want to add a precise per-row table
/// here as well; for now, this is a no-op so the switch stays
/// exhaustive against the LumiverbKit `ScrollCommand` enum.
@MainActor
func applyScrollCommand(_ command: ScrollCommand, to scrollView: NSScrollView) {
    let clipView = scrollView.contentView
    let lineHeight = scrollView.verticalLineScroll > 0 ? scrollView.verticalLineScroll : 40

    switch command {
    case .pageUp:
        scrollView.pageUp(nil)
    case .pageDown:
        scrollView.pageDown(nil)
    case .lineUp:
        let target = NSPoint(
            x: clipView.bounds.origin.x,
            y: max(0, clipView.bounds.origin.y - lineHeight)
        )
        clipView.scroll(to: target)
        scrollView.reflectScrolledClipView(clipView)
    case .lineDown:
        guard let doc = scrollView.documentView else { return }
        let maxY = max(0, doc.bounds.height - clipView.bounds.height)
        let target = NSPoint(
            x: clipView.bounds.origin.x,
            y: min(maxY, clipView.bounds.origin.y + lineHeight)
        )
        clipView.scroll(to: target)
        scrollView.reflectScrolledClipView(clipView)
    case .home:
        clipView.scroll(to: .zero)
        scrollView.reflectScrolledClipView(clipView)
    case .end:
        guard let doc = scrollView.documentView else { return }
        let maxY = max(0, doc.bounds.height - clipView.bounds.height)
        clipView.scroll(to: NSPoint(x: 0, y: maxY))
        scrollView.reflectScrolledClipView(clipView)
    case .toRow:
        // No-op for now: macOS keyboard nav doesn't dispatch this case.
        // If/when search-hit jumping arrives on macOS, replace with a
        // per-row offset lookup against the grid layout.
        break
    }
}

/// Tiny `ObservableObject` that holds a weak reference to an
/// `NSScrollView`. Used by grid views to stash the introspected
/// scroll view so they can dispatch scroll commands to it without
/// fighting `@State`'s value semantics.
@MainActor
final class NSScrollViewBox: ObservableObject {
    weak var scrollView: NSScrollView?
}

/// macOS implementation of LumiverbKit's `ScrollViewAccessor` protocol.
/// Wraps an `NSScrollViewBox` so callers can dispatch `ScrollCommand`s
/// without needing to know about AppKit. The grid views (M2) will
/// consume this via `@Environment(\.scrollAccessor)`; until then, this
/// type is constructed by `AppState` and lives alongside the existing
/// `NSScrollViewBox`-based plumbing in the grid views, which keeps M1
/// behavior-preserving.
@MainActor
final class MacScrollAccessor: ObservableObject, ScrollViewAccessor {
    let box = NSScrollViewBox()

    nonisolated init() {}

    func apply(_ command: ScrollCommand) {
        guard let sv = box.scrollView else { return }
        applyScrollCommand(command, to: sv)
    }
}
