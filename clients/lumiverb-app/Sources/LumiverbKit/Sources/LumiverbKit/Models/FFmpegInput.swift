import Foundation

/// ffmpeg's and ffprobe's arguments for opening a video, before its input:
/// a local file only, read only by the demuxers of the containers Lumiverb
/// takes in. A file whose content is in any other format (a playlist, a list
/// of other files), whatever its name, is refused, not read as that format.
/// Twin of `src/processing/video/ffmpeg_run.py` (`DEMUXERS`, `input_args`).
public enum FFmpegInput {
    /// The ffmpeg demuxer that reads each video extension Lumiverb takes in
    /// (`FileExtensions.video`; an extension added there needs its demuxer
    /// here). A whitelist matches any of a demuxer's names: "mov" is the one
    /// named "mov,mp4,m4a,3gp,3g2,mj2", "matroska" the one named "matroska,webm".
    public static let demuxers: [String: String] = [
        ".mov": "mov",
        ".mp4": "mov",
        ".mkv": "matroska",
    ]

    /// The demuxers ffmpeg may read a video with, as `-format_whitelist` takes them.
    public static let formats: String = Set(FileExtensions.video.compactMap { demuxers[$0] })
        .sorted()
        .joined(separator: ",")

    /// Goes before ffmpeg's `-i` (ffprobe's: before the file).
    public static let arguments: [String] = ["-protocol_whitelist", "file", "-format_whitelist", formats]
}
