import SwiftUI

/// People browse panel — a grid of named people sorted by face count
/// descending (server side). Phase 6 M3 of ADR-014.
///
/// Read-only in M3: clicking a card pushes a `PersonDetailView`; rename /
/// merge / delete land in M6. Wraps the grid + detail in a `NavigationStack`
/// so SwiftUI handles back navigation and animation.
public struct PeopleView: View {
    @ObservedObject public var peopleState: PeopleState
    @ObservedObject public var browseState: BrowseState
    public let client: APIClient?

    /// Optional cluster review state. When provided, the People view
    /// renders a "Review unnamed faces" entry at the top that pushes
    /// `ClusterReviewView` via the navigation stack. macOS doesn't pass
    /// this — its sidebar already has a separate cluster-review section.
    public let clusterReviewState: ClusterReviewState?

    public init(
        peopleState: PeopleState,
        browseState: BrowseState,
        client: APIClient?,
        clusterReviewState: ClusterReviewState? = nil
    ) {
        self.peopleState = peopleState
        self.browseState = browseState
        self.client = client
        self.clusterReviewState = clusterReviewState
    }

    /// Four columns of circular avatars. iOS uses tighter spacing to
    /// fit 72pt circles; macOS uses wider spacing for 120pt circles.
    #if os(iOS)
    private let columns = Array(
        repeating: GridItem(.flexible(), spacing: 8),
        count: 4
    )
    #else
    private let columns = Array(
        repeating: GridItem(.flexible(), spacing: 16),
        count: 4
    )
    #endif

    public var body: some View {
        NavigationStack {
            ScrollView {
                if clusterReviewState != nil {
                    reviewEntryPoint
                        .padding(.horizontal, 20)
                        .padding(.top, 12)
                }

                modePicker
                    .padding(.horizontal, 20)
                    .padding(.top, 12)

                if peopleState.people.isEmpty && !peopleState.isLoadingPeople {
                    emptyState
                } else {
                    LazyVGrid(columns: columns, spacing: 20) {
                        ForEach(peopleState.people) { person in
                            NavigationLink(value: person) {
                                PersonCardView(person: person, client: client)
                            }
                            .buttonStyle(.plain)
                            .onAppear {
                                // Infinite scroll: trigger another page when
                                // the user has scrolled near the end.
                                if let last = peopleState.people.last,
                                   last.personId == person.personId {
                                    Task { await peopleState.loadNextPage() }
                                }
                            }
                        }
                    }
                    .padding(20)
                }

                if peopleState.isLoadingPeople {
                    ProgressView()
                        .padding()
                }

                if let error = peopleState.peopleError {
                    Text(error)
                        .foregroundColor(.red)
                        .font(.caption)
                        .padding()
                }
            }
            .navigationTitle(peopleState.mode == .active ? "People" : "Dismissed People")
            .navigationDestination(for: PersonItem.self) { person in
                PersonDetailView(
                    person: person,
                    peopleState: peopleState,
                    browseState: browseState,
                    client: client
                )
                .onAppear {
                    // Drive PeopleState's per-person state from navigation
                    // pushes so the back button (which is a NavigationStack
                    // pop, not a clearSelection() call) still resets it.
                    if peopleState.selectedPerson?.personId != person.personId {
                        peopleState.selectPerson(person)
                    }
                }
                .onDisappear {
                    peopleState.clearSelection()
                }
            }
            .navigationDestination(for: ClusterReviewDestination.self) { _ in
                if let clusterReviewState {
                    ClusterReviewView(
                        state: clusterReviewState,
                        browseState: browseState,
                        client: client
                    )
                    .navigationTitle("Review Faces")
                }
            }
        }
        // Use an unstructured Task in onAppear (rather than `.task`) so
        // the initial fetch survives a transient view teardown — for
        // example, a library autoload race that briefly flips the
        // sidebar section back to .library mid-request. SwiftUI's
        // `.task` modifier cancels its work when the view disappears,
        // which would surface as a `cancelled` URL error.
        .onAppear {
            Task { await peopleState.loadIfNeeded() }
        }
    }

    /// "Review unnamed faces" entry point shown above the people grid.
    /// NavigationLink uses the `ClusterReviewDestination` marker type
    /// matched by the `.navigationDestination` modifier on the
    /// surrounding ScrollView.
    private var reviewEntryPoint: some View {
        NavigationLink(value: ClusterReviewDestination.shared) {
            HStack(spacing: 12) {
                Image(systemName: "person.crop.rectangle.stack")
                    .font(.title2)
                    .foregroundColor(.accentColor)
                    .frame(width: 40, height: 40)
                    .background(Color.accentColor.opacity(0.15))
                    .clipShape(Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text("Review unnamed faces")
                        .font(.subheadline.weight(.medium))
                        .foregroundColor(.primary)
                    Text("Tap to name new people in your library")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundColor(.secondary)
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color.gray.opacity(0.1))
            )
        }
        .buttonStyle(.plain)
    }

    /// Segmented control switching between active and dismissed lists.
    /// Wraps `peopleState.setMode(_:)` so changing the segment also
    /// resets pagination + kicks a fresh fetch in one place.
    private var modePicker: some View {
        Picker("List", selection: Binding(
            get: { peopleState.mode },
            set: { peopleState.setMode($0) }
        )) {
            Text("Active").tag(PeopleListMode.active)
            Text("Dismissed").tag(PeopleListMode.dismissed)
        }
        .pickerStyle(.segmented)
        .frame(maxWidth: 320)
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: peopleState.mode == .active
                  ? "person.2.slash"
                  : "person.crop.circle.badge.checkmark")
                .font(.system(size: 40))
                .foregroundColor(.secondary)
            Text(peopleState.mode == .active
                 ? "No named people yet"
                 : "No dismissed people")
                .font(.title3)
                .foregroundColor(.secondary)
            Text(peopleState.mode == .active
                 ? "Use the lightbox face overlay to assign people to faces."
                 : "Dismissed clusters will appear here so you can restore them.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.top, 80)
    }
}

/// Single person card: circular representative-face thumbnail, name, and
/// face count. Falls back to the SF Symbol "person" silhouette inside
/// `FaceThumbnailView` if there's no representative face id (which only
/// happens for empty/just-created people in this read-only view).
struct PersonCardView: View {
    let person: PersonItem
    let client: APIClient?

    #if os(iOS)
    private let faceSize: CGFloat = 72
    #else
    private let faceSize: CGFloat = 120
    #endif

    var body: some View {
        VStack(spacing: 6) {
            FaceThumbnailView(faceId: person.representativeFaceId, client: client)
                .frame(width: faceSize, height: faceSize)
                .background(Circle().fill(Color.gray.opacity(0.15)))
                .clipShape(Circle())
                .overlay(
                    Circle().stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                )

            Text(person.displayName)
                .font(.callout)
                .lineLimit(1)
                .truncationMode(.tail)

            Text("\(person.faceCount) photo\(person.faceCount == 1 ? "" : "s")")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity)
        .contentShape(Rectangle())
    }
}

/// Marker type for the cluster review navigation destination. Singleton
/// because there's only one cluster review screen — using a struct
/// (rather than a Bool flag) lets us hang it off the navigation stack
/// the same way `PersonItem` does, with a clean back button.
public struct ClusterReviewDestination: Hashable, Sendable {
    public static let shared = ClusterReviewDestination()
    private init() {}
}
