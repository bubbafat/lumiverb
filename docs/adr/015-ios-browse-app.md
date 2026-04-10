# ADR-015: iOS Browse App + Ratings & Collections

## Status

Proposed

## Progress

> **Note on terminology.** This ADR uses **Milestones** (M1–M9) for its own
> internal phasing to avoid clashing with ADR-014's Phase numbering (where
> "Phase 5" refers to the iOS browse app overall — which this entire ADR
> implements).

| Milestone | Description | Status |
|-----------|-------------|--------|
| M1 | Cache + ScrollView abstractions, defensive fixes | Complete |
| M2 | Move browse UI into LumiverbKit | Complete |
| M3 | Ratings editor (stars, favorite, color) — macOS + LumiverbKit | Complete |
| M4 | Collections CRUD (private + shared) — macOS + LumiverbKit | Complete |
| M5 | Collection sharing UI (visibility toggle + share link) | Not started |
| M5.5 | Date-grouped grid with multi-select + batch operations | Not started |
| M6 | iOS app shell (tabs, library picker, browse) | Not started |
| M7 | iOS touch adaptations for cluster/face management | Not started |
| M8 | iOS cellular + Low Data Mode policy | Not started |
| M9 | Tests, docs, launch checklist | Not started |
| M10 | Trash view: visibility + selective undelete | Not started |

## Overview

ADR-014 delivered a native macOS app with browse, search, enrichment, and face
tagging (Phases 0–4 plus the macOS half of Phase 6). Phase 5 — the iOS browse
app — is unstarted. This ADR is the implementation plan for Phase 5 plus two
new features (ratings, collections) that the user wants on iOS from day one and
therefore need to exist on both platforms.

The iOS app is **browse-only**: search, filter, face cluster management,
ratings, collections. No enrichment, no scan, no source media access — only
thumbnails and proxies. It shares the browse UI with macOS via `LumiverbKit`,
so the prep work is almost entirely "move macOS views into the shared package"
plus new feature work that lands in the shared package from day one.

This ADR is written to be implementable by a less-capable model (Sonnet/Haiku).
Every design decision is pinned down; the execution milestones contain file lists,
code sketches, and explicit acceptance criteria. If something is ambiguous, the
implementation agent should stop and ask rather than guess.

## Motivation

- **Phase 5 has been pending for months.** The macOS app is feature-complete
  enough that iOS porting is the obvious next step.
- **Ratings and collections are missing on every client.** The server has full
  APIs (`ratings.py`, `collections.py`, `public_collections.py`) but no Swift
  client surfaces them. Ratings can be *filtered* on but not *set*.
- **Doing iOS first would fork UI work.** Building iOS-only features forces us
  to port them back to macOS later (friction, divergence). Building them in
  LumiverbKit from day one gives both platforms parity at the cost of a small
  amount of extra abstraction work per feature.

## Scope

### iOS app includes
- Browse (filter, sort, paginate by library)
- Search (text + similarity)
- Filter UI (the same filter schema as macOS: media type, camera, EXIF, dates,
  ratings, faces, person)
- Face cluster management: assign cluster → person, rename person, dismiss
  false positives, add/remove faces from clusters
- Ratings: set stars (0-5), toggle favorite, set/clear color label
- Collections: create, rename, delete, add/remove assets, reorder
- Collection sharing: promote private → shared/public, copy share link,
  revoke

### iOS app explicitly excludes
- Any scan, enrichment, or ML inference
- Menu bar, background processing, file watching
- Source-media download or export
- Library creation or configuration
- Settings for enrichment (Whisper, Vision API)

### Out of scope for this ADR
- Windows client
- iOS widgets, lock screen, Siri shortcuts, App Intents
- iOS background refresh of collections
- Offline mode beyond the thumbnail disk cache
- iPad-specific layout optimizations (use adaptive layouts from SwiftUI but
  don't build a split-view sidebar for iPad in this pass)

## Design

### Cache architecture

**Problem.** The current `ProxyCacheOnDisk` and `ThumbnailCacheOnDisk` live in
LumiverbKit but hardcode `~/.cache/lumiverb/` and are unbounded. iOS cannot
write to `~/.cache/`, and an unbounded disk cache is hostile on a phone. The
macOS use case (share proxy cache with Python CLI via SHA sidecars, browse
exhaustively, disk is cheap) is fundamentally different from the iOS use case
(sandboxed container, cellular network, ephemeral browse sessions).

**Decision.** Define cache *protocols* in LumiverbKit and provide separate
macOS and iOS implementations. `AuthenticatedImageView` consumes the protocols,
not concrete types. Each platform wires the right impls at app startup.

**Protocols** (new file `LumiverbKit/Sources/LumiverbKit/API/CacheProtocols.swift`):

```swift
import Foundation

public protocol ProxyCache: Sendable {
    func get(assetId: String) -> Data?
    func put(assetId: String, data: Data)
    func has(assetId: String) -> Bool
    func remove(assetId: String)
}

public protocol ThumbnailCache: Sendable {
    func get(assetId: String) -> Data?
    func put(assetId: String, data: Data)
    func has(assetId: String) -> Bool
    func remove(assetId: String)
    func removeAll()
}
```

**macOS implementations** — the existing `ProxyCacheOnDisk` and
`ThumbnailCacheOnDisk` are renamed to `MacProxyDiskCache` and
`MacThumbnailDiskCache`, made to conform to the protocols, and wrapped in
`#if os(macOS)`. No behavior change. The Scan-specific methods
(`putScan`, `getSHA`, `isValid`) stay as extensions on the concrete macOS
class — they are **not** part of the protocol. Enrichment code that uses
them (`Sources/macOS/Scan/*`) keeps the concrete type.

**iOS proxy cache** — a `MemoryImageCache` instance (proxy slot):
- In-memory only, backed by an internal `NSCache<NSString, NSData>`.
- `costLimit = 150 * 1024 * 1024` (150 MB).
- `countLimit = 400` (generous; a typical lightbox session touches 20-50
  proxies, 400 gives room for back/forward sweeps).
- `has()` returns whether the key is currently resident.
- `removeAll()` flushes the underlying NSCache; also fires automatically
  on memory pressure via `NSCache` defaults.
- No disk fallback. Lightbox re-fetches on a cold start. Acceptable because
  proxies are cheap (~200 KB) and the user's expectation for "reopen the
  app" is that it re-fetches.
- See "Resolution of the dual-protocol default" below for why this is the
  same `MemoryImageCache` type used for the thumbnail-cache default — the
  iOS runtime nonetheless uses **two distinct instances** so the proxy
  budget and the thumbnail budget never collide.

**iOS thumbnail cache** — `IOSThumbnailDiskCache`:
- Disk-backed at `FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first!.appendingPathComponent("lumiverb/thumbnails")`.
- Capped at **200 MB** via an **approximate-LRU eviction** pass run in
  `put()`. The eviction key is `contentModificationDate`, with reads
  bumping mtime via `FileManager.setAttributes([.modificationDate: Date()])`
  on cache hits. This is "approximate" because:
  - True LRU would track per-key access timestamps in a sidecar/index;
    we deliberately don't, to keep the impl one file with no schema.
  - Touching mtime on read is one extra metadata write per hit, which
    is cheap on APFS but not free. Acceptable tradeoff for a 200 MB
    cache that holds tens of thousands of small files.
- After writing, check total cache size; if over 200 MB, delete the
  oldest files by mtime until under 180 MB.
- The 20 MB hysteresis prevents thrashing on the boundary.
- No SHA sidecars. No atomic-write ceremony (iOS apps are single-process
  accessing their own container). Use `Data.write(to:, options: .atomic)`.
- Key naming: same as macOS — raw `asset_id` as filename, no extension.
- If churn analysis later shows that mtime-touching-on-read is too
  expensive or that the eviction keeps useful files around too short,
  revisit with a real LRU index. Do not preempt that work in M1.

**Wiring.** Two new initializers on `AuthenticatedImageView` accept the
protocols as injected dependencies. At app startup each platform's state
object creates the right concrete impls and passes them down via
`.environmentObject` or explicit initializer arguments. Prefer a single
`CacheBundle` environment object to avoid threading two protocols through
every call site:

```swift
// New in LumiverbKit
public struct CacheBundle: Sendable {
    public let proxies: any ProxyCache
    public let thumbnails: any ThumbnailCache
    public init(proxies: any ProxyCache, thumbnails: any ThumbnailCache) {
        self.proxies = proxies
        self.thumbnails = thumbnails
    }
}
```

- `AppState.swift` (macOS): builds `CacheBundle(proxies: MacProxyDiskCache.shared, thumbnails: MacThumbnailDiskCache.shared)`.
- `iOSAppState.swift` (iOS): builds `CacheBundle(proxies: MemoryImageCache(name: "ios.proxies"), thumbnails: IOSThumbnailDiskCache())`.
- Passed via a SwiftUI `Environment` value (`@Environment(\.cacheBundle)`)
  so `AuthenticatedImageView` doesn't need explicit injection.

**Environment key** (new file `LumiverbKit/Sources/LumiverbKit/API/CacheEnvironment.swift`):

**Resolution of the dual-protocol default.** The default-value problem is
solved by collapsing `IOSProxyMemoryCache` into a more general
`MemoryImageCache` that conforms to **both** protocols (NSCache shape works
for either; `removeAll()` flushes the underlying NSCache). The iOS
runtime uses two **separate** `MemoryImageCache` instances — one for
proxies, one for thumbnails — so they have independent budgets and never
contend. The environment default also uses two separate instances:

```swift
import SwiftUI

private struct CacheBundleKey: EnvironmentKey {
    // Default is a pair of independent in-memory caches so views work in
    // previews and tests without crashing on missing cache injection.
    // Tests that exercise cache behavior MUST inject a real CacheBundle —
    // the default has no disk layer, no eviction, and no realistic
    // budgets, and is sized for "render a SwiftUI preview without
    // exploding," not for any kind of integration testing.
    static let defaultValue = CacheBundle(
        proxies: MemoryImageCache(name: "preview.proxies"),
        thumbnails: MemoryImageCache(name: "preview.thumbnails")
    )
}

public extension EnvironmentValues {
    var cacheBundle: CacheBundle {
        get { self[CacheBundleKey.self] }
        set { self[CacheBundleKey.self] = newValue }
    }
}
```

`MemoryImageCache(name:)` takes a name purely so the two instances are
distinguishable in instruments / log dumps; behavior is identical between
them.

### ScrollViewAccessor architecture

**Problem.** `MediaGridView`, `SearchResultsGrid`, `SimilarResultsGrid`, and
`BrowseState` depend on `NSScrollViewIntrospector` and `NSScrollViewBox` in
`Sources/macOS/AppKitScrollIntrospector.swift` because SwiftUI's
`ScrollViewReader.scrollTo` silently fails on disposed `LazyVStack` cells.
These files cannot move to LumiverbKit with AppKit imports.

**Decision.** Define a `ScrollViewAccessor` protocol in LumiverbKit. The macOS
half stays in `Sources/macOS/` (keeps AppKitScrollIntrospector), the iOS half
uses a UIKit `UIViewRepresentable`, and the browse views accept the protocol
as an opaque `@ObservedObject` binding.

**Protocol** (new file `LumiverbKit/Sources/LumiverbKit/State/ScrollViewAccessor.swift`):

```swift
import Foundation

public enum ScrollCommand: Sendable, Equatable {
    case top
    case bottom
    case pageUp
    case pageDown
    case lineUp
    case lineDown
    case toRow(Int)
}

@MainActor
public protocol ScrollViewAccessor: AnyObject {
    func apply(_ command: ScrollCommand)
}
```

**macOS implementation** — keep the existing `NSScrollViewIntrospector` and
`NSScrollViewBox` in `Sources/macOS/AppKitScrollIntrospector.swift`. Add a
small `MacScrollAccessor` class that wraps `NSScrollViewBox` and conforms to
`ScrollViewAccessor`:

```swift
// Sources/macOS/AppKitScrollIntrospector.swift (append)
@MainActor
final class MacScrollAccessor: ObservableObject, ScrollViewAccessor {
    let box = NSScrollViewBox()
    func apply(_ command: ScrollCommand) {
        guard let sv = box.scrollView else { return }
        // Move the existing applyScrollCommand(_:to:) helper here unchanged.
    }
}
```

**iOS implementation** — new file
`Sources/iOS/UIKitScrollAccessor.swift`:

```swift
import SwiftUI
import UIKit
import LumiverbKit

@MainActor
final class IOSScrollAccessor: ObservableObject, ScrollViewAccessor {
    weak var scrollView: UIScrollView?

    /// Average row height in points, updated as the grid lays out. Used to
    /// approximate `toRow` jumps when there's no per-row geometry. Set by
    /// `MediaGridView`/`SearchResultsGrid` after layout via
    /// `accessor.averageRowHeight = ...`. Defaults to a sane mid-grid value
    /// so a jump-before-layout doesn't divide by zero.
    var averageRowHeight: CGFloat = 240

    func apply(_ command: ScrollCommand) {
        guard let sv = scrollView else { return }
        switch command {
        case .top:
            sv.setContentOffset(CGPoint(x: 0, y: -sv.adjustedContentInset.top), animated: true)
        case .bottom:
            let y = sv.contentSize.height - sv.bounds.height + sv.adjustedContentInset.bottom
            sv.setContentOffset(CGPoint(x: 0, y: max(0, y)), animated: true)
        case .pageUp:
            let y = max(0, sv.contentOffset.y - sv.bounds.height)
            sv.setContentOffset(CGPoint(x: 0, y: y), animated: true)
        case .pageDown:
            let maxY = sv.contentSize.height - sv.bounds.height
            let y = min(maxY, sv.contentOffset.y + sv.bounds.height)
            sv.setContentOffset(CGPoint(x: 0, y: y), animated: true)
        case .toRow(let row):
            // Best-effort jump using average row height. Justified-row
            // grids have variable row heights so this is approximate, not
            // pixel-perfect; the user lands within ~1 row of the target,
            // which is acceptable for "jump to search hit" UX. macOS uses
            // a precise NSTableView-style index map; iOS will gain that
            // precision when the grid exposes a per-row offset table.
            let target = max(0, CGFloat(row) * averageRowHeight - sv.adjustedContentInset.top)
            let maxY = max(0, sv.contentSize.height - sv.bounds.height)
            sv.setContentOffset(CGPoint(x: 0, y: min(target, maxY)), animated: true)
        case .lineUp, .lineDown:
            // iOS touch-first: no keyboard line-scroll semantics. No-op.
            break
        }
    }
}

struct UIScrollViewIntrospector: UIViewRepresentable {
    let onFound: (UIScrollView) -> Void

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        attemptFind(from: view)
        return view
    }

    /// Retry on every SwiftUI update so a late-mounted UIScrollView (fast
    /// navigation, LazyVStack recycling, NavigationStack push/pop) gets
    /// picked up. `attemptFind` is idempotent — if `onFound` was already
    /// called with a still-valid scroll view, the caller should ignore
    /// duplicate notifications (the IOSScrollAccessor's `weak` reference
    /// makes this safe).
    func updateUIView(_ uiView: UIView, context: Context) {
        attemptFind(from: uiView)
    }

    private func attemptFind(from view: UIView) {
        DispatchQueue.main.async {
            if let sv = Self.findScrollView(from: view) {
                onFound(sv)
            }
        }
    }

    static func findScrollView(from view: UIView) -> UIScrollView? {
        var v: UIView? = view.superview
        while let current = v {
            if let sv = current as? UIScrollView { return sv }
            for sub in current.subviews {
                if let sv = sub as? UIScrollView { return sv }
            }
            v = current.superview
        }
        return nil
    }
}
```

**Hardening note.** The `attemptFind` retry pattern is best-effort — if a
scroll view is found and later replaced (e.g. NavigationStack tab swap),
the introspector picks up the new one on the next update. If field
testing in M6 reveals flakiness (scroll commands silently dropped because
the accessor's `weak` reference is nil), the followup is to keep a
strong reference until explicit teardown rather than relying on
SwiftUI's update cadence.

**BrowseState refactor.** `BrowseState` currently has a
`pendingScrollCommand: ScrollCommandToken?` field and downstream views
observe it. Keep the field. Remove any direct references to
`NSScrollViewBox` from `BrowseState`. Views read the accessor from the
environment instead:

```swift
@Environment(\.scrollAccessor) private var scrollAccessor
.onChange(of: browseState.pendingScrollCommand) { _, token in
    guard let token else { return }
    scrollAccessor?.apply(token.command)
}
```

Where `scrollAccessor` is an optional environment value injected by each
platform.

**Grid view introspector parameter.** Dispatch via env is sufficient,
but the *attachment* of the introspector view has to happen inside the
grid's own `ScrollView { LazyVStack { ... } }` content (the introspector
walks its superview chain and only finds the platform scroll view if
it's a child). LumiverbKit can't import `NSScrollViewIntrospector` or
`UIScrollViewIntrospector` directly, so the grid views are **generic
over the introspector view type** and accept it as a `@ViewBuilder`
closure parameter:

```swift
public struct MediaGridView<ScrollIntrospector: View>: View {
    public init(
        browseState: BrowseState,
        client: APIClient?,
        @ViewBuilder scrollIntrospector: () -> ScrollIntrospector
    )
    public var body: some View {
        ScrollView {
            LazyVStack { ... }
                .background(scrollIntrospector)   // ← walks UP into the ScrollView
        }
    }
}

public extension MediaGridView where ScrollIntrospector == EmptyView {
    init(browseState: BrowseState, client: APIClient?)
}
```

macOS's `BrowseWindow` constructs the grid with a private
`macScrollIntrospector` closure that returns an `NSScrollViewIntrospector`
whose callback writes the discovered scroll view into
`appState.scrollAccessor.box.scrollView`. M6 will do the same on iOS
with `UIScrollViewIntrospector`. The `EmptyView` extension is for
previews/tests.

See "Grid views must be generic over their introspector view" in the
Implementation Notes section below for the rationale behind this
indirection.

### View relocation

Move these files from `Sources/macOS/` to
`LumiverbKit/Sources/LumiverbKit/Views/` (create the `Views/` directory if
it doesn't exist):

| File | Notes |
|------|-------|
| `AuthenticatedImageView.swift` | Replace `NSImage` with `PlatformImage` typealias; consume `CacheBundle` from environment |
| `FaceOverlayView.swift` | Pure SwiftUI. No changes beyond import adjustments |
| `LightboxView.swift` | Pure SwiftUI. No changes beyond import adjustments |
| `MediaGridView.swift` (+ `AssetCellView`) | Remove AppKit import, consume `ScrollViewAccessor` from environment |
| `SearchResultsGrid.swift` (+ `SearchHitCellView`) | Same as MediaGridView |
| `SimilarResultsGrid.swift` | Same as MediaGridView |
| `PeopleView.swift` | Pure SwiftUI |
| `PersonDetailView.swift` | Pure SwiftUI |
| `ClusterReviewView.swift` (+ `ClusterCardView`) | Pure SwiftUI |

Move these observable-state files to
`LumiverbKit/Sources/LumiverbKit/State/`:

| File | Notes |
|------|-------|
| `BrowseState.swift` | Remove `NSScrollViewBox`; use `ScrollViewAccessor` |
| `PeopleState.swift` | Pure Swift |
| `ClusterReviewState.swift` | Pure Swift |

These files **stay in `Sources/macOS/`**:

| File | Reason |
|------|--------|
| `AppKitScrollIntrospector.swift` | AppKit-only, macOS half of scroll abstraction |
| `BrowseWindow.swift` | Uses `NSWindow`, menu bar integration, macOS-only scene code |
| `LibrarySidebar.swift` | Uses macOS-specific sidebar style |
| `LibrarySettingsSheet.swift` | Uses `NSWorkspace.selectFile` |
| `MenuBarView.swift` | `MenuBarExtra` is macOS-only |
| `SettingsView.swift` | Tied to `Settings` scene type (macOS-only) |
| `LumiverbApp.swift` | macOS app entry, menu bar scenes |
| `AppState.swift` | Holds enrichment config + macOS-specific persistence |
| `Enrich/*` | Enrichment pipeline (Whisper, ArcFace, CLIP, Vision, OCR) — iOS does not enrich |
| `Scan/*` | Scan pipeline, library watcher — iOS does not scan |

### Ratings

**Server API is already complete** at
`src/server/api/routers/ratings.py`. Endpoints:

| Method | Path | Body | Response |
|--------|------|------|----------|
| PUT | `/v1/assets/{asset_id}/rating` | `{favorite?: bool, stars?: int, color?: string}` | `{asset_id, favorite, stars, color}` |
| PUT | `/v1/assets/ratings` | `{asset_ids: [string], favorite?, stars?, color?}` | `{updated: int}` |
| POST | `/v1/assets/ratings/lookup` | `{asset_ids: [string]}` | `{ratings: {asset_id: {favorite, stars, color}}}` |
| GET | `/v1/assets/favorites?after&limit` | — | `{items: [asset], next_cursor}` |

**Colors** are a closed set: `red`, `orange`, `yellow`, `green`, `blue`,
`purple`. Defined in `src/server/models/tenant.py:318`. Mirror as a
Swift enum in LumiverbKit.

**Stars** are integers 0–5 where 0 means "unrated". The server treats a
missing rating as `stars=0, favorite=false, color=null`.

**Color semantics.** `color` in the PUT body has a three-way distinction:
omitted (no change), explicit `null` (clear color), or a string (set).
The server reads the raw JSON to distinguish. The Swift client must
preserve this: use a custom encoding path rather than a standard Codable
struct, or send the raw JSON dictionary directly for `PUT /rating`.

**LumiverbKit additions** (new file
`LumiverbKit/Sources/LumiverbKit/Models/Rating.swift`):

```swift
import Foundation

public enum ColorLabel: String, Codable, CaseIterable, Sendable {
    case red, orange, yellow, green, blue, purple
}

public struct Rating: Codable, Equatable, Sendable {
    public let assetId: String
    public let favorite: Bool
    public let stars: Int
    public let color: ColorLabel?

    enum CodingKeys: String, CodingKey {
        case assetId = "asset_id"
        case favorite
        case stars
        case color
    }
}
```

**API client additions** to `LumiverbClient` (in `APIClient.swift`):

```swift
// Set/clear rating on a single asset. Pass nil for fields that should not
// change. Pass `.clear` for `color` to explicitly null it; nil leaves it
// unchanged.
public enum ColorChange: Sendable {
    case unchanged
    case clear
    case set(ColorLabel)
}

public func updateRating(
    assetId: String,
    favorite: Bool? = nil,
    stars: Int? = nil,
    color: ColorChange = .unchanged
) async throws -> Rating

public func batchUpdateRatings(
    assetIds: [String],
    favorite: Bool? = nil,
    stars: Int? = nil,
    color: ColorChange = .unchanged
) async throws -> Int  // returns `updated` count

public func lookupRatings(assetIds: [String]) async throws -> [String: Rating]

public func listFavorites(after: String? = nil, limit: Int = 200) async throws -> AssetPage
```

Implementation sends the request body as a `[String: Any]` dictionary so
`color` can be omitted, set to `NSNull()`, or set to a string.

**Editor UI** — new file
`LumiverbKit/Sources/LumiverbKit/Views/RatingEditorView.swift`:

```swift
public struct RatingEditorView: View {
    @Binding var rating: Rating
    let onChange: (ColorChange, Bool?, Int?) -> Void
    // Renders: heart toggle (favorite), 5-star row, 6-swatch color picker
    // + "clear color" button. Tappable on iOS, clickable on macOS.
    // No hover-only interactions.
}
```

**Integration points.**
- `LightboxView` sidebar: embed `RatingEditorView` beneath the metadata.
- `MediaGridView`: long-press context menu on iOS, right-click on macOS,
  with "Favorite / 1-5 stars / color..." items. Batch-applies to the
  current selection.
- Keyboard shortcuts on macOS only: `1`-`5` set stars, `0` clears,
  `F` toggles favorite. iOS has no keyboard shortcuts.

### Collections

**Server API is already complete** at
`src/server/api/routers/collections.py` and `public_collections.py`.

Visibility is a three-value enum:
- `private` — only the owner can view
- `shared` — any authenticated tenant member can view (the `_can_view`
  check allows both `shared` and `public` for non-owners)
- `public` — publicly resolvable via `/v1/public/collections/{id}` with no
  auth, plus mirrored in the control-plane `PublicCollectionRepository`

**Endpoints** (authenticated, `/v1/collections`):

| Method | Path | Purpose |
|--------|------|---------|
| POST | `` | Create collection (`name`, `description?`, `visibility=private`, `asset_ids?`) |
| GET | `` | List user's own + shared collections |
| GET | `/{id}` | Get detail |
| PATCH | `/{id}` | Update name/description/visibility/sort_order/cover |
| DELETE | `/{id}` | Delete |
| POST | `/{id}/assets` | Add assets (idempotent) |
| DELETE | `/{id}/assets` | Remove assets |
| GET | `/{id}/assets?after&limit` | List assets in collection |
| PATCH | `/{id}/reorder` | Reorder (manual sort_order only) |

**Public endpoints** (no auth, `/v1/public/collections`):

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/{id}` | Public metadata (name, description, cover, count) |
| GET | `/{id}/assets?after&limit` | Privacy-stripped asset list |

**LumiverbKit model additions** (new file
`LumiverbKit/Sources/LumiverbKit/Models/Collection.swift`):

```swift
public enum CollectionVisibility: String, Codable, Sendable {
    case `private`, shared, `public`
}

public enum CollectionSortOrder: String, Codable, Sendable {
    case manual, added_at, taken_at
}

public struct Collection: Codable, Identifiable, Sendable {
    public let id: String                 // server: collection_id
    public let name: String
    public let description: String?
    public let coverAssetId: String?
    public let ownerUserId: String?
    public let visibility: CollectionVisibility
    public let ownership: String          // "own" | "shared"
    public let sortOrder: CollectionSortOrder
    public let assetCount: Int
    public let createdAt: String
    public let updatedAt: String

    enum CodingKeys: String, CodingKey {
        case id = "collection_id"
        case name, description
        case coverAssetId = "cover_asset_id"
        case ownerUserId = "owner_user_id"
        case visibility, ownership
        case sortOrder = "sort_order"
        case assetCount = "asset_count"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}
```

**API client additions:**

```swift
public func listCollections() async throws -> [Collection]
public func getCollection(id: String) async throws -> Collection
public func createCollection(
    name: String,
    description: String? = nil,
    visibility: CollectionVisibility = .private,
    assetIds: [String]? = nil
) async throws -> Collection
public func updateCollection(
    id: String,
    name: String? = nil,
    description: String? = nil,
    visibility: CollectionVisibility? = nil,
    sortOrder: CollectionSortOrder? = nil,
    coverAssetId: String? = nil
) async throws -> Collection
public func deleteCollection(id: String) async throws
public func addAssetsToCollection(_ id: String, assetIds: [String]) async throws -> Int
public func removeAssetsFromCollection(_ id: String, assetIds: [String]) async throws -> Int
public func listCollectionAssets(_ id: String, after: String? = nil, limit: Int = 200) async throws -> AssetPage
public func reorderCollection(_ id: String, assetIds: [String]) async throws
```

**UI** (new files in `LumiverbKit/Sources/LumiverbKit/Views/`):

- `CollectionsListView.swift` — user's collections, grouped by ownership
  (Mine / Shared). Tap opens detail. "+" creates a new collection with a
  name prompt (sheet on iOS, form on macOS).
- `CollectionDetailView.swift` — title + metadata header, then a
  `MediaGridView` bound to the collection's asset page. Toolbar: rename,
  delete, share, reorder.
- `AddToCollectionSheet.swift` — appears from grid/lightbox with a
  multi-select list of existing collections and a "New collection..." row.

**Default visibility on create is always `private`.** The user can
promote to shared/public via the detail view's share action (M5).

**macOS integration**
- Add a "Collections" section to the sidebar in `BrowseWindow` (maybe
  under Libraries). Tapping a collection opens the detail view.
- Add "Add to collection..." to the existing right-click menu on grid
  items and in the lightbox toolbar.

### Collection sharing

**Decision.** Sharing is a visibility change: PATCH the collection with
`visibility: "shared"` (tenant-wide) or `visibility: "public"` (public URL).
Reverting is PATCH with `visibility: "private"`.

**Share URL format.** Defined by the **web UI**, not the API. Native clients
compose the URL by concatenating the configured server base URL with the
web app's public-collection route. The route is **verified** at
`src/ui/web/src/main.tsx:60`:

```tsx
<Route path="/public/collections/:collectionId" element={<PublicCollectionPage />} />
```

So the share URL is exactly `{serverBaseURL}/public/collections/{id}`.

If the web app's route ever changes, this ADR's assumption breaks and the
M5 implementer must re-verify before shipping. M5's "Done when" includes
a manual paste-the-link-into-a-browser check — that's the safety net.

Native clients cannot render public collections themselves in this ADR —
they only copy the link.

**Share UI.**
- Native iOS: `ShareLink(item: URL(...))` in a sheet.
- Native macOS: a "Copy share link" button and a "Manage visibility"
  picker (Private / Shared with tenant / Public).

**Revoke.** PATCH visibility back to `private`. Confirm dialog before
revoking because any shared links become dead.

### iOS app shell

**Target state.** `Lumiverb-iOS` launches into a `TabView`:

1. **Browse** — library picker → `MediaGridView` for the selected library
2. **Search** — `SearchResultsGrid`
3. **People** — `PeopleView` → `PersonDetailView`
4. **Collections** — `CollectionsListView` → `CollectionDetailView`
5. **Settings** — account info, server URL, logout, app version

`iOSAppState` is already the singleton holder for auth + libraries. Extend
it with a `selectedLibraryId: String?` that persists in `UserDefaults`
(key `"io.lumiverb.app.lastLibraryId"`). When the Browse tab opens with
`selectedLibraryId == nil`, show a library picker; once selected, jump
straight into the grid on subsequent launches.

**Navigation.** One `NavigationStack` per tab, so deep navigation within
People → PersonDetail doesn't interfere with the Browse tab. The lightbox
is a full-screen cover (`.fullScreenCover`) from the Browse tab, not a
navigation push, so swipe-down-to-dismiss works naturally.

**Source media fence.** Browse views must never construct a URL ending in
`/source` (or `/download`, or `/original`) for an asset. All image
fetches go through `/thumbnail` or `/proxy`. M6 adds an
`XCTest`-based fence: `SourceFencingTests` enumerates every `.swift`
file under `LumiverbKit/Sources/LumiverbKit/Views/` and fails the test
suite if any file contains any of these literals as a substring:
`"/source"`, `"/download"`, `"/original"`. Tests don't try to parse the
Swift — substring matching is intentional, simple, and rejects all the
cases we care about. False positives (e.g. a comment that mentions
`/source`) are acceptable: the implementer reworks the comment.

### iOS touch adaptations

**Cluster management on iOS** adapts the existing macOS face-tagging UI (built in ADR-014 Phase 6):
- Right-click → long-press with context menu
- Hover highlights → tap highlights with timed fade
- Keyboard shortcuts → swipe actions on rows
- Popover person-picker → sheet-based person picker
- Click-to-dismiss → swipe-left to dismiss

**Face overlay on iOS.** The overlay's tap target must be at least 44×44
(Apple HIG). On the macOS app the overlay rectangles are tight to the
face bbox; on iOS, inflate the hit region without inflating the visible
rectangle.

### iOS cellular + Low Data Mode

Use `NWPathMonitor` to observe network status. Two observable signals:
`isConstrained` (`isConstrained` from `NWPath`) and `isCellular`
(`usesInterfaceType(.cellular)`).

**The policy enum** (applies to iOS only; macOS is unchanged):

```swift
public enum NetworkPolicy: Sendable {
    case full          // prefetch + neighbor preload + autoplay
    case conservative  // no neighbor preload, no autoplay; viewport prefetch OK
    case minimal       // viewport only; no prefetch, no preload, no autoplay
}
```

**Path → policy mapping (the only place this decision is made):**

| `isCellular` | `isConstrained` | Resulting `NetworkPolicy` |
|--------------|-----------------|---------------------------|
| false | false | `.full` |
| false | true (Low Data Mode on WiFi) | `.conservative` |
| true | false | `.conservative` |
| true | true (Low Data Mode on cellular) | `.minimal` |

The `NetworkMonitor` listener computes the policy from the current path
*exactly once*, at the path-update boundary. Views never re-derive the
policy from `isCellular`/`isConstrained` directly — they only read
`networkPolicy` from the environment. This is intentional: it gives one
canonical place where the mapping lives, so M8 doesn't fork it
across views.

**Per-policy view behavior:**

| Policy | Viewport prefetch | Neighbor preload | Video autoplay | Face embedding fetch |
|--------|-------------------|-------------------|----------------|----------------------|
| `.full` | ✅ | ✅ (lightbox N±1) | ✅ | ✅ |
| `.conservative` | ✅ | ❌ | ❌ | ✅ (lazy) |
| `.minimal` | ❌ | ❌ | ❌ | ❌ |

A user-controllable "Always conservative" toggle in Settings clamps the
result to `.conservative` (or whatever it would have been if more
restrictive). It never relaxes the network-derived policy — only
tightens it.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| User logs out while on iOS Collections tab | Drop to login screen, clear all state |
| iOS thumbnail cache exceeds 200 MB in a single session | Approximate-LRU (oldest-by-mtime) eviction runs on next `put()`, deletes down to 180 MB; user notices nothing |
| iOS user jumps to a search hit far down the result list | `IOSScrollAccessor.apply(.toRow)` computes target offset as `row * averageRowHeight`. Approximate (lands within ~1 row of target on justified-row grids); precise indexing is a follow-up if needed |
| iOS UIScrollView not yet attached when first scroll command fires | `IOSScrollAccessor.scrollView` is nil; command silently no-ops. The next `UIScrollViewIntrospector.updateUIView` cycle re-attaches and subsequent commands work. Document and revisit if observed in field testing |
| iOS app backgrounded during proxy download | `URLSession` cancels or completes — on next foreground, retry lazily |
| Collection deleted on another client while open | Detail view shows "Collection not found" and pops back |
| Rating color set to invalid string | Client validates against `ColorLabel` enum before sending; server returns 422 if bypass attempted |
| Setting a star rating of 0 | Treated as "clear rating"; server accepts `stars=0` |
| Collection visibility toggled public while shared link was cached | Old link works; no action needed |
| Collection visibility toggled public → private | Server deletes the `public_collections` control-plane row; any cached share link now 404s |
| iOS user taps an asset that has no proxy yet (still enriching) | Show a placeholder and a "Still processing" hint; do not fall back to source |
| User tries to add 2000 assets to a collection in one go | Client chunks into batches of 1000 (server cap) and retries as needed |
| iOS on cellular taps into lightbox for a large video | Do not autoplay; show thumbnail + tap-to-play; still only load the proxy |
| LumiverbKit test suite runs on macOS and touches `MemoryImageCache` | The cache is platform-neutral (NSCache works on macOS too); tests run everywhere |
| User pinches-to-zoom on lightbox on iOS | Standard `ScrollView` zoom; macOS uses a different gesture model — keep them separate |
| Face cluster tagged simultaneously from macOS and iOS | Last write wins; no conflict resolution needed |

## Code References

| Area | File | Notes |
|------|------|-------|
| Cache — current macOS unbounded disk | `clients/lumiverb-app/Sources/LumiverbKit/Sources/LumiverbKit/API/ProxyCacheOnDisk.swift` | Rename to `MacProxyDiskCache`, conform to `ProxyCache` protocol |
| Cache — current macOS thumbnail disk | `clients/lumiverb-app/Sources/LumiverbKit/Sources/LumiverbKit/API/ThumbnailCacheOnDisk.swift` | Rename to `MacThumbnailDiskCache`, conform to `ThumbnailCache` protocol |
| Cache consumer | `clients/lumiverb-app/Sources/macOS/AuthenticatedImageView.swift` | Move to LumiverbKit, swap to protocol + `PlatformImage` |
| PlatformImage typealias | `clients/lumiverb-app/Sources/LumiverbKit/Sources/LumiverbKit/API/ImageCache.swift` | Already defined; use everywhere |
| Scroll introspection (macOS-only) | `clients/lumiverb-app/Sources/macOS/AppKitScrollIntrospector.swift` | Keep in place, extend with `MacScrollAccessor` |
| Browse state machine | `clients/lumiverb-app/Sources/macOS/BrowseState.swift` | Move to LumiverbKit, remove NSScrollViewBox reference |
| Grid view | `clients/lumiverb-app/Sources/macOS/MediaGridView.swift` | Move to LumiverbKit, consume env `ScrollViewAccessor` |
| Search grid | `clients/lumiverb-app/Sources/macOS/SearchResultsGrid.swift` | Same as MediaGridView |
| Similar grid | `clients/lumiverb-app/Sources/macOS/SimilarResultsGrid.swift` | Same as MediaGridView |
| Lightbox | `clients/lumiverb-app/Sources/macOS/LightboxView.swift` | Pure SwiftUI move |
| Face overlay | `clients/lumiverb-app/Sources/macOS/FaceOverlayView.swift` | Pure SwiftUI move |
| People list/detail | `clients/lumiverb-app/Sources/macOS/{PeopleView,PersonDetailView}.swift` | Pure SwiftUI move |
| Cluster review | `clients/lumiverb-app/Sources/macOS/ClusterReviewView.swift` | Pure SwiftUI move |
| iOS app state | `clients/lumiverb-app/Sources/iOS/iOSAppState.swift` | Extend with `selectedLibraryId`, `networkPolicy` |
| iOS placeholder | `clients/lumiverb-app/Sources/iOS/ConnectedView.swift` | Delete — replaced by `TabView` root |
| Ratings server | `src/server/api/routers/ratings.py` | API shape reference; do not modify |
| Collections server | `src/server/api/routers/collections.py` | API shape reference; do not modify |
| Public collections server | `src/server/api/routers/public_collections.py` | API shape reference; do not modify |
| Valid color constants | `src/server/models/tenant.py:318` | `VALID_COLORS = {red, orange, yellow, green, blue, purple}` |
| Public collection web route | `src/ui/web/src/main.tsx:60` | `<Route path="/public/collections/:collectionId" />` — verified; share URL format is `{serverBaseURL}/public/collections/{id}` |
| Collection JSON shape (server side) | `src/server/api/routers/collections.py` `_collection_to_item` | Verified `owner_user_id: str \| None` (legacy tenant-wide collections have null owner). Swift `Collection.ownerUserId` matches this optionality |
| XcodeGen project | `clients/lumiverb-app/project.yml` | No structural changes required; sources already split by target |

## Doc References

- `docs/cursor-api.md` — add ratings and collections endpoints to the client-facing API reference.
- `docs/adr/014-native-clients.md` — mark Phase 5 complete when this ADR's milestones finish; update Phase 6 status for iOS.
- `CLAUDE.md` — update the "Finding things by topic" table: move browse view paths from `Sources/macOS/` to `LumiverbKit/Sources/LumiverbKit/Views/`, add rows for ratings, collections, cache protocols, scroll accessor.

## Implementation Notes (learned during M1 + M2)

These are patterns that surfaced during M1/M2 implementation. They apply
to M3 and later — read this section before starting any milestone that
moves code into LumiverbKit or makes existing observable state
cross-module.

### Cross-module observable state needs protocol abstraction

When `BrowseState` moved into LumiverbKit it had hard references to
**three** macOS-only types that couldn't follow it across the module
boundary:

1. `AppState` — full ObservableObject with enrichment config + library list + auth
2. `ReEnrichmentRunner` — actor that imports CLIP/ArcFace/Whisper/Vision/OCR providers
3. `CLIPProvider` / `FeaturePrintProvider` — direct module references for embedding model id/version

The fix is the same in each case: define a protocol in LumiverbKit that
captures the *minimal* surface BrowseState needs, and have the macOS
type conform via an extension. The macOS type stays in `Sources/macOS/`
and pulls in whatever providers it wants; LumiverbKit only sees the
protocol shape.

This pattern will recur in every milestone that adds a feature to a
LumiverbKit-resident view. **Apply it when:**
- The feature wants something that lives in macOS-only enrichment code
- The feature wants something from `AppState` that isn't already on `BrowseAppContext`
- The feature wants to invoke a runner/provider with a long-running task

**Don't try to move the macOS-only thing into LumiverbKit** — the
providers genuinely cannot move (they import CoreML / Vision / Speech /
custom CoreML models that the iOS browse target doesn't link). The
protocol seam is the right answer.

### Different state classes need different protocol surfaces

`BrowseState` needs the full `BrowseAppContext` (vision/whisper config,
embedding model id, library list, client). But `PeopleState` and
`ClusterReviewState` only need a `client: APIClient?`. Don't force all
state classes through the same protocol — minimal-surface initialization
keeps the test/preview story simple. iOS-side construction is just
`PeopleState(client: someClient)`, no protocol existential gymnastics.

### Grid views must be generic over their introspector view

The `\.scrollAccessor` env value is sufficient for *dispatching* scroll
commands, but the **attachment** of the introspector view has to happen
*inside* the grid's `ScrollView { LazyVStack { ... } }` content. The
introspector walks UP its superview chain to find the platform scroll
view; if you put it outside the ScrollView (e.g. as a `.background` of
the grid itself), it walks the wrong direction.

LumiverbKit can't import `NSScrollViewIntrospector` / `UIScrollViewIntrospector`
directly, so the grids are **generic over `ScrollIntrospector: View`**
and accept the introspector as a `@ViewBuilder` closure parameter. The
macOS callsite passes `NSScrollViewIntrospector { ... }`; the iOS
callsite (M6) will pass `UIScrollViewIntrospector { ... }`. Provide an
`extension where ScrollIntrospector == EmptyView` convenience init for
previews/tests.

Apply this same pattern to any future LumiverbKit view that needs to
attach a *platform-specific child view* whose location matters. The env
value is for cross-cutting state; the generic parameter is for child
views.

### Visibility cascade is the dominant cost of moving state

Making `BrowseState` cross-module required adding `public` to:
- The class declaration + the `init`
- ~25 `@Published` properties (every one read or written from outside the module)
- ~15 methods called from `BrowseWindow.swift` or grid views
- 1 associated enum (`BrowseMode`) with `Sendable` conformance for the public property

This was a significant chunk of mechanical work and several `replace_all`
gotchas. Budget for ~50 `public` annotations whenever you move an
observable-state class to LumiverbKit. Use `git diff --stat` after each
publication pass to track surface growth.

**Replace_all gotcha:** the Edit tool's `replace_all` mode silently
trims trailing whitespace from `old_string` / `new_string`. Don't try
`@Published var ` → `@Published public var ` (with trailing spaces) — it
becomes `@Published public var<no space>` and breaks every declaration.
Use `@Published var X` → `@Published public var X` per-property if you
need the trailing space, or include the next character explicitly in
the pattern.

### Swift type-checker timeout on cross-module SwiftUI bodies

After `BrowseState` becomes a cross-module public type, several inline
SwiftUI closures in `BrowseWindow.swift` start failing with:

> error: the compiler is unable to type-check this expression in
> reasonable time; try breaking up the expression into distinct
> sub-expressions

The trigger is the combination of:
- Cross-module observable references (each access goes through a public getter)
- Existential types (`any BrowseAppContext`)
- Nested SwiftUI bodies (ZStack + switch + ForEach + condition chains)
- Optional unwrap chains over cross-module properties

The fix is mechanical: extract the offending body into a `@ViewBuilder`
helper (`private var thingX: some View` / `private func thingX(arg:) -> some View`).
Each helper is type-checked independently. We hit this 3 times in M2 —
expect to hit it again whenever you add features to `BrowseWindow.swift`
or any other macOS-side view that consumes BrowseState.

**Symptom → fix lookup:**
- Inline `.searchSuggestions { ForEach(...) }` over cross-module models → extract `personSuggestions`/`personSuggestionRow`-style helpers
- Inline `detail: { ZStack { switch ... } }` → extract `detailContent`/`sectionContent`/`columnContent`
- Multi-condition `if let ... else if let ... else if ...` chains → extract `private func evaluateX()`

### Color literals need conditional compilation

`Color(nsColor: .controlBackgroundColor)` is macOS-only. iOS uses
`Color(uiColor: .secondarySystemBackground)`. Wrap in
`#if canImport(AppKit) / #elseif canImport(UIKit)`. We hit two instances
in M2 (LightboxView, ClusterReviewView) — when porting any view that
sets a system background or fill color, grep for `nsColor:` /
`NSColor.` first.

### Trace dependencies before listing files to move

The original M2 file list missed three files: `FaceThumbnailView`,
`LightboxVideoPlayerView`, `ReEnrichMenu`. They're not "browse views"
in the obvious sense, but they're transitively referenced from
`PersonDetailView` / `ClusterReviewView` / `LightboxView` and have to
move with them. Before any future relocation milestone, grep the
files-to-move for type names that aren't in the move set, and add them.

### `LightboxVideoPlayerView` shows the cross-platform video pattern

Video playback in the lightbox needs both platforms but uses different
AVKit APIs:

```swift
#if canImport(AppKit)
struct PlayerView: NSViewRepresentable {
    let player: AVPlayer
    func makeNSView(context: Context) -> AVPlayerView { ... }
    func updateNSView(_ nsView: AVPlayerView, context: Context) { ... }
}
#elseif canImport(UIKit)
struct PlayerView: UIViewControllerRepresentable {
    let player: AVPlayer
    func makeUIViewController(context: Context) -> AVPlayerViewController { ... }
    func updateUIViewController(_ uiVC: AVPlayerViewController, context: Context) { ... }
}
#endif
```

Same pattern applies to any future view that needs a `PlatformRepresentable`
wrapper around an AppKit/UIKit class. Keep the type name identical
(`PlayerView`) so the call site doesn't need conditional compilation —
only the implementation does.

### iOS "no library root" path is dead code on iOS, gate explicitly

`LightboxVideoPlayerView`'s "try local file at `{libraryRootPath}/{relPath}`"
branch is dead on iOS because iOS has no library root path (iOS is
sandboxed and never sees a real filesystem path). The iOS
`BrowseAppContext` will return nil for `selectedLibraryRootPath`, so
the `if let rootPath` guard already short-circuits. We *also* gate the
whole branch with `#if os(macOS)` to make the intent explicit and
avoid carrying any AppKit-coupled `(rootPath as NSString)...`
incantations into iOS builds.

When you encounter a branch that "is dead on iOS because the iOS
context returns nil," gate it with `#if os(macOS)` anyway. Future
edits to the runtime shouldn't quietly resurrect the dead branch.

## Build Milestones

### Requirements

Every milestone must satisfy all of the following before it is marked complete:

1. **Swift builds on both targets.** `xcodebuild -project Lumiverb.xcodeproj -scheme Lumiverb-macOS build` and `xcodebuild -project Lumiverb.xcodeproj -scheme Lumiverb-iOS build CODE_SIGNING_ALLOWED=NO` both succeed. Any milestone that only touches macOS code still needs the iOS target to build.
2. **Tests pass.** `swift test` on the LumiverbKit package runs green. Any milestone that adds API client methods must add tests for them using the existing network-mocking pattern in `APIClientNetworkTests.swift`.
3. **No regressions.** The macOS app's existing behavior (browse, enrich, scan, face tagging) is unchanged. Run the macOS app manually at the end of each milestone and verify core flows.
4. **Docs updated.** CLAUDE.md's topic table reflects any file moves. New features are mentioned in `docs/cursor-api.md`.
5. **Milestone status table updated** at the top of this ADR.

### Milestone 1 — Foundation refactors

**Goal:** lay down the abstractions that everything else depends on, without moving views yet. Macos app continues to work unchanged.

**Deliverables:**
1. New file `LumiverbKit/Sources/LumiverbKit/API/CacheProtocols.swift` defining `ProxyCache`, `ThumbnailCache`, `CacheBundle`.
2. New file `LumiverbKit/Sources/LumiverbKit/API/MemoryImageCache.swift` — the iOS-friendly in-memory impl that satisfies both protocols.
3. Rename `ProxyCacheOnDisk.swift` → `MacProxyDiskCache.swift`, wrap entire file in `#if os(macOS)`, add `extension MacProxyDiskCache: ProxyCache {}`.
4. Rename `ThumbnailCacheOnDisk.swift` → `MacThumbnailDiskCache.swift`, wrap in `#if os(macOS)`, add conformance.
5. New file `IOSThumbnailDiskCache.swift` — disk cache in `.cachesDirectory` with LRU eviction at 200 MB. Wrap in `#if os(iOS)`.
6. New file `CacheEnvironment.swift` — SwiftUI environment key for `CacheBundle`.
7. Move `AuthenticatedImageView.swift` from `Sources/macOS/` to `LumiverbKit/Sources/LumiverbKit/Views/AuthenticatedImageView.swift`. Swap `NSImage` → `PlatformImage`, swap `ProxyCacheOnDisk.shared` / `ThumbnailCacheOnDisk.shared` to environment-injected `CacheBundle`.
8. New file `LumiverbKit/Sources/LumiverbKit/State/ScrollViewAccessor.swift` defining the protocol and `ScrollCommand` enum.
9. Extend `Sources/macOS/AppKitScrollIntrospector.swift` with `MacScrollAccessor` conforming to `ScrollViewAccessor`. The existing `applyScrollCommand(_:to:)` helper moves into `MacScrollAccessor.apply(_:)`.
10. Add `#if os(macOS)` guard to `FileTokenStore.init` in `LumiverbKit/Sources/LumiverbKit/Auth/FileTokenStore.swift` — iOS users get a `fatalError` if they instantiate it.
11. macOS `AppState` builds a `CacheBundle(proxies: MacProxyDiskCache.shared, thumbnails: MacThumbnailDiskCache.shared)` and installs it as an environment value at the root of the macOS scene.
12. Callers of `ProxyCacheOnDisk.shared` from enrichment/scan code (`Sources/macOS/Scan/*`, `Sources/macOS/Enrich/*`) keep using the concrete class directly via a `MacProxyDiskCache.shared` reference. Do not route enrichment through the protocol — the scan-specific methods (`putScan`, `getSHA`, `isValid`) are not part of the protocol.

**Does NOT include:** Moving grid views, moving BrowseState, any iOS-visible behavior. The iOS target continues to build with just the scaffold.

**Read-ahead:** M2 moves grid views. When `MediaGridView` moves, it will take a `@Environment(\.scrollAccessor)` — the environment key for `ScrollViewAccessor` should be defined in M1 alongside the `CacheBundle` environment key.

**Done when:**
- [x] Both targets build
- [x] `swift test` green
- [x] Existing macOS app browse flow verified manually
- [x] Milestone table updated

### Milestone 2 — Move browse UI into LumiverbKit

**Goal:** Relocate all browse-related views and state from `Sources/macOS/` into `LumiverbKit/Sources/LumiverbKit/Views/` and `State/`. After this milestone the macOS app still works but its browse code now lives in LumiverbKit, and iOS can import the same views.

**Deliverables** — move (not copy) these files with the following mechanical adjustments:

| From | To | Adjustments |
|------|----|-------------|
| `Sources/macOS/FaceOverlayView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/FaceOverlayView.swift` | Remove `import LumiverbKit` (now self-import) |
| `Sources/macOS/LightboxView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/LightboxView.swift` | Gate `NSWorkspace.shared.activateFileViewerSelecting` and `NSWorkspace.shared.open` with `#if os(macOS)`. Gate `Color(nsColor: .controlBackgroundColor)` with `#if canImport(AppKit) / #elseif canImport(UIKit) → Color(uiColor: .secondarySystemBackground)` |
| `Sources/macOS/FaceThumbnailView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/FaceThumbnailView.swift` | **Discovered during M2** — referenced from PersonDetailView/ClusterReviewView, has to move with them. Swap `NSImage` → `PlatformImage` + `Image(nsImage:)` / `Image(uiImage:)` under `#if canImport`. Make `public struct` + `public init`. |
| `Sources/macOS/LightboxVideoPlayerView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/LightboxVideoPlayerView.swift` | **Discovered during M2** — referenced from LightboxView. The `PlayerView` wrapper splits into two `#if canImport(AppKit) / canImport(UIKit)` branches: `NSViewRepresentable<AVPlayerView>` for macOS, `UIViewControllerRepresentable<AVPlayerViewController>` for iOS. The local-file playback branch is gated `#if os(macOS)` because iOS has no library root. |
| `Sources/macOS/ReEnrichMenu.swift` | `LumiverbKit/Sources/LumiverbKit/Views/ReEnrichMenu.swift` | **Discovered during M2** — referenced from LightboxView. Pure SwiftUI; just needs `public struct` + `public init`. |
| `Sources/macOS/MediaGridView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/MediaGridView.swift` | Make generic over `ScrollIntrospector: View` parameter; consume `@Environment(\.scrollAccessor)` for command dispatch. See "grid view introspector pattern" below. |
| `Sources/macOS/SearchResultsGrid.swift` | `LumiverbKit/Sources/LumiverbKit/Views/SearchResultsGrid.swift` | Same generic introspector pattern |
| `Sources/macOS/SimilarResultsGrid.swift` | `LumiverbKit/Sources/LumiverbKit/Views/SimilarResultsGrid.swift` | Same generic introspector pattern |
| `Sources/macOS/PeopleView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/PeopleView.swift` | `public struct` + `public init` |
| `Sources/macOS/PersonDetailView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/PersonDetailView.swift` | `public struct` + `public init` |
| `Sources/macOS/ClusterReviewView.swift` | `LumiverbKit/Sources/LumiverbKit/Views/ClusterReviewView.swift` | `public struct` + `public init`. Same `Color(nsColor:)` gating as LightboxView. |
| `Sources/macOS/BrowseState.swift` | `LumiverbKit/Sources/LumiverbKit/State/BrowseState.swift` | Major refactor — see "BrowseState cross-module surgery" below |
| `Sources/macOS/PeopleState.swift` | `LumiverbKit/Sources/LumiverbKit/State/PeopleState.swift` | `public final class`. Drop `appState: AppState` field; take `client: APIClient?` directly. |
| `Sources/macOS/ClusterReviewState.swift` | `LumiverbKit/Sources/LumiverbKit/State/ClusterReviewState.swift` | Same: `public final class`, take `client: APIClient?`. |

**New files in LumiverbKit (created during M2 to break cross-module dependencies):**
- `LumiverbKit/Sources/LumiverbKit/State/BrowseAppContext.swift` — protocol that BrowseState consumes instead of holding a concrete `AppState` reference. Surfaces: `client`, `libraries`, `whisperEnabled` (+ `whisperEnabledPublisher: AnyPublisher<Bool, Never>`), `embeddingModelId`, `embeddingModelVersion`, vision/whisper enrichment config (`resolvedVisionApiUrl`/`resolvedVisionApiKey`/`resolvedVisionModelId`/`whisperModelSize`/`whisperLanguage`/`whisperBinaryPath`). macOS `AppState` conforms via an extension at the bottom of `Sources/macOS/AppState.swift`. iOS will get an `iOSAppState` adapter in M6.
- `LumiverbKit/Sources/LumiverbKit/State/ReEnrichInvoker.swift` — pluggable re-enrichment runner protocol. BrowseState delegates `reEnrich` / `reEnrichAsset` to the invoker so LumiverbKit doesn't need to import macOS-only enrichment providers (CLIP, ArcFace, Whisper, Vision, OCR, FeaturePrint). Includes a `ReEnrichmentResult` value type. iOS leaves it nil; the methods short-circuit.

**New files in macOS sources:**
- `Sources/macOS/Enrich/MacReEnrichInvoker.swift` — wraps the existing `ReEnrichmentRunner`, owns the polling task that forwards progress (`processed`/`total`/`phase`) into BrowseState's `@Published` fields via the closure parameter.

**Grid view introspector pattern.** The grid views (`MediaGridView`, `SearchResultsGrid`, `SimilarResultsGrid`) cannot consume the `\.scrollAccessor` env value alone — they also need to *attach* a platform-specific introspector view inside their `ScrollView { LazyVStack { ... } }` content so the introspector's superview walk reaches the real `NSScrollView` / `UIScrollView`. LumiverbKit can't import `NSScrollViewIntrospector` directly, so the grids are **generic over the introspector view type**:

```swift
public struct MediaGridView<ScrollIntrospector: View>: View {
    public init(
        browseState: BrowseState,
        client: APIClient?,
        @ViewBuilder scrollIntrospector: () -> ScrollIntrospector
    ) { ... }

    public var body: some View {
        ScrollView {
            LazyVStack { ... }
                .background(scrollIntrospector)   // ← inside the scroll view content
        }
        .onChange(of: browseState.pendingScrollCommand) { _, token in
            scrollAccessor?.apply(token.command)  // ← env-injected dispatch
        }
    }
}

public extension MediaGridView where ScrollIntrospector == EmptyView {
    init(browseState: BrowseState, client: APIClient?) {
        self.init(browseState: browseState, client: client) { EmptyView() }
    }
}
```

The macOS-side `BrowseWindow` constructs the grids with a private `macScrollIntrospector` view that wraps `NSScrollViewIntrospector` and writes the discovered scroll view into `appState.scrollAccessor.box.scrollView`. The iOS side (M6) will pass a `UIScrollViewIntrospector` doing the same with `UIScrollView`. The `EmptyView` extension exists for previews and tests that don't have a real scroll view to attach to.

A new public `MediaGridLayoutConstants` enum at the top of `MediaGridView.swift` exposes `targetRowHeight` (180), `spacing` (4), and `verticalLineScrollHeight` (184) so the macOS introspector callback can configure `NSScrollView.verticalLineScroll` to match the grid's row height (one arrow-key tap should advance ~one row, not the AppKit ~10pt default).

**BrowseState cross-module surgery.** The original M2 plan said "remove NSScrollViewBox field; remove any direct AppKit references." In practice the move was much larger because BrowseState had hard references to:

1. **`AppState`** (macOS-only ObservableObject with enrichment config) → Replaced with `appContext: any BrowseAppContext`. The init signature changed from `init(appState: AppState)` to `init(appContext: any BrowseAppContext)`. macOS `BrowseWindow.init` now passes `appState` (which conforms to the protocol via an extension).
2. **`ReEnrichmentRunner`** (macOS-only enrichment runner that imports CLIP/ArcFace/Whisper/Vision/OCR providers) → BrowseState's `reEnrich` / `reEnrichAsset` methods now delegate to a pluggable `reEnrichInvoker: (any ReEnrichInvoker)?` property. macOS sets this from `BrowseWindow.init` to a `MacReEnrichInvoker(appState:)`. iOS leaves it nil and the methods short-circuit. The progress polling moved out of BrowseState into the macOS-side invoker; BrowseState only owns the `@Published` mirror that the invoker updates via a closure parameter.
3. **`CLIPProvider.modelId` / `CLIPProvider.modelVersion` / `FeaturePrintProvider.modelId`/`modelVersion`** (used by `findSimilar` to tell the server which embedding vectors to look up) → replaced with `appContext.embeddingModelId` / `embeddingModelVersion`. macOS reports the local provider; iOS reports a canonical CLIP id since iOS doesn't enrich.
4. **`var query` capture in a sendable closure** (Swift 6 strict-concurrency violation that was a *warning* in the macOS target but became an *error* in LumiverbKit's stricter mode) → captured into an immutable `let queryForRequest` before the closure.

The class declaration becomes `@MainActor public class BrowseState: ObservableObject` and **all** of the following gain `public` modifiers because they're called from `BrowseWindow.swift` (cross-module): `appContext`, `selectedLibraryRootPath`, `client`, `init`, `sendScrollCommand`, all 25+ `@Published` properties (selectedLibraryId, pendingScrollCommand, directories, expandedPaths, childDirectories, selectedPath, filters, personSuggestions, assets, isLoadingAssets, hasMoreAssets, isChangingLibrary, libraryChangeError, searchQuery, searchResults, searchTotal, isSearching, similarResults, similarTotal, isFindingSimilar, similarSourceId, selectedAssetId, assetDetail, isLoadingDetail, displayedAssetIdsOverride, displayedFaceIdsOverride, pendingHighlightFaceId, mode, error, focusedIndex, whisperEnabled, isReEnriching, reEnrichPhase, reEnrichTotal, reEnrichProcessed, reEnrichSkipped), the `reEnrichInvoker` property, and ~15 methods (`loadNextPage`, `loadAssetDetail`, `loadRootDirectories`, `selectPath`, `toggleExpanded`, `performSearch`, `clearSearch`, `debouncedPersonSearch`, `filterByPerson`, `clearPersonFilter`, `closeLightbox`, `navigateLightbox`, `revertLibraryChange`, `retryLibraryChange`, `handleSelectedLibraryChange`, `cancelReEnrich`, `reEnrich`, `reEnrichAsset`). The associated `BrowseMode` enum also needs `public enum BrowseMode: Equatable, Sendable`.

**BrowseWindow.swift type-checker fixes.** After BrowseState becomes a cross-module public type, several inline closures in `BrowseWindow.swift` exceed Swift's type-checker timeout ("the compiler is unable to type-check this expression in reasonable time"). Three locations need extraction into helpers:
1. The `.searchSuggestions { }` `ForEach` over `[PersonItem]` → extract `personSuggestions: some View` and `personSuggestionRow(person:) -> some View`
2. The `detail: { }` closure of `NavigationSplitView` → extract `detailContent: some View`, `sectionContent: some View`, `libraryColumn: some View`
3. The `.onChange(of: appState.libraries.count)` block with a triple if-let chain → extract `private func restoreLastOpenedLibraryIfNeeded()`

These are mechanical extractions; behavior is preserved exactly.

**Additional adjustments:**
- Remove `import LumiverbKit` from every moved file (it becomes a self-import after the move).
- The macOS app's `BrowseWindow.swift` stays in `Sources/macOS/` and imports `LumiverbKit` to use the moved views. It already installs the `CacheBundle` and `MacScrollAccessor` as environment values from M1.
- Update test imports/references in `LumiverbKit/Tests/LumiverbKitTests/` that referenced moved types (none did in practice — the tests touch models/API/cache/scroll, not browse state).
- `Sources/macOS/AppState.swift` adds `import Combine` at the top (needed for `AnyPublisher` in the conformance extension).

**Does NOT include:** Any iOS UI work. iOS still lands in `ConnectedView` — we haven't built the tab bar yet.

**Read-ahead:** M6 builds the iOS TabView. `MediaGridView` will be consumed by the iOS Browse tab — the iOS callsite will pass a `UIScrollViewIntrospector` closure and otherwise look identical to the macOS callsite.

**Done when:**
- [x] Both targets build (iOS build doesn't need to *use* the new views yet, just compile them)
- [x] `swift test` green
- [x] macOS app browse + search + people + clusters all work manually
- [x] Milestone table updated

### Milestone 3 — Ratings editor

**Goal:** Set ratings from macOS (and, later, iOS). Feature lands in LumiverbKit from day one.

**Deliverables:**
1. `LumiverbKit/Sources/LumiverbKit/Models/Rating.swift` — `ColorLabel` enum, `Rating` struct, `ColorChange` enum.
2. `APIClient.swift` additions: `updateRating`, `batchUpdateRatings`, `lookupRatings`, `listFavorites`. Each sends the request body as a dictionary so `color` can be omitted/null/set.
3. Tests in `APIClientNetworkTests.swift` covering each rating method including the three color states (unchanged / clear / set).
4. `LumiverbKit/Sources/LumiverbKit/Views/RatingEditorView.swift` — heart toggle, 5-star row, 6-swatch color picker + clear button. Touch-friendly, no hover requirements.
5. Embed `RatingEditorView` in `LightboxView`'s metadata sidebar.
6. Add a context-menu "Rate selection" item to `MediaGridView` (macOS right-click for now; iOS long-press will work in M7 because `.contextMenu` handles both).
7. macOS keyboard shortcuts: `1`-`5` set stars, `0` clears stars, `F` toggles favorite. Gated with `#if os(macOS)`.
8. The lightbox fetches the current rating when opening an asset via `lookupRatings([assetId])` and caches it in local state. Mutations optimistically update local state and retry once on failure.

**Does NOT include:** Any iOS-specific polish. Ratings in the iOS grid context menu ship in M7.

**Read-ahead:** The grid's existing selection model must expose `selectedAssetIds` for batch ratings — verify this exists in `BrowseState` before starting. If not, add it as an M3 deliverable.

**Done when:**
- [ ] Both targets build
- [ ] New tests green
- [ ] Manual: on macOS, set stars/favorite/color on single and multiple assets, filter by each, verify persistence across app restart
- [ ] Milestone table updated

### Milestone 4 — Collections: CRUD

**Goal:** Create, list, view, rename, delete collections on macOS. Private by default. iOS gets the list in M6.

**Deliverables:**
1. `LumiverbKit/Sources/LumiverbKit/Models/Collection.swift` — `CollectionVisibility`, `CollectionSortOrder`, `Collection` struct.
2. `APIClient.swift` additions: `listCollections`, `getCollection`, `createCollection`, `updateCollection`, `deleteCollection`, `addAssetsToCollection`, `removeAssetsFromCollection`, `listCollectionAssets`, `reorderCollection`.
3. Tests in `APIClientNetworkTests.swift` for each method.
4. `LumiverbKit/Sources/LumiverbKit/State/CollectionsState.swift` — observable state holding the list, currently-open collection, loading states.
5. `LumiverbKit/Sources/LumiverbKit/Views/CollectionsListView.swift` — grouped list (Mine / Shared), "+" button opens create sheet.
6. `LumiverbKit/Sources/LumiverbKit/Views/CollectionDetailView.swift` — header with metadata + `MediaGridView` bound to `listCollectionAssets` pagination. Toolbar: rename, delete, share (share is a placeholder in M4, implemented in M5).
7. `LumiverbKit/Sources/LumiverbKit/Views/AddToCollectionSheet.swift` — multi-select existing collections, "New collection..." row, confirm button.
8. macOS integration: add a "Collections" section to `LibrarySidebar.swift`. Tapping a collection opens `CollectionDetailView` in the main content area.
9. Add "Add to collection..." menu item to the existing right-click menu on `MediaGridView` cells and the lightbox toolbar.

**Does NOT include:** Sharing (M5). Drag-to-reorder UI (future). Collection covers beyond what the server auto-resolves.

**Read-ahead:** M5 adds visibility toggles. The model already has `visibility` as a settable field, so no data model change is needed in M5 — only UI.

**Done when:**
- [ ] Both targets build
- [ ] New tests green
- [ ] Manual: on macOS, create a collection, add assets from browse + lightbox, remove assets, rename, delete
- [ ] Milestone table updated

### Milestone 5 — Collection sharing

**Goal:** Promote a private collection to shared (tenant) or public (link), copy the share link, revoke.

**Deliverables:**
1. `LumiverbKit/Sources/LumiverbKit/Views/ShareCollectionView.swift` — visibility picker (`Private` / `Shared with tenant` / `Public with link`), a "Copy link" action that's only enabled when `visibility == .public`.
2. Share link construction: `"\(serverBaseURL)/public/collections/\(collection.id)"`. Server base URL comes from the platform state object.
3. The share view presents as a sheet from `CollectionDetailView`'s share button.
4. macOS: "Copy link" uses `NSPasteboard.general.setString(_, forType: .string)`.
5. iOS: the button is wrapped in a `ShareLink` (`ShareLink(item: url)`) which produces the system share sheet. (iOS integration lands here even though iOS detail view isn't shown until M6 — the view itself builds correctly because it's cross-platform.)
6. Revoke confirmation: a `.confirmationDialog` before going from shared/public back to private.
7. Tests for the state transitions (no network tests for the clipboard/system share — those are platform-specific UX).

**Does NOT include:** Public collection browsing in the native client. The native clients copy share links but do not render public collections themselves; the web UI owns that experience.

**Read-ahead:** M6 exposes this via the iOS detail view.

**Done when:**
- [ ] Both targets build
- [ ] Manual: on macOS, create a private collection, toggle to shared, toggle to public, copy link, paste into a browser and verify the web UI resolves it, toggle back to private, verify the link no longer works
- [ ] Milestone table updated

### Milestone 5.5 — Date-grouped grid with multi-select

**Goal:** Match the web client's date-grouped browse grid with date-level and
item-level selection for bulk operations. The web client groups assets by date,
renders date headers with select-all checkboxes, and exposes a selection toolbar
for batch rating, adding to collections, etc. The macOS client currently has a
flat grid with no multi-select. This milestone closes that gap in LumiverbKit so
both macOS and iOS inherit it.

**Deliverables:**

1. `LumiverbKit/Sources/LumiverbKit/Models/DateGroup.swift` — `DateGroup` struct
   (`label: String`, `dateISO: String`, `assets: [AssetPageItem]`).
   `groupAssetsByDate(_:) -> [DateGroup]` groups by day using `takenAt` with
   `createdAt` fallback, sorted most-recent-first. Assets with neither date go
   into an "Unknown date" group at the end.

2. `BrowseState.swift` additions:
   - `@Published var selectedAssetIds: Set<String>` — multi-select set.
   - `@Published var isSelecting: Bool` — toggles between browse (tap → lightbox)
     and select (tap → toggle) modes.
   - `func toggleSelection(assetId:)` — add/remove individual.
   - `func selectGroup(dateISO:)` — toggle all assets in a date group (select all
     if any unselected, deselect all if all already selected).
   - `func selectAll()` / `func clearSelection()`.
   - Exiting select mode clears the selection.

3. `MediaGridView.swift` refactor:
   - Group `browseState.assets` into `[DateGroup]` at the top of the body.
   - Compute `MediaLayout` per section (one call per `DateGroup`).
   - Render date headers between sections: date label on left, checkbox + asset
     count on right. Header taps call `browseState.selectGroup(dateISO:)`.
   - Each cell shows a selection overlay (SF Symbol checkmark circle, bottom-left)
     when `isSelecting` is true. Selected cells get a highlighted border.
   - Tap behavior: in browse mode → lightbox (existing). In select mode → toggle
     selection. On macOS, Cmd+click enters select mode and toggles the item.
     Shift+click range-selects from the last toggled item.

4. `LumiverbKit/Sources/LumiverbKit/Views/SelectionToolbarView.swift` — appears
   above the grid when `selectedAssetIds` is non-empty.
   - Shows count ("12 selected") and a "Deselect All" button.
   - Inline `RatingEditorView` bound to a transient `Rating` that fires
     `APIClient.batchUpdateRatings` on change.
   - "Add to Collection..." button that opens `AddToCollectionSheet` with the
     selected IDs.
   - On macOS, keyboard shortcuts: Cmd+A selects all visible, Escape exits select
     mode and clears selection.

5. Apply the same date-grouping and selection to `SearchResultsGrid` and
   `SimilarResultsGrid` — they use the same justified-row pattern and should
   share the grouping logic. If the body refactor is too large, extract a shared
   `DateGroupedGrid` view that all three grids use.

6. Tests:
   - `DateGroupTests.swift` — grouping logic: mixed dates, nil dates, sort order,
     single-day, empty input.
   - `SelectionTests.swift` — toggle, selectGroup, selectAll, clearSelection,
     range-select logic.

**Does NOT include:** Drag-to-select (marque selection). Shift+click range select
is sufficient for now. Also does not include collapsible date sections (all
sections are always expanded).

**Read-ahead:** The web client's `groupByDate.ts` and `useSelection.ts` are the
reference implementations. The Swift grouping function mirrors `groupByDate.ts`
exactly (same date field priority, same "Unknown date" fallback, same sort order).

**Done when:**
- [ ] Both targets build
- [ ] New tests green
- [ ] Manual: on macOS, browse a library, see date headers with asset counts,
      click a date checkbox to select all in that date, Cmd+click individual items,
      use the toolbar to batch-rate and add to collection
- [ ] Date grouping works in search results too
- [ ] Milestone table updated

### Milestone 6 — iOS app shell

**Goal:** Replace `ConnectedView` with a real iOS `TabView` exposing Browse, Search, People, Collections, Settings. This is where the iOS app becomes usable.

**Deliverables:**
1. Delete `Sources/iOS/ConnectedView.swift`.
2. New `Sources/iOS/MainTabView.swift` — `TabView` with five tabs, each wrapped in its own `NavigationStack`.
3. `Sources/iOS/Browse/BrowseTabView.swift` — library picker (list of `appState.libraries`) → `MediaGridView` from LumiverbKit. Persists selected library in `UserDefaults` (key `io.lumiverb.app.lastLibraryId`).
4. `Sources/iOS/Browse/LightboxCover.swift` — presents `LightboxView` as a `.fullScreenCover` from the grid with swipe-down-to-dismiss.
5. `Sources/iOS/Search/SearchTabView.swift` — search field + `SearchResultsGrid`.
6. `Sources/iOS/People/PeopleTabView.swift` — `PeopleView` → `PersonDetailView` navigation stack.
7. `Sources/iOS/Collections/CollectionsTabView.swift` — `CollectionsListView` → `CollectionDetailView`.
8. `Sources/iOS/Settings/SettingsTabView.swift` — user info, server URL, logout, app version.
9. `iOSAppState.swift` — add `selectedLibraryId: String?` persisted in `UserDefaults`. Add `cacheBundle: CacheBundle` initialized at app launch with `MemoryImageCache(name: "ios.proxies")` + `IOSThumbnailDiskCache()`. Add `scrollAccessor: IOSScrollAccessor`.
10. `Sources/iOS/UIKitScrollAccessor.swift` — the `IOSScrollAccessor` class and `UIScrollViewIntrospector` from the Design section.
11. `LumiverbiOSApp.swift` — install `CacheBundle` and `IOSScrollAccessor` as environment values at the root; swap `ConnectedView` reference to `MainTabView`.
12. Source media fence: add `LumiverbKitTests/Views/SourceFencingTests.swift` that enumerates every `.swift` file under `LumiverbKit/Sources/LumiverbKit/Views/` and fails if any file contains any of `"/source"`, `"/download"`, or `"/original"` as a substring (see "Source media fence" in Design for full rationale and pattern set).

**Does NOT include:** Touch polish (M7), cellular policy (M8), iPad split-view.

**Read-ahead:** M7 will refine touch interactions — plan for it by avoiding hover-only or right-click-only affordances on iOS in this milestone.

**Done when:**
- [ ] iOS target launches in the simulator
- [ ] Login, browse a library, open lightbox, search, view people, view collections, open a collection, rate an asset, log out — all work on iOS
- [ ] Source fencing test green
- [ ] Milestone table updated

### Milestone 7 — iOS touch adaptations

**Goal:** Replace macOS-centric interactions (right-click, hover, keyboard shortcuts) with touch-first equivalents on iOS without regressing macOS.

**Deliverables:**
1. Audit every `.onHover` in `LumiverbKit/Sources/LumiverbKit/Views/`. Either remove (if decorative) or wrap in `#if os(macOS)` (if load-bearing).
2. Audit every right-click context menu. `.contextMenu { }` already works on iOS via long-press — verify each menu is reasonable on touch. Split any that are not (e.g. a menu with 15 items is unusable on a phone).
3. Face overlay: inflate hit regions to at least 44×44 points on iOS. Wrap the inflation in `#if os(iOS)`.
4. Cluster review swipe actions: add leading/trailing swipe actions on `.swipeActions` for "Dismiss" and "Assign to person" on iOS. Keep the existing macOS context menu.
5. Person picker sheet: when tagging a face on iOS, present a `.sheet` with a searchable list of existing persons and a "+ New person" row. macOS keeps its existing popover.
6. Manual verification: run through the M6 iOS app and make sure every interaction feels native on a touch device.

**Does NOT include:** Haptics (nice-to-have, defer). Pinch-zoom in the lightbox — SwiftUI's `ScrollView` handles this natively.

**Done when:**
- [ ] Both targets build
- [ ] Manual: end-to-end cluster management on iOS works — assign, rename, dismiss, create person
- [ ] macOS is not regressed
- [ ] Milestone table updated

### Milestone 8 — iOS cellular + Low Data Mode

**Goal:** Respect iOS network constraints. No behavior change on macOS.

**Deliverables:**
1. New file `Sources/iOS/NetworkMonitor.swift` — `NWPathMonitor`-backed observable that publishes `isConstrained` and `isCellular`.
2. Extend `iOSAppState` with `@Published var networkPolicy: NetworkPolicy` derived from the monitor.
3. Install `networkPolicy` as an environment value. Browse/search views read it and:
   - Skip viewport prefetch when policy is `.conservative` or `.minimal`
   - Skip lightbox neighbor preload when policy is `.conservative` or `.minimal`
   - Skip video autoplay when policy is `.minimal`
4. Settings tab: add a "Network" section with the current policy displayed, plus an "Always conservative" toggle (overrides the network-derived policy).
5. Manual verification with Network Link Conditioner or a cellular device.

**Done when:**
- [ ] iOS target builds
- [ ] Manual: toggling Low Data Mode in iOS Settings changes the behavior measurable by reduced network traffic in the proxy endpoint logs
- [ ] Milestone table updated

### Milestone 9 — Tests, docs, launch

**Goal:** Backfill anything that slipped, update docs, verify the full suite.

**Deliverables:**
1. New LumiverbKit tests for any untested additions — specifically `IOSThumbnailDiskCache` LRU eviction, `RatingEditorView` state transitions, `CollectionsState` list/create/delete flows, `NetworkMonitor` observable behavior.
2. Update `CLAUDE.md` "Finding things by topic" table — move browse-view rows from `Sources/macOS/` to `LumiverbKit/Sources/LumiverbKit/Views/`; add rows for ratings, collections, cache protocols, scroll accessor, iOS app shell.
3. Update `docs/cursor-api.md` — add ratings and collections endpoints to the client-facing API reference (if they're not already documented).
4. Update ADR-014 — mark Phase 5 complete and update Phase 6 status for iOS.
5. Mark this ADR's status as Done and the progress table all-complete.
6. Update `MEMORY.md` if any new project-level preferences emerged during the port (e.g. "iOS uses `cachesDirectory`, never `homeDirectoryForCurrentUser`").

**Done when:**
- [ ] `swift test` green
- [ ] `xcodebuild` green for both targets
- [ ] Docs updated
- [ ] ADR-014 and ADR-015 status tables updated
- [ ] Milestone table updated

### Milestone 10 — Trash view: visibility + selective undelete

**Goal:** Soft-deleted assets are currently invisible and irrecoverable from any
client. The scan pipeline (or a user action) can soft-delete hundreds of assets
in one pass — a runaway delete phase, a filter misconfiguration, or a
temporarily-offline volume can make assets disappear with no way to notice or
recover. This milestone adds a "Trash" view so users can see what was deleted,
when, and restore individual assets or bulk-undelete.

**Motivation (incident 2026-04-10):** The macOS scan pipeline's delete phase
batch-deleted 1500 assets that were still on disk. The scanner's
`fetchServerAssets` returned them, `discoverFiles` missed them (likely a transient
volume enumeration issue), and classification marked them as "deleted from disk."
The batch DELETE calls succeeded (200 OK), and the assets became invisible.
Subsequent scans re-discovered the files, re-ingested them (also 200 OK), but the
ingest endpoint updated the existing soft-deleted rows without clearing
`deleted_at`. A deletion safety gate and `deleted_at` clearing on re-ingest were
shipped as hotfixes, but the core data-trust problem remains: there is no way to
see or undo deletions from the client.

**Deliverables:**

1. **Server endpoint:** `GET /v1/assets/trash?library_id=...&after=...&limit=200`
   — paginated list of soft-deleted assets for a library, sorted by `deleted_at`
   descending (most recently deleted first). Response shape matches
   `AssetPageResponse` but queries `assets WHERE deleted_at IS NOT NULL` instead
   of `active_assets`.

2. **Server endpoint:** `POST /v1/assets/restore` — accepts `{"asset_ids": [...]}`
   and sets `deleted_at = NULL` on each. Returns `{"restored": N}`. Max 1000 IDs
   per call (same limit as batch ratings).

3. **Server endpoint:** `POST /v1/assets/hard-delete` — permanently removes
   soft-deleted assets (proxy, thumbnail, all child rows). Only operates on
   assets where `deleted_at IS NOT NULL`. This is the "empty trash" action.
   Requires `admin` role.

4. `LumiverbKit/Models/TrashedAsset.swift` — model for the trash list response.
   Includes `deletedAt` so the UI can show when each asset was deleted.

5. `APIClient.swift` additions: `listTrash`, `restoreAssets`, `hardDeleteAssets`.

6. `LumiverbKit/State/TrashState.swift` — observable state holding the trash
   list, loading flags, pagination.

7. `LumiverbKit/Views/TrashView.swift` — asset grid (reuse `MediaGridView`
   layout or a simpler list), with a banner showing count + date range. Each cell
   has a "Restore" context menu item. Toolbar has "Restore All" and "Empty Trash"
   (with confirmation dialog for both).

8. **macOS integration:** Add a "Trash" section to `LibrarySidebar.swift` with a
   badge showing the count of trashed assets. Tapping opens `TrashView` in the
   detail pane.

9. **Scan pipeline integration:** After the delete phase, if any assets were
   deleted, log the count at `.warning` level and set `ScanState.lastTrashCount`
   so the sidebar badge can surface it without an extra API call.

10. Tests for restore and hard-delete endpoints; model decoding tests.

**Does NOT include:** Automatic expiry (e.g., "permanently delete after 30 days").
That's a server-side policy decision for a future milestone. Also does not include
undo for user-initiated deletes from the web UI — that's a separate feature.

**Done when:**
- [ ] Both targets build
- [ ] New tests green
- [ ] Manual: on macOS, soft-delete an asset, see it in Trash view, restore it, verify it reappears in browse
- [ ] Manual: "Empty Trash" permanently removes assets
- [ ] Milestone table updated

## Alternatives Considered

**macOS-first, then port.** Build ratings and collections on macOS only, then port them to LumiverbKit later when iOS needs them. Rejected: creates two rounds of UI work, risks divergence, and we already know the macOS views need to move to LumiverbKit for Phase 5 anyway.

**iOS-only for the new features.** Build ratings and collections directly in `Sources/iOS/` and port them back to macOS later. Rejected: the macOS app is the daily driver and needs feature parity; and the user explicitly wants shared UI, not a fork.

**One cache impl with `#if os(iOS)` branches.** Keep `ProxyCacheOnDisk` as a single class and switch behavior internally. Rejected: the macOS and iOS behaviors are fundamentally different (macOS shares cache with CLI via SHA sidecars; iOS has no sidecars, hard cap, different paths). Two impls under one protocol is cleaner than one impl with scattered `#if` branches.

**Use `ScrollViewReader` for scroll commands instead of introspection.** Rejected: this was already tried in the macOS app. `ScrollViewReader.scrollTo` silently fails when targeting cells that have been disposed by `LazyVStack` — which is always the case for page-up/page-down across large grids.

**`ShareSheet` on macOS too.** Use `NSSharingServicePicker` for collection sharing on macOS. Rejected: overkill for a single "copy link" action. A button + `NSPasteboard` is simpler.

**Build full public-collection browsing in native clients.** Rejected for scope: the web UI owns public-collection rendering, and duplicating that in native clients means tracking the web UI's public-collection layout over time. Out of scope for this ADR.

## What This Does NOT Include

- Windows client work
- Web UI changes (except any follow-up needed to make public collection links land at a stable URL)
- iOS widgets, App Intents, Siri shortcuts
- iOS background refresh for collections
- iPad-specific layouts (iOS app uses SwiftUI adaptive layouts; no split-view sidebar)
- Face detection or embedding generation on iOS (read-only from server)
- Any new server endpoints — the server is complete for this feature set
- Ratings or collections UX in the web UI
- Offline mode beyond the thumbnail disk cache
- Pinch-to-zoom customization in the lightbox

## Deferred / Known Limitations

These are intentional limitations or follow-ups that are **not** open questions
— the design is decided, but the limitations should be visible to anyone
implementing or reviewing the work later.

- **iOS `toRow` is approximate.** `IOSScrollAccessor.apply(.toRow)` computes
  the target offset from `row * averageRowHeight`. Justified-row grids have
  variable row heights, so the user lands within ~1 row of the target. If
  search-hit jumping feels imprecise in M6 manual testing, the follow-up is
  to expose a per-row offset table from the grid layout and consult it in
  the accessor.

- **iOS thumbnail cache LRU is approximate.** Eviction is by
  `contentModificationDate`, with reads bumping mtime via
  `setAttributes`. This is "approximate-LRU" — true LRU would track per-key
  access timestamps in a sidecar index, which we deliberately don't for
  simplicity. Acceptable for a 200 MB cache holding tens of thousands of
  small files; revisit only if churn analysis shows useful files being
  evicted prematurely.

- **`UIScrollViewIntrospector` retries via `updateUIView`.** If a scroll
  view is replaced mid-flight (NavigationStack tab swap, LazyVStack
  recycling), the introspector picks up the new one on the next SwiftUI
  update cycle. If field testing in M6 reveals dropped scroll commands,
  switch to a strong reference held until explicit teardown.

- **Source-media access on iOS is fenced by a string-literal test.** The
  `SourceFencingTests` test in M6 fails the build if any view file under
  `LumiverbKit/Sources/LumiverbKit/Views/` references `/v1/assets/`
  followed by `/source` (or any constructed equivalent). This is a
  defense-in-depth check, not a substitute for code review. Reviewers
  should still confirm new image-fetch code paths.

- **Public collections are not rendered in native clients.** Native
  clients copy the share link (`{serverBaseURL}/public/collections/{id}`)
  but only the web UI knows how to display the public collection page
  (route at `src/ui/web/src/main.tsx:60`). If we later want native public
  collection rendering, that's a separate ADR.

- **No iPad-specific layout.** The iOS app uses SwiftUI's adaptive
  layouts; no NavigationSplitView or sidebar. iPad users get a slightly
  larger phone UX. A real iPad layout is a follow-up if there's demand.

- **Cluster review simultaneous edits.** Two clients tagging the same
  cluster at the same time is "last write wins" — there is no conflict
  resolution. Acceptable because the user typically operates one device
  at a time on cluster review.
