import XCTest
@testable import LumiverbKit
import CoreGraphics

final class MediaLayoutTests: XCTestCase {

    // MARK: - Edge cases

    func testEmptyInputProducesEmptyLayout() {
        let layout = MediaLayout.compute(
            aspectRatios: [],
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertEqual(layout.rows.count, 0)
        XCTAssertEqual(layout.frames.count, 0)
    }

    func testZeroContainerWidthProducesEmptyLayout() {
        // Pre-measurement state: GeometryReader hasn't reported a width
        // yet. Layout should gracefully return nothing rather than
        // crashing on division by zero.
        let layout = MediaLayout.compute(
            aspectRatios: [1.5, 0.66, 1.0],
            containerWidth: 0,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertEqual(layout.rows.count, 0)
    }

    func testZeroTargetHeightProducesEmptyLayout() {
        let layout = MediaLayout.compute(
            aspectRatios: [1.0],
            containerWidth: 800,
            targetRowHeight: 0,
            spacing: 4
        )
        XCTAssertEqual(layout.rows.count, 0)
    }

    func testNonFiniteAspectRatiosFallBackToSquare() {
        // NaN, infinity, zero, negative — all should be treated as 1.0.
        let layout = MediaLayout.compute(
            aspectRatios: [.nan, .infinity, 0, -1, 1.5],
            containerWidth: 1000,
            targetRowHeight: 100,
            spacing: 0
        )
        XCTAssertFalse(layout.frames.isEmpty)
        // Items 0..3 should all have the same width (square at scale).
        // Item 4 (1.5 aspect) should be wider.
        XCTAssertEqual(layout.frames[0].width, layout.frames[1].width, accuracy: 0.001)
        XCTAssertEqual(layout.frames[0].width, layout.frames[2].width, accuracy: 0.001)
        XCTAssertEqual(layout.frames[0].width, layout.frames[3].width, accuracy: 0.001)
        XCTAssertGreaterThan(layout.frames[4].width, layout.frames[0].width)
    }

    // MARK: - Single-row packing

    func testSingleItemFitsInOneRow() {
        let layout = MediaLayout.compute(
            aspectRatios: [1.5],
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertEqual(layout.rows.count, 1)
        XCTAssertEqual(layout.rows[0], [0])
        // Last row + fits naturally → no scaling, height stays at target.
        XCTAssertEqual(layout.frames[0].height, 180, accuracy: 0.001)
        XCTAssertEqual(layout.frames[0].width, 270, accuracy: 0.001)  // 1.5 * 180
    }

    func testGreedyPackingClosesRowOnOverflow() {
        // 5 squares at 180×180 with 10 spacing in a 740 container.
        // Natural per-row math: 3 items = 540 + 20 spacing = 560 (fits),
        // 4 items = 720 + 30 spacing = 750 (overflows). Greedy packer
        // closes the row at 3 items, starts new row with the 4th.
        let layout = MediaLayout.compute(
            aspectRatios: [1, 1, 1, 1, 1],
            containerWidth: 740,
            targetRowHeight: 180,
            spacing: 10
        )
        XCTAssertEqual(layout.rows.count, 2)
        XCTAssertEqual(layout.rows[0].count, 3, "First row should pack 3 squares")
        XCTAssertEqual(layout.rows[1].count, 2)
    }

    func testRowOverflowStartsNewRow() {
        // Three items each ~300 wide at 180 tall in a 700-wide container
        // → first row holds 2 (600 + 4 spacing = 604), second row gets the 3rd.
        let layout = MediaLayout.compute(
            aspectRatios: [1.667, 1.667, 1.667],  // 16:9-ish
            containerWidth: 700,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertEqual(layout.rows.count, 2)
        XCTAssertEqual(layout.rows[0].count, 2)
        XCTAssertEqual(layout.rows[1].count, 1)
    }

    // MARK: - Last-row exception

    func testPartialLastRowDoesNotStretch() {
        // First row packs 2 items, leftover single item on row 2.
        // The leftover should KEEP its natural height (180) rather than
        // getting scaled up to fill the whole container width.
        let layout = MediaLayout.compute(
            aspectRatios: [1.0, 1.0, 1.5],
            containerWidth: 600,
            targetRowHeight: 180,
            spacing: 0
        )
        XCTAssertEqual(layout.rows.count, 2)
        XCTAssertEqual(layout.rows[1].count, 1)
        XCTAssertEqual(layout.frames[2].height, 180, accuracy: 0.001,
                       "Partial last row must not be scaled up")
        XCTAssertEqual(layout.frames[2].width, 270, accuracy: 0.001)  // 1.5 * 180
    }

    func testLastRowThatExactlyFillsKeepsTargetHeight() {
        // A "perfect-fit" partial last row (natural width == container)
        // is still treated as last-row → no stretching, height stays
        // at target. Symmetric with the partial case below it.
        let layout = MediaLayout.compute(
            aspectRatios: [2.0],  // 360 wide at 180 tall
            containerWidth: 360,
            targetRowHeight: 180,
            spacing: 0
        )
        XCTAssertEqual(layout.rows.count, 1)
        XCTAssertEqual(layout.frames[0].height, 180, accuracy: 0.001)
        XCTAssertEqual(layout.frames[0].width, 360, accuracy: 0.001)
    }

    func testFullRowThatOverflowsScalesDownToFit() {
        // Two items each ~600 wide at 180 tall (3.33 aspect each), in a
        // 1000 container. They fit greedy-wise (item 1 = 600, +600+0 =
        // 1200 > 1000 → close at 1 item, then item 2 in row 2). Pick a
        // smaller pair so they fit in one row but overflow naturally.
        // Two 1.5 aspect items: 270 + 0 + 270 = 540 fits in 600 → only
        // 1 row, last row, 540 < 600 → no scaling. Need to push more
        // items in.
        // Use 4 items at 1.5 aspect, container 600, spacing 0: each
        // 270 wide. Greedy: 270, 540, 810 > 600 close. Row 0 = [0,1].
        // Row 0 is NOT last → must scale to fill 600 from natural 540.
        let layout = MediaLayout.compute(
            aspectRatios: [1.5, 1.5, 1.5, 1.5],
            containerWidth: 600,
            targetRowHeight: 180,
            spacing: 0
        )
        XCTAssertEqual(layout.rows.count, 2)
        XCTAssertEqual(layout.rows[0].count, 2)
        // Row 0: natural 540 → scaled to 600. Each item width 600/2 = 300.
        XCTAssertEqual(layout.frames[0].width, 300, accuracy: 0.001)
        XCTAssertEqual(layout.frames[1].width, 300, accuracy: 0.001)
        // Row height grows proportionally: scale = 600/540 = 1.111
        XCTAssertEqual(layout.frames[0].height, 200, accuracy: 0.001)
    }

    // MARK: - Row scaling math

    func testFilledRowSumsToContainerWidth() {
        let containerWidth: CGFloat = 1200
        let layout = MediaLayout.compute(
            aspectRatios: [1.5, 0.75, 1.0, 1.333, 1.0, 0.667, 1.5],
            containerWidth: containerWidth,
            targetRowHeight: 200,
            spacing: 4
        )
        // Every non-last row should sum to exactly containerWidth (within
        // floating-point tolerance), counting spacing between items.
        for (rowIdx, row) in layout.rows.enumerated() {
            let isLast = rowIdx == layout.rows.count - 1
            if isLast { continue }
            let widthSum = row.reduce(CGFloat(0)) { acc, idx in
                acc + layout.frames[idx].width
            }
            let totalSpacing = CGFloat(row.count - 1) * 4
            XCTAssertEqual(widthSum + totalSpacing, containerWidth, accuracy: 0.5,
                           "Row \(rowIdx) should fill container width exactly")
        }
    }

    func testAllItemsInARowShareSameHeight() {
        let layout = MediaLayout.compute(
            aspectRatios: [1.5, 0.75, 1.0, 1.333, 1.0, 0.667, 1.5],
            containerWidth: 1200,
            targetRowHeight: 200,
            spacing: 4
        )
        for row in layout.rows {
            guard let firstHeight = row.first.map({ layout.frames[$0].height }) else { continue }
            for idx in row {
                XCTAssertEqual(layout.frames[idx].height, firstHeight, accuracy: 0.001,
                               "Items in row should share a height")
            }
        }
    }

    func testEveryItemHasAFrame() {
        let aspects: [Double] = (0..<50).map { _ in Double.random(in: 0.5...2.5) }
        let layout = MediaLayout.compute(
            aspectRatios: aspects,
            containerWidth: 1024,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertEqual(layout.frames.count, 50)
        for frame in layout.frames {
            XCTAssertGreaterThan(frame.width, 0)
            XCTAssertGreaterThan(frame.height, 0)
        }
    }

    func testInputOrderPreserved() {
        // The layout must never reorder items — index 0 stays at index 0
        // visually (row 0, column 0), index N-1 ends up in the last row.
        let layout = MediaLayout.compute(
            aspectRatios: Array(repeating: 1.0, count: 20),
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertEqual(layout.rows[0].first, 0, "First item should be at row 0, col 0")
        XCTAssertEqual(layout.rows.last?.last, 19, "Last item should be in the last row")
    }

    // MARK: - Row map / vertical navigation

    func testRowForAndColumnForMatchRowsArray() {
        let layout = MediaLayout.compute(
            aspectRatios: Array(repeating: 1.0, count: 12),
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        for (rowIdx, row) in layout.rows.enumerated() {
            for (col, idx) in row.enumerated() {
                XCTAssertEqual(layout.rowFor[idx], rowIdx)
                XCTAssertEqual(layout.columnFor[idx], col)
            }
        }
    }

    func testIndexAboveMovesToPreviousRow() {
        let layout = MediaLayout.compute(
            aspectRatios: Array(repeating: 1.0, count: 12),
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        // Pick the first item of row 1 and verify indexAbove gives an
        // item on row 0 at the same (or saturated) column.
        guard layout.rows.count >= 2 else {
            XCTFail("Test fixture should produce ≥ 2 rows")
            return
        }
        let secondRowFirst = layout.rows[1][0]
        let above = layout.indexAbove(secondRowFirst)
        XCTAssertNotNil(above)
        XCTAssertEqual(layout.rowFor[above!], 0)
        XCTAssertEqual(layout.columnFor[above!], 0)
    }

    func testIndexAboveOnFirstRowReturnsNil() {
        let layout = MediaLayout.compute(
            aspectRatios: [1.0, 1.0, 1.0],
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        XCTAssertNil(layout.indexAbove(0))
    }

    func testIndexBelowMovesToNextRow() {
        let layout = MediaLayout.compute(
            aspectRatios: Array(repeating: 1.0, count: 12),
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        guard layout.rows.count >= 2 else {
            XCTFail("Test fixture should produce ≥ 2 rows")
            return
        }
        let firstRowFirst = layout.rows[0][0]
        let below = layout.indexBelow(firstRowFirst)
        XCTAssertNotNil(below)
        XCTAssertEqual(layout.rowFor[below!], 1)
    }

    func testIndexBelowOnLastRowReturnsNil() {
        let layout = MediaLayout.compute(
            aspectRatios: [1.0, 1.0, 1.0],
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        let lastIndex = layout.frames.count - 1
        XCTAssertNil(layout.indexBelow(lastIndex))
    }

    func testColumnSaturatesWhenPreviousRowIsShorter() {
        // Construct a layout where row 0 has 2 items and row 1 has 4.
        // indexAbove of row-1 col-3 should saturate at row-0 col-1
        // (the rightmost item in the shorter row).
        // Container 800, target 180, spacing 0:
        //   row 0: two 2.0-aspect items (360+360 = 720 fits, third would
        //          overflow at 720+180=900 > 800 → close)
        //   row 1: four 1.0-aspect items (720 ≤ 800 fits exactly)
        let layout = MediaLayout.compute(
            aspectRatios: [2.0, 2.0,             // row 0: 2 wide items
                           1.0, 1.0, 1.0, 1.0],  // row 1: 4 squares
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 0
        )
        guard layout.rows.count == 2 else {
            XCTFail("Expected 2-row layout, got \(layout.rows.count)")
            return
        }
        XCTAssertEqual(layout.rows[0].count, 2)
        XCTAssertEqual(layout.rows[1].count, 4)
        let row1Col3 = layout.rows[1][3]
        let above = layout.indexAbove(row1Col3)
        XCTAssertNotNil(above)
        XCTAssertEqual(layout.columnFor[above!], 1, "Should saturate at last col of shorter row")
    }

    func testIndexAtRowOffsetSaturatesAtTopAndBottom() {
        let layout = MediaLayout.compute(
            aspectRatios: Array(repeating: 1.0, count: 12),
            containerWidth: 800,
            targetRowHeight: 180,
            spacing: 4
        )
        // Going way up from item 0 saturates at first row → returns nil
        // because we're already there.
        XCTAssertNil(layout.indexAtRowOffset(0, rowDelta: -100))
        // Going way down from last item saturates at last row → nil.
        let last = layout.frames.count - 1
        XCTAssertNil(layout.indexAtRowOffset(last, rowDelta: 100))
        // Going from first item by +1 row should give a valid index.
        XCTAssertNotNil(layout.indexAtRowOffset(0, rowDelta: 1))
    }
}
