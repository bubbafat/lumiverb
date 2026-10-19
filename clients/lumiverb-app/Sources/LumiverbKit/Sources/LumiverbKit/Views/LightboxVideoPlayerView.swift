import SwiftUI
import AVKit

/// Plays video in the lightbox using AVKit.
///
/// URL resolution order:
/// 1. Local file at `{libraryRootPath}/{relPath}` — full-length playback (macOS only — iOS has no local library access)
/// 2. The server's stream via `GET /v1/assets/{id}/playback` — a signed link
///    AVPlayer streams: the whole video once its analysis proxy exists (up
///    to the account's cap), else the scan's 10-second clip
/// 3. Server preview via `GET /v1/assets/{id}/artifacts/lumiverb.video.preview` — 10-second clip, downloaded to temp file (an older server)
/// 4. Static proxy image if none is available
///
/// `start` (a person's moment) is where it plays from: once the source
/// reaches it, playing at once when the request says so.
public struct LightboxVideoPlayerView: View {
    public let detail: AssetDetail
    public let libraryRootPath: String?
    public let client: APIClient?
    public let start: VideoStart?

    @StateObject private var viewModel = VideoPlayerViewModel()

    public init(detail: AssetDetail, libraryRootPath: String?, client: APIClient?, start: VideoStart? = nil) {
        self.detail = detail
        self.libraryRootPath = libraryRootPath
        self.client = client
        self.start = start
    }

    public var body: some View {
        ZStack {
            Color.black

            switch viewModel.source {
            case .loading:
                posterView
                ProgressView()
                    .tint(.white)

            case .local, .stream, .serverPreview:
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

            // A moment this source doesn't reach.
            if let moment = viewModel.unreachableMoment {
                VStack {
                    Spacer()
                    Text(viewModel.isPreview
                         ? "\(FaceMoments.clock(Int(moment * 1000))) plays once the whole video is ready"
                         : "\(FaceMoments.clock(Int(moment * 1000))) is past what plays here")
                        .font(.caption)
                        .foregroundColor(.white)
                        .padding(8)
                        .background(.black.opacity(0.6))
                        .cornerRadius(6)
                        .padding(.bottom, 56)
                }
            }

            // Source indicator
            if viewModel.isPreview && viewModel.hasStartedPlaying {
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
            if let start, start.assetId == detail.assetId { await viewModel.seek(start) }
        }
        .onChange(of: start) { _, newValue in
            guard let newValue, newValue.assetId == detail.assetId else { return }
            Task { await viewModel.seek(newValue) }
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
    /// The server's stream (`/playback`): the whole video, or the 10-second clip.
    case stream
    case serverPreview
    case unavailable
}

@MainActor
final class VideoPlayerViewModel: ObservableObject {
    @Published var player: AVPlayer?
    @Published var source: PlaybackSource = .loading
    @Published var hasStartedPlaying = false

    /// The stream is the scan's 10-second clip, not the whole video (a
    /// shorter clip's is the whole of it: not a preview).
    @Published var streamIsPreview = false
    /// A moment asked for that this source doesn't reach (past the short
    /// preview, or the account's playback cap): said where the video is.
    @Published var unreachableMoment: Double?

    private var currentAssetId: String?
    private var tempFileURL: URL?

    var isPreview: Bool { source == .serverPreview || (source == .stream && streamIsPreview) }

    func play() {
        guard let player, !hasStartedPlaying else { return }
        hasStartedPlaying = true
        player.play()
    }

    /// Go to a moment, once the player's item says how long it is (a moment
    /// past the 10-second clip can't be shown: it stays where it is). Plays
    /// from there when asked to; otherwise the next play starts there.
    func seek(_ start: VideoStart) async {
        guard let player, let item = player.currentItem else { return }
        var length = try? await item.asset.load(.duration)
        if length == nil || !(length!.seconds.isFinite) {
            length = nil
        }
        if let length, start.seconds >= length.seconds {
            unreachableMoment = start.seconds
            return
        }
        unreachableMoment = nil
        await player.seek(to: CMTime(seconds: start.seconds, preferredTimescale: 600),
                          toleranceBefore: .zero, toleranceAfter: .zero)
        if start.play {
            hasStartedPlaying = true
            player.play()
        }
    }

    /// The server's signed stream link (`GET /v1/assets/{id}/playback`):
    /// asked again while its capped copy is made (`ready` false), up to ~20 s.
    /// Nil when there's nothing to play, or an older server.
    static func streamLink(_ client: APIClient, assetId: String) async -> PlaybackLink? {
        for _ in 0..<10 {
            guard let link: PlaybackLink = try? await client.get("/v1/assets/\(assetId)/playback") else {
                return nil
            }
            if link.ready { return link }
            try? await Task.sleep(for: .seconds(2))
        }
        return nil
    }

    /// The video preview; nil when there's none. While the server makes a
    /// capped copy it answers 503 (Retry-After 2): asked again for up to 30 s.
    static func previewData(_ client: APIClient, assetId: String) async throws -> Data? {
        for _ in 0..<15 {
            do {
                return try await client.getData(ClipFile.videoPreview.path(assetId))
            } catch APIError.serverError(statusCode: 503, _) {
                try await Task.sleep(for: .seconds(2))
            }
        }
        return nil
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
        if let rootPath = libraryRootPath,
           let url = LibraryPath.fileURL(root: rootPath, relPath: detail.relPath) {
            if FileManager.default.fileExists(atPath: url.path) {
                player = AVPlayer(url: url)
                source = .local
                return
            }
        }
        #endif

        // 2. The server's stream: AVPlayer streams the signed link (Range requests).
        if let client, let link = await Self.streamLink(client, assetId: detail.assetId) {
            guard currentAssetId == detail.assetId else { return }
            // Under the server's address as every request is (a server under a path keeps it).
            let url = client.baseURL.appendingPathComponent(link.url)
            player = AVPlayer(url: url)
            streamIsPreview = !link.isWhole
            source = .stream
            return
        }

        // 3. Try server preview
        let previewDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-previews")
        if detail.videoPreviewKey != nil, let client,
           let fileURL = cacheEntryURL(previewDir, detail.assetId, suffix: ".mp4") {
            do {
                if let data = try await Self.previewData(client, assetId: detail.assetId) {
                    try? FileManager.default.createDirectory(at: previewDir, withIntermediateDirectories: true)
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

        // 4. Nothing available
        source = .unavailable
    }

    func tearDown() {
        player?.pause()
        player = nil
        hasStartedPlaying = false
        streamIsPreview = false
        unreachableMoment = nil
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
