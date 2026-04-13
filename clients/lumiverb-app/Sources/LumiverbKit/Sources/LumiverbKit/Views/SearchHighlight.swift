import SwiftUI

/// Build an `AttributedString` from `text` with every whole-word
/// occurrence of any term in `terms` highlighted. Used by the search
/// results grid (cell captions) and the lightbox details sheet (match
/// section) so users can see at a glance *which* word in the snippet
/// brought a result back.
///
/// Matching mirrors the server-side query builder's tokenizer:
///
/// - Case-insensitive.
/// - Whole-word only (a search for "card" highlights "card" but not
///   "cardboard"). Lines up with Quickwit's `default` tokenizer so
///   the highlights match what BM25 actually scored.
/// - Tokens that match the user's query as a substring of a longer
///   word are intentionally NOT highlighted — that would be
///   misleading because BM25 didn't match them either.
///
/// Returns the original string unmodified if `terms` is empty or
/// nothing matches.
public func highlightSearchTerms(
    in text: String,
    terms: [String],
    color: Color = .accentColor
) -> AttributedString {
    var attributed = AttributedString(text)
    guard !text.isEmpty, !terms.isEmpty else { return attributed }

    // Build a single regex that alternates over all terms with
    // word-boundary anchors. NSRegularExpression's `\b` is
    // Unicode-aware enough for our cases (English / European tags).
    let escaped = terms
        .map(NSRegularExpression.escapedPattern(for:))
        .filter { !$0.isEmpty }
    guard !escaped.isEmpty else { return attributed }

    let pattern = "\\b(" + escaped.joined(separator: "|") + ")\\b"
    guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else {
        return attributed
    }

    let nsText = text as NSString
    let range = NSRange(location: 0, length: nsText.length)
    let matches = regex.matches(in: text, options: [], range: range)
    guard !matches.isEmpty else { return attributed }

    // Walk matches in reverse so range offsets stay valid as we apply
    // attributes. AttributedString uses its own range type; we have
    // to convert from NSRange via the underlying String.Index.
    for match in matches.reversed() {
        guard
            let lower = Range(match.range, in: text)?.lowerBound,
            let upper = Range(match.range, in: text)?.upperBound,
            let attrLower = AttributedString.Index(lower, within: attributed),
            let attrUpper = AttributedString.Index(upper, within: attributed)
        else { continue }
        let attrRange = attrLower..<attrUpper
        attributed[attrRange].foregroundColor = color
        attributed[attrRange].font = .system(.caption2, weight: .semibold)
    }
    return attributed
}

/// Tokenize a raw search query the same way the server-side query
/// builder does — lowercased, alphanumeric + hyphen, splitting on
/// everything else. Use this on the iOS side before passing terms
/// to `highlightSearchTerms` so client highlighting matches the
/// server-side tokens that BM25 actually fired on.
public func tokenizeSearchQuery(_ query: String) -> [String] {
    let lowered = query.lowercased()
    var tokens: [String] = []
    var current = ""
    for ch in lowered {
        if ch.isLetter || ch.isNumber || ch == "-" {
            current.append(ch)
        } else if !current.isEmpty {
            tokens.append(current)
            current = ""
        }
    }
    if !current.isEmpty { tokens.append(current) }
    return tokens
}
