import SwiftUI
import AVKit

/// Plays video in the lightbox using AVKit.
///
/// URL resolution order:
/// 1. Local file at `{libraryRootPath}/{relPath}` — full-length playback (macOS only — iOS has no local library access)
/// 2. Server preview via `GET /v1/assets/{id}/preview` — 10-second clip, downloaded to temp file
/// 3. Static proxy image if neither is available
public struct LightboxVideoPlayerView: View {
    public let detail: AssetDetail
    public let libraryRootPath: String?
    public let client: APIClient?

    @StateObject private var viewModel = VideoPlayerViewModel()

    public init(detail: AssetDetail, libraryRootPath: String?, client: APIClient?) {
        self.detail = detail
        self.libraryRootPath = libraryRootPath
        self.client = client
    }

    public var body: some View {
        ZStack {
            Color.black

            switch viewModel.source {
            case .loading:
                posterView
                ProgressView()
                    .tint(.white)

            case .local, .serverPreview:
                if let player = viewModel.player {
                    if viewModel.hasStartedPlaying {
                        PlayerView(player: player)
                    } else {
                        posterView
                        playButton
                    }
                } else {
                    posterView
                }

            case .unavailable:
                posterView
                VStack {
                    Spacer()
                    Text("Video not available for playback")
                        .font(.caption)
                        .foregroundColor(.white.opacity(0.7))
                        .padding(8)
                        .background(.black.opacity(0.5))
                        .cornerRadius(6)
                        .padding(.bottom, 40)
                }
            }

            // Source indicator
            if viewModel.source == .serverPreview && viewModel.hasStartedPlaying {
                VStack {
                    HStack {
                        Text("10s preview")
                            .font(.caption2)
                            .fontWeight(.medium)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(.ultraThinMaterial)
                            .cornerRadius(4)
                        Spacer()
                    }
                    .padding(8)
                    Spacer()
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task(id: detail.assetId) {
            await viewModel.resolve(
                detail: detail,
                libraryRootPath: libraryRootPath,
                client: client
            )
        }
        .onDisappear {
            viewModel.tearDown()
        }
    }

    @ViewBuilder
    private var posterView: some View {
        AuthenticatedImageView(
            assetId: detail.assetId,
            client: client,
            type: .proxy
        )
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var playButton: some View {
        Button {
            viewModel.play()
        } label: {
            Image(systemName: "play.circle.fill")
                .font(.system(size: 64))
                .foregroundColor(.white.opacity(0.85))
                .shadow(color: .black.opacity(0.4), radius: 8)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Platform AVPlayer wrapper
//
// SwiftUI's `VideoPlayer` view crashes on certain assets when AVKit
// queries metadata (a known regression that's why we wrap directly).
// Each platform gets a thin representable around its native player
// type:
//   - macOS: `AVPlayerView` (AppKit) wrapped in `NSViewRepresentable`
//   - iOS:   `AVPlayerViewController` (UIKit) wrapped in
//            `UIViewControllerRepresentable`

#if canImport(AppKit)
struct PlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .floating
        view.showsFullScreenToggleButton = true
        return view
    }

    func updateNSView(_ nsView: AVPlayerView, context: Context) {
        if nsView.player !== player {
            nsView.player = player
        }
    }
}
#elseif canImport(UIKit)
struct PlayerView: UIViewControllerRepresentable {
    let player: AVPlayer

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let vc = AVPlayerViewController()
        vc.player = player
        vc.showsPlaybackControls = true
        return vc
    }

    func updateUIViewController(_ uiViewController: AVPlayerViewController, context: Context) {
        if uiViewController.player !== player {
            uiViewController.player = player
        }
    }
}
#endif

// MARK: - View Model

enum PlaybackSource: Equatable {
    case loading
    case local
    case serverPreview
    case unavailable
}

@MainActor
final class VideoPlayerViewModel: ObservableObject {
    @Published var player: AVPlayer?
    @Published var source: PlaybackSource = .loading
    @Published var hasStartedPlaying = false

    private var currentAssetId: String?
    private var tempFileURL: URL?

    func play() {
        guard let player, !hasStartedPlaying else { return }
        hasStartedPlaying = true
        player.play()
    }

    func resolve(
        detail: AssetDetail,
        libraryRootPath: String?,
        client: APIClient?
    ) async {
        // Reset if switching assets
        if currentAssetId != detail.assetId {
            tearDown()
            currentAssetId = detail.assetId
        }

        // 1. Try local file. macOS only — iOS is sandboxed and never
        // sees a real `libraryRootPath`. The iOS BrowseAppContext returns
        // nil for `selectedLibraryRootPath`, so this branch is dead on
        // iOS even without the explicit gate.
        #if os(macOS)
        if let rootPath = libraryRootPath {
            let fullPath = (rootPath as NSString).appendingPathComponent(detail.relPath)
            if FileManager.default.fileExists(atPath: fullPath) {
                let url = URL(fileURLWithPath: fullPath)
                player = AVPlayer(url: url)
                source = .local
                return
            }
        }
        #endif

        // 2. Try server preview
        if detail.videoPreviewKey != nil, let client {
            do {
                if let data = try await client.getData("/v1/assets/\(detail.assetId)/preview") {
                    let dir = FileManager.default.temporaryDirectory
                        .appendingPathComponent("lumiverb-previews")
                    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                    let fileURL = dir.appendingPathComponent("\(detail.assetId).mp4")
                    try data.write(to: fileURL)
                    self.tempFileURL = fileURL
                    player = AVPlayer(url: fileURL)
                    source = .serverPreview
                    return
                }
            } catch {
                // Fall through
            }
        }

        // 3. Nothing available
        source = .unavailable
    }

    func tearDown() {
        player?.pause()
        player = nil
        hasStartedPlaying = false
        source = .loading

        if let url = tempFileURL {
            try? FileManager.default.removeItem(at: url)
            tempFileURL = nil
        }
    }

    deinit {
        if let url = tempFileURL {
            try? FileManager.default.removeItem(at: url)
        }
    }
}
