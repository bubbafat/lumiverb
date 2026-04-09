import SwiftUI
import LumiverbKit

/// Expandable directory tree shown in the sidebar under the selected library.
struct DirectoryTreeView: View {
    @ObservedObject var browseState: BrowseState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // "All Items" row to clear path filter
            allItemsRow

            // Root directories
            ForEach(browseState.directories) { node in
                DirectoryRowView(
                    node: node,
                    depth: 0,
                    browseState: browseState
                )
            }
        }
    }

    private var allItemsRow: some View {
        Button {
            browseState.selectPath(nil)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "photo.on.rectangle.angled")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .frame(width: 16)
                Text("All Items")
                    .font(.callout)
                Spacer()
            }
            .padding(.vertical, 3)
            .padding(.horizontal, 8)
            .background(
                browseState.selectedPath == nil
                    ? Color.accentColor.opacity(0.15)
                    : Color.clear
            )
            .cornerRadius(4)
        }
        .buttonStyle(.plain)
    }
}

/// A single row in the directory tree, with expand/collapse and children.
struct DirectoryRowView: View {
    let node: DirectoryNode
    let depth: Int
    @ObservedObject var browseState: BrowseState

    private var isExpanded: Bool {
        browseState.expandedPaths.contains(node.path)
    }

    private var isSelected: Bool {
        browseState.selectedPath == node.path
    }

    private var children: [DirectoryNode]? {
        browseState.childDirectories[node.path]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // This row
            Button {
                browseState.selectPath(node.path)
            } label: {
                HStack(spacing: 4) {
                    // Expand/collapse chevron
                    Button {
                        browseState.toggleExpanded(path: node.path)
                    } label: {
                        Image(systemName: "chevron.right")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                            .rotationEffect(isExpanded ? .degrees(90) : .zero)
                            .animation(.easeInOut(duration: 0.15), value: isExpanded)
                            .frame(width: 12)
                    }
                    .buttonStyle(.plain)

                    Image(systemName: "folder")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .frame(width: 16)

                    Text(node.name)
                        .font(.callout)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Spacer()

                    Text("\(node.assetCount)")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.12))
                        .cornerRadius(4)
                }
                .padding(.vertical, 3)
                .padding(.leading, CGFloat(8 + depth * 16))
                .padding(.trailing, 8)
                .background(
                    isSelected
                        ? Color.accentColor.opacity(0.15)
                        : Color.clear
                )
                .cornerRadius(4)
                .contextMenu {
                    if let rootPath = browseState.selectedLibraryRootPath {
                        Button("Open Source Location") {
                            let fullPath = (rootPath as NSString).appendingPathComponent(node.path)
                            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: fullPath)
                        }
                        Divider()
                    }
                    ReEnrichMenu(
                        onReEnrich: { ops in
                            browseState.reEnrich(operations: ops, pathPrefix: node.path)
                        },
                        whisperEnabled: browseState.whisperEnabled,
                    )
                }
            }
            .buttonStyle(.plain)

            // Children (if expanded)
            if isExpanded {
                if let children, !children.isEmpty {
                    ForEach(children) { child in
                        DirectoryRowView(
                            node: child,
                            depth: depth + 1,
                            browseState: browseState
                        )
                    }
                }
            }
        }
    }
}
