import SwiftUI

/// Inline rating editor: heart toggle, 5-star row, 6-swatch color picker.
/// Touch-friendly (44pt tap targets), no hover requirements.
/// Used in the lightbox sidebar and the batch-rate context menu.
public struct RatingEditorView: View {
    @Binding public var rating: Rating
    public var onChange: (RatingUpdateBody) -> Void

    public init(rating: Binding<Rating>, onChange: @escaping (RatingUpdateBody) -> Void) {
        self._rating = rating
        self.onChange = onChange
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Favorite + stars row
            HStack(spacing: 12) {
                favoriteButton
                starsRow
                Spacer()
            }

            // Color swatches
            colorRow
        }
    }

    // MARK: - Favorite

    private var favoriteButton: some View {
        Button {
            let newValue = !rating.favorite
            rating.favorite = newValue
            onChange(RatingUpdateBody(favorite: newValue))
        } label: {
            Image(systemName: rating.favorite ? "heart.fill" : "heart")
                .font(.title3)
                .foregroundColor(rating.favorite ? .red : .secondary)
        }
        .buttonStyle(.plain)
        .frame(minWidth: 44, minHeight: 44)
        .contentShape(Rectangle())
    }

    // MARK: - Stars

    private var starsRow: some View {
        HStack(spacing: 2) {
            ForEach(1...5, id: \.self) { star in
                Button {
                    let newStars = rating.stars == star ? 0 : star
                    rating.stars = newStars
                    onChange(RatingUpdateBody(stars: newStars))
                } label: {
                    Image(systemName: star <= rating.stars ? "star.fill" : "star")
                        .font(.body)
                        .foregroundColor(star <= rating.stars ? .yellow : .secondary)
                }
                .buttonStyle(.plain)
                .frame(minWidth: 30, minHeight: 44)
                .contentShape(Rectangle())
            }
        }
    }

    // MARK: - Color swatches

    private static let swatchColors: [(ColorLabel, Color)] = [
        (.red, .red),
        (.orange, .orange),
        (.yellow, .yellow),
        (.green, .green),
        (.blue, .blue),
        (.purple, .purple),
    ]

    private var colorRow: some View {
        HStack(spacing: 6) {
            ForEach(Self.swatchColors, id: \.0) { label, color in
                Button {
                    if rating.color == label {
                        rating.color = nil
                        onChange(RatingUpdateBody(color: .clear))
                    } else {
                        rating.color = label
                        onChange(RatingUpdateBody(color: .set(label)))
                    }
                } label: {
                    ZStack {
                        Circle()
                            .fill(color)
                            .frame(width: 20, height: 20)
                        if rating.color == label {
                            Image(systemName: "checkmark")
                                .font(.caption2)
                                .fontWeight(.bold)
                                .foregroundColor(.white)
                        }
                    }
                }
                .buttonStyle(.plain)
                .frame(minWidth: 32, minHeight: 32)
                .contentShape(Rectangle())
            }

            // Clear button (only shown when a color is set)
            if rating.color != nil {
                Button {
                    rating.color = nil
                    onChange(RatingUpdateBody(color: .clear))
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
                .frame(minWidth: 32, minHeight: 32)
                .contentShape(Rectangle())
            }
        }
    }
}
