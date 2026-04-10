import SwiftUI

// MARK: - CacheBundle environment

private struct CacheBundleKey: EnvironmentKey {
    /// Default value for previews and tests. Two **separate** in-memory
    /// caches so the preview proxy budget and the preview thumbnail
    /// budget have independent storage. The defaults have unrealistic
    /// budgets and no disk layer — tests that exercise real cache
    /// behavior MUST inject a real `CacheBundle` and not rely on this.
    static let defaultValue = CacheBundle(
        proxies: MemoryImageCache(name: "preview.proxies"),
        thumbnails: MemoryImageCache(name: "preview.thumbnails")
    )
}

public extension EnvironmentValues {
    /// The pair of proxy/thumbnail caches available to browse views.
    /// Each platform installs the right concrete implementations at app
    /// startup; views read this and pass it to `AuthenticatedImageView`
    /// (or any other code that needs disk/memory cache access).
    var cacheBundle: CacheBundle {
        get { self[CacheBundleKey.self] }
        set { self[CacheBundleKey.self] = newValue }
    }
}

// MARK: - ScrollViewAccessor environment

private struct ScrollAccessorKey: EnvironmentKey {
    /// Default is nil — views must tolerate the absence of an accessor
    /// (preview / test contexts where there's no real scroll view to
    /// reach into). The grid views' `.onChange(of: pendingScrollCommand)`
    /// handler should `guard let` the accessor before calling `apply`.
    ///
    /// `nonisolated(unsafe)` is correct here because the *value* is
    /// literally `nil` and has no mutable state to share. The protocol
    /// is `@MainActor`, so any concrete accessor instances installed via
    /// `.environment(\.scrollAccessor, ...)` are still constructed and
    /// invoked from main-actor contexts (SwiftUI view bodies). The
    /// `unsafe` opt-out only relaxes the static-storage check, not the
    /// actor isolation of the protocol methods.
    nonisolated(unsafe) static let defaultValue: (any ScrollViewAccessor)? = nil
}

public extension EnvironmentValues {
    /// The scroll accessor that browse views use to dispatch
    /// `ScrollCommand`s to their underlying scroll view. Optional because
    /// previews and tests run without one.
    var scrollAccessor: (any ScrollViewAccessor)? {
        get { self[ScrollAccessorKey.self] }
        set { self[ScrollAccessorKey.self] = newValue }
    }
}
