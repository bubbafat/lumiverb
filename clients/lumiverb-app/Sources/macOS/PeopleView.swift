import SwiftUI
import LumiverbKit

/// People browse panel — a grid of named people sorted by face count
/// descending (server side). Phase 6 M3 of ADR-014.
///
/// Read-only in M3: clicking a card pushes a `PersonDetailView`; rename /
/// merge / delete land in M6. Wraps the grid + detail in a `NavigationStack`
/// so SwiftUI handles back navigation and animation.
struct PeopleView: View {
    @ObservedObject var peopleState: PeopleState
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    /// Four columns of large-ish circular avatars; matches roughly the
    /// density of the existing media grid (4 cols) for visual rhythm.
    private let columns = Array(
        repeating: GridItem(.flexible(), spacing: 16),
        count: 4
    )

    var body: some View {
        NavigationStack {
            ScrollView {
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
            .navigationTitle("People")
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
        }
        .task {
            await peopleState.loadIfNeeded()
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "person.2.slash")
                .font(.system(size: 40))
                .foregroundColor(.secondary)
            Text("No named people yet")
                .font(.title3)
                .foregroundColor(.secondary)
            Text("Use the lightbox face overlay to assign people to faces.")
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

    var body: some View {
        VStack(spacing: 6) {
            FaceThumbnailView(faceId: person.representativeFaceId, client: client)
                .frame(width: 120, height: 120)
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
