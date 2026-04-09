import SwiftUI
import LumiverbKit
import AppKit

/// Per-library settings modal.
///
/// Three sections in one sheet:
///   1. Name                — text field + Save
///   2. Root Path           — read-only label + Change… (NSOpenPanel)
///   3. Path Filters        — include & exclude lists with inline
///                            add/delete and a preview-and-confirm
///                            flow for exclude patterns that would
///                            trash existing assets
///
/// Keeps its own edit buffers + in-flight state per section so the user
/// can edit Name without blocking a filter mutation mid-sheet, and so
/// server errors stay visible until the next attempt.
struct LibrarySettingsSheet: View {
    @ObservedObject var appState: AppState
    let library: Library

    /// Called when the sheet dismisses after changes that callers may
    /// need to react to (re-fetch directory tree after a root change,
    /// re-render the sidebar, etc.). `true` means "something was saved".
    let onDismiss: (Bool) -> Void

    @Environment(\.dismiss) private var dismiss

    // Name section
    @State private var name: String = ""
    @State private var isSavingName = false
    @State private var nameError: String?

    // Root section
    @State private var rootPath: String = ""
    @State private var isSavingRoot = false
    @State private var rootError: String?
    @State private var showRootChangeWarning = false
    @State private var pendingRootPath: String?

    // Filter section
    @State private var filters: LibraryFiltersResponse?
    @State private var filtersError: String?
    @State private var isLoadingFilters = false

    @State private var includePattern = ""
    @State private var excludePattern = ""
    @State private var isAddingInclude = false
    @State private var isAddingExclude = false
    @State private var includeError: String?
    @State private var excludeError: String?
    @State private var pendingDeleteFilterId: String?
    @State private var isDeletingFilter = false

    /// "This exclude pattern matches N existing assets — confirm to trash."
    @State private var trashConfirm: TrashConfirmState?

    /// Tracks whether anything was actually saved so we can tell the
    /// caller on dismiss whether a refresh is needed.
    @State private var anythingChanged = false

    private struct TrashConfirmState: Equatable {
        let pattern: String
        let count: Int
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    nameSection
                    Divider()
                    rootSection
                    Divider()
                    filtersSection
                }
                .padding(20)
            }

            Divider()
            footer
        }
        .frame(minWidth: 520, idealWidth: 560, minHeight: 520, idealHeight: 640)
        .onAppear {
            name = library.name
            rootPath = library.rootPath
            Task { await loadFilters() }
        }
    }

    // MARK: - Header / Footer

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Library Settings")
                    .font(.headline)
                Text(library.name)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var footer: some View {
        HStack {
            Spacer()
            Button("Done") {
                onDismiss(anythingChanged)
                dismiss()
            }
            .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    // MARK: - Name

    private var nameSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Name")
                .font(.subheadline.bold())
            HStack {
                TextField("", text: $name)
                    .textFieldStyle(.roundedBorder)
                    .disabled(isSavingName)
                Button(isSavingName ? "Saving…" : "Save") {
                    Task { await saveName() }
                }
                .disabled(
                    isSavingName
                    || name.trimmingCharacters(in: .whitespaces).isEmpty
                    || name == library.name
                )
            }
            if let nameError {
                Text(nameError)
                    .font(.caption)
                    .foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func saveName() async {
        nameError = nil
        isSavingName = true
        defer { isSavingName = false }

        let trimmed = name.trimmingCharacters(in: .whitespaces)
        do {
            _ = try await appState.updateLibrary(libraryId: library.libraryId, name: trimmed)
            anythingChanged = true
        } catch {
            nameError = errorMessage(from: error)
        }
    }

    // MARK: - Root path

    private var rootSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Root Folder")
                .font(.subheadline.bold())
            Text(rootPath)
                .font(.system(.body, design: .monospaced))
                .textSelection(.enabled)
                .foregroundColor(.secondary)
                .lineLimit(2)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color.secondary.opacity(0.08))
                .cornerRadius(6)

            HStack {
                Button("Change…") { chooseNewRoot() }
                    .disabled(isSavingRoot)
                Button("Reveal in Finder") {
                    NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: rootPath)
                }
                Spacer()
                if isSavingRoot {
                    ProgressView().controlSize(.small)
                }
            }

            if showRootChangeWarning, let pending = pendingRootPath {
                warningBox(
                    title: "Change root folder?",
                    message: """
                    Lumiverb will index assets under \(pending) on the next scan. Files already \
                    indexed under the old path will appear as missing until you move them or \
                    re-scan the new location.
                    """,
                    confirmLabel: "Change Root",
                    destructive: false,
                    onConfirm: { Task { await saveRoot(pending) } },
                    onCancel: {
                        pendingRootPath = nil
                        showRootChangeWarning = false
                    }
                )
            }

            if let rootError {
                Text(rootError)
                    .font(.caption)
                    .foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func chooseNewRoot() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        panel.message = "Select the new root folder for this library."
        panel.directoryURL = URL(fileURLWithPath: rootPath)
        if panel.runModal() == .OK, let url = panel.url {
            if url.path == rootPath { return }
            pendingRootPath = url.path
            showRootChangeWarning = true
        }
    }

    private func saveRoot(_ newPath: String) async {
        rootError = nil
        isSavingRoot = true
        showRootChangeWarning = false
        defer { isSavingRoot = false }

        do {
            _ = try await appState.updateLibrary(
                libraryId: library.libraryId,
                rootPath: newPath
            )
            rootPath = newPath
            pendingRootPath = nil
            anythingChanged = true
        } catch {
            rootError = errorMessage(from: error)
        }
    }

    // MARK: - Filters

    private var filtersSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Path Filters")
                    .font(.subheadline.bold())
                Spacer()
                if isLoadingFilters {
                    ProgressView().controlSize(.small)
                }
            }
            Text("""
            Control which paths are indexed. Includes narrow the scan to \
            matching paths only; excludes prune results after includes apply. \
            Glob syntax (e.g. **/Proxy/**).
            """)
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if let filtersError {
                Text(filtersError)
                    .font(.caption)
                    .foregroundColor(.red)
            }

            filterSubsection(
                title: "Include patterns",
                empty: "No include patterns. All paths are included by default.",
                items: filters?.includes ?? [],
                pattern: $includePattern,
                isAdding: isAddingInclude,
                errorMessage: includeError,
                onAdd: { Task { await addInclude() } },
                isExclude: false
            )

            filterSubsection(
                title: "Exclude patterns",
                empty: "No exclude patterns.",
                items: filters?.excludes ?? [],
                pattern: $excludePattern,
                isAdding: isAddingExclude,
                errorMessage: excludeError,
                onAdd: { Task { await addExcludeOrPreview() } },
                isExclude: true
            )

            if let confirm = trashConfirm {
                warningBox(
                    title: "Trash existing assets?",
                    message: "This will trash \(confirm.count.formatted()) existing \(confirm.count == 1 ? "asset" : "assets") matching \(confirm.pattern) and prevent future ingestion under this pattern.",
                    confirmLabel: "Trash \(confirm.count.formatted())",
                    destructive: true,
                    onConfirm: { Task { await confirmExcludeTrash(confirm) } },
                    onCancel: { trashConfirm = nil }
                )
            }
        }
    }

    @ViewBuilder
    private func filterSubsection(
        title: String,
        empty: String,
        items: [FilterItem],
        pattern: Binding<String>,
        isAdding: Bool,
        errorMessage: String?,
        onAdd: @escaping () -> Void,
        isExclude: Bool
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.bold())
                .foregroundColor(.secondary)

            if items.isEmpty {
                Text(empty)
                    .font(.caption)
                    .italic()
                    .foregroundColor(.secondary)
                    .padding(.vertical, 4)
            } else {
                VStack(spacing: 4) {
                    ForEach(items, id: \.filterId) { item in
                        filterRow(item: item)
                    }
                }
            }

            HStack {
                TextField("e.g. **/Proxy/**", text: pattern)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.body, design: .monospaced))
                    .disabled(isAdding)
                    .onSubmit(onAdd)
                Button(isAdding ? "Adding…" : "Add") {
                    onAdd()
                }
                .disabled(
                    isAdding
                    || pattern.wrappedValue.trimmingCharacters(in: .whitespaces).isEmpty
                )
            }

            if let errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private func filterRow(item: FilterItem) -> some View {
        HStack(spacing: 8) {
            Text(item.pattern)
                .font(.system(.caption, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)

            if pendingDeleteFilterId == item.filterId {
                Button("Confirm") {
                    if let fid = item.filterId {
                        Task { await deleteFilter(filterId: fid) }
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .controlSize(.small)
                .disabled(isDeletingFilter)

                Button("Cancel") { pendingDeleteFilterId = nil }
                    .controlSize(.small)
                    .disabled(isDeletingFilter)
            } else {
                Button("Delete") {
                    pendingDeleteFilterId = item.filterId
                }
                .buttonStyle(.borderless)
                .foregroundColor(.red)
                .controlSize(.small)
                // A filter row without a filterId would be a server shape
                // regression — disable delete rather than silently no-op.
                .disabled(item.filterId == nil)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Color.secondary.opacity(0.06))
        .cornerRadius(4)
    }

    private func loadFilters() async {
        filtersError = nil
        isLoadingFilters = true
        defer { isLoadingFilters = false }
        do {
            filters = try await appState.listLibraryFilters(libraryId: library.libraryId)
        } catch {
            filtersError = errorMessage(from: error)
        }
    }

    private func addInclude() async {
        let trimmed = includePattern.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        includeError = nil
        isAddingInclude = true
        defer { isAddingInclude = false }
        do {
            _ = try await appState.addLibraryFilter(
                libraryId: library.libraryId,
                type: "include",
                pattern: trimmed
            )
            includePattern = ""
            anythingChanged = true
            await loadFilters()
        } catch {
            includeError = errorMessage(from: error)
        }
    }

    private func addExcludeOrPreview() async {
        let trimmed = excludePattern.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        excludeError = nil
        isAddingExclude = true
        defer { isAddingExclude = false }

        // Preview first — if it matches existing assets, require confirmation
        // before calling add() with trash_matching=true. This mirrors the web
        // UI and avoids silently trashing user content.
        do {
            let preview = try await appState.previewLibraryFilter(
                libraryId: library.libraryId,
                type: "exclude",
                pattern: trimmed
            )
            if preview.matchingAssetCount > 0 {
                trashConfirm = TrashConfirmState(
                    pattern: trimmed,
                    count: preview.matchingAssetCount
                )
                return
            }
        } catch {
            // Preview failed (usually invalid pattern) — surface the error
            // without proceeding to add. The add() call would fail with the
            // same 400 anyway, but this is clearer about what went wrong.
            excludeError = errorMessage(from: error)
            return
        }

        // Zero-match excludes can be applied immediately.
        do {
            _ = try await appState.addLibraryFilter(
                libraryId: library.libraryId,
                type: "exclude",
                pattern: trimmed,
                trashMatching: false
            )
            excludePattern = ""
            anythingChanged = true
            await loadFilters()
        } catch {
            excludeError = errorMessage(from: error)
        }
    }

    private func confirmExcludeTrash(_ confirm: TrashConfirmState) async {
        excludeError = nil
        isAddingExclude = true
        trashConfirm = nil
        defer { isAddingExclude = false }
        do {
            _ = try await appState.addLibraryFilter(
                libraryId: library.libraryId,
                type: "exclude",
                pattern: confirm.pattern,
                trashMatching: true
            )
            excludePattern = ""
            anythingChanged = true
            await loadFilters()
        } catch {
            excludeError = errorMessage(from: error)
        }
    }

    private func deleteFilter(filterId: String) async {
        isDeletingFilter = true
        defer {
            isDeletingFilter = false
            pendingDeleteFilterId = nil
        }
        do {
            try await appState.deleteLibraryFilter(
                libraryId: library.libraryId,
                filterId: filterId
            )
            anythingChanged = true
            await loadFilters()
        } catch {
            filtersError = errorMessage(from: error)
        }
    }

    // MARK: - Helpers

    @ViewBuilder
    private func warningBox(
        title: String,
        message: String,
        confirmLabel: String,
        destructive: Bool,
        onConfirm: @escaping () -> Void,
        onCancel: @escaping () -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.caption.bold())
                .foregroundColor(destructive ? .red : .primary)
            Text(message)
                .font(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button(confirmLabel, action: onConfirm)
                    .buttonStyle(.borderedProminent)
                    .tint(destructive ? .red : .accentColor)
                    .controlSize(.small)
                Button("Cancel", action: onCancel)
                    .controlSize(.small)
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill((destructive ? Color.red : Color.accentColor).opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke((destructive ? Color.red : Color.accentColor).opacity(0.3))
        )
    }

    private func errorMessage(from error: Error) -> String {
        if let api = error as? APIError {
            switch api {
            case .serverError(_, let message): return message
            case .networkError(let message): return "Network error: \(message)"
            case .unauthorized(let message): return "Unauthorized: \(message)"
            case .noToken: return "Not signed in."
            case .decodingError(let message): return "Decoding error: \(message)"
            }
        }
        return error.localizedDescription
    }
}
