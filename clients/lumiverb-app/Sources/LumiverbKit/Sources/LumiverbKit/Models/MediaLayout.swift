import CoreGraphics
import Foundation

/// Justified-row layout for a list of items with known aspect ratios.
///
/// Conceptually identical to Google Photos' grid: rows have **uniform
/// height** but **variable item count**, items keep their natural aspect
/// ratio (no cropping), and the items in a row are scaled uniformly so
/// the row's total width matches the container.
///
/// **Last-row exception:** the final row is *not* stretched to fill the
/// container if it doesn't already overflow at natural size. Without
/// this, a single image left over at the end would balloon to fill the
/// entire container width — visually jarring and inconsistent with
/// every justified-gallery implementation users have seen elsewhere
/// (Google Photos, Flickr, masonry plugins, etc.).
///
/// **Aspect ratio of 0 or NaN** is treated as 1.0 (square fallback).
/// `AssetPageItem.aspectRatio` already returns 1.0 for missing
/// dimensions, but the layout helper guards anyway so any caller
/// passing raw values can't crash the layout.
public struct MediaLayout: Sendable, Equatable {
    /// Each row is a list of indices into the original `aspectRatios`
    /// array. Indices are preserved in input order — the layout never
    /// reorders items.
    public let rows: [[Int]]

    /// Per-item rendered frame. Indexed by the item's position in the
    /// original `aspectRatios` array. `frames[i].width` and
    /// `frames[i].height` are the exact pixel dimensions the cell
    /// should occupy after row scaling.
    public let frames: [CGSize]

    /// Per-item row index. Used by row-aware keyboard navigation
    /// (PgUp / PgDn / arrow up / arrow down) to find which row a given
    /// focus position belongs to.
    public let rowFor: [Int]

    /// Per-item column position within its row (0-based). Used by
    /// vertical keyboard nav to find the "same column" in the next /
    /// previous row.
    public let columnFor: [Int]

    public init(
        rows: [[Int]],
        frames: [CGSize],
        rowFor: [Int],
        columnFor: [Int]
    ) {
        self.rows = rows
        self.frames = frames
        self.rowFor = rowFor
        self.columnFor = columnFor
    }

    /// Pack `aspectRatios` into justified rows that fit `containerWidth`.
    ///
    /// - Parameters:
    ///   - aspectRatios: width/height ratio per item, in the order they
    ///     should be displayed. 0/NaN/negative values are treated as 1.0.
    ///   - containerWidth: pixel width available for each row. Must be
    ///     greater than 0; pass the width from a `GeometryReader`.
    ///   - targetRowHeight: the *aspirational* height for each row before
    ///     scaling. Final row heights will deviate from this — wider
    ///     rows scale down, narrower rows scale up — but the average
    ///     stays close. 180pt is a reasonable default for desktop
    ///     viewing; smaller values pack more items per screen.
    ///   - spacing: horizontal gap between cells in a row. Vertical gap
    ///     between rows is the caller's responsibility (LazyVStack
    ///     `spacing:`). Set both to the same value for a uniform grid.
    ///
    /// Empty `aspectRatios` returns an empty layout. `containerWidth ≤ 0`
    /// also returns an empty layout — there's nothing useful to compute
    /// before the view has been measured.
    public static func compute(
        aspectRatios: [Double],
        containerWidth: CGFloat,
        targetRowHeight: CGFloat,
        spacing: CGFloat
    ) -> MediaLayout {
        guard !aspectRatios.isEmpty, containerWidth > 0, targetRowHeight > 0 else {
            return MediaLayout(rows: [], frames: [], rowFor: [], columnFor: [])
        }

        let safeAspect: (Double) -> CGFloat = { ar in
            guard ar.isFinite, ar > 0 else { return 1.0 }
            return CGFloat(ar)
        }

        // Pack greedily: keep adding items until the next item would
        // overflow the container width, then close the row.
        var rows: [[Int]] = []
        var current: [Int] = []
        var currentNaturalWidth: CGFloat = 0

        for index in aspectRatios.indices {
            let naturalWidth = safeAspect(aspectRatios[index]) * targetRowHeight
            let withItem = currentNaturalWidth
                + naturalWidth
                + (current.isEmpty ? 0 : spacing)

            // A single item that overflows on its own gets its own row
            // (we don't subdivide a single image). Otherwise, close the
            // current row when adding this item would push us over.
            if !current.isEmpty && withItem > containerWidth {
                rows.append(current)
                current = [index]
                currentNaturalWidth = naturalWidth
            } else {
                current.append(index)
                currentNaturalWidth = withItem
            }
        }
        if !current.isEmpty {
            rows.append(current)
        }

        // Compute frames row by row.
        var frames = Array(repeating: CGSize.zero, count: aspectRatios.count)
        var rowFor = Array(repeating: 0, count: aspectRatios.count)
        var columnFor = Array(repeating: 0, count: aspectRatios.count)

        for (rowIdx, row) in rows.enumerated() {
            // Sum of natural widths at the target row height.
            let naturalSum: CGFloat = row.reduce(0) { acc, idx in
                acc + safeAspect(aspectRatios[idx]) * targetRowHeight
            }
            let totalSpacing = spacing * CGFloat(max(0, row.count - 1))
            let availableWidth = containerWidth - totalSpacing

            // Last-row rule: if the row's natural width fits inside the
            // container, leave it at natural size instead of stretching
            // it. This is what every justified gallery does — a partial
            // last row should look like a partial row, not a comically
            // oversized image.
            let isLastRow = rowIdx == rows.count - 1
            let naturalRowWidth = naturalSum + totalSpacing

            let rowHeight: CGFloat
            let scale: CGFloat
            if isLastRow && naturalRowWidth <= containerWidth {
                rowHeight = targetRowHeight
                scale = 1.0
            } else {
                // Scale so the row fills exactly. Guard against
                // pathological zero-sum (shouldn't happen because we
                // clamp aspect ratios to ≥ 1.0, but be safe).
                guard naturalSum > 0 else {
                    rowHeight = targetRowHeight
                    scale = 1.0
                    for (col, idx) in row.enumerated() {
                        frames[idx] = CGSize(width: targetRowHeight, height: targetRowHeight)
                        rowFor[idx] = rowIdx
                        columnFor[idx] = col
                    }
                    continue
                }
                scale = availableWidth / naturalSum
                rowHeight = targetRowHeight * scale
            }

            for (col, idx) in row.enumerated() {
                let naturalWidth = safeAspect(aspectRatios[idx]) * targetRowHeight
                let scaledWidth = naturalWidth * scale
                frames[idx] = CGSize(width: scaledWidth, height: rowHeight)
                rowFor[idx] = rowIdx
                columnFor[idx] = col
            }
        }

        return MediaLayout(
            rows: rows,
            frames: frames,
            rowFor: rowFor,
            columnFor: columnFor
        )
    }

    // MARK: - Row-aware keyboard navigation helpers

    /// Index of the item directly above `index`, preferring the same
    /// column position. Returns `nil` if `index` is on the first row or
    /// out of range. The "same column" preference saturates at the
    /// previous row's last column when the previous row is shorter.
    public func indexAbove(_ index: Int) -> Int? {
        guard rows.indices.contains(rowFor[safe: index] ?? -1) else { return nil }
        let row = rowFor[index]
        guard row > 0 else { return nil }
        let col = columnFor[index]
        let prevRow = rows[row - 1]
        let targetCol = min(col, prevRow.count - 1)
        return prevRow[targetCol]
    }

    /// Index of the item directly below `index`, preferring the same
    /// column position. Returns `nil` if `index` is on the last row or
    /// out of range.
    public func indexBelow(_ index: Int) -> Int? {
        guard rows.indices.contains(rowFor[safe: index] ?? -1) else { return nil }
        let row = rowFor[index]
        guard row < rows.count - 1 else { return nil }
        let col = columnFor[index]
        let nextRow = rows[row + 1]
        let targetCol = min(col, nextRow.count - 1)
        return nextRow[targetCol]
    }

    /// Index of the item N rows above (or below if `rowDelta` is
    /// positive). Used by PgUp/PgDn — pass the number of visible rows.
    /// Saturates at the first/last row instead of returning `nil`.
    public func indexAtRowOffset(_ index: Int, rowDelta: Int) -> Int? {
        guard rowFor.indices.contains(index) else { return nil }
        let row = rowFor[index]
        let target = max(0, min(rows.count - 1, row + rowDelta))
        if target == row { return nil }
        let col = columnFor[index]
        let targetRow = rows[target]
        let targetCol = min(col, targetRow.count - 1)
        return targetRow[targetCol]
    }
}

// MARK: - Safe array subscript

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
