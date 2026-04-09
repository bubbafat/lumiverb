import AVFoundation
import Foundation

/// Extracts the audio track of a video file to a 16 kHz mono signed-16-bit
/// little-endian PCM WAV file. This is the canonical input format expected by
/// `whisper.cpp` (and most other speech recognition engines), so producing it
/// directly avoids needing an external `ffmpeg` install on the macOS app.
///
/// Mirrors the Python production reference at
/// `src/client/cli/repair.py:120-146`, which uses
/// `ffmpeg -vn -ar 16000 -ac 1 -f wav` for the same purpose.
public enum AudioExtraction {

    /// Whisper expects 16 kHz mono PCM_S16LE.
    private static let sampleRate: Int = 16000
    private static let channelCount: Int = 1
    private static let bitsPerSample: Int = 16

    public enum AudioExtractionError: Error, CustomStringConvertible {
        case readerCreationFailed(String)
        case readerStartFailed(String)
        case readFailed(String)
        case writeFailed(String)

        public var description: String {
            switch self {
            case .readerCreationFailed(let m): return "AVAssetReader creation failed: \(m)"
            case .readerStartFailed(let m): return "AVAssetReader start failed: \(m)"
            case .readFailed(let m): return "PCM read failed: \(m)"
            case .writeFailed(let m): return "WAV write failed: \(m)"
            }
        }
    }

    /// Extract audio from `sourceURL` and write a 16 kHz mono PCM_S16LE WAV
    /// file to `destinationURL`.
    ///
    /// **Memory profile:** streams PCM samples directly to disk via a
    /// `FileHandle` rather than accumulating the whole file in memory. A
    /// 1-hour video produces ~110 MB of PCM, a 3-hour interview ~330 MB —
    /// holding all of that in a `Data` was a real OOM risk for long-form
    /// content. The current implementation uses one `CMBlockBuffer` worth
    /// of memory at a time regardless of source duration.
    ///
    /// - Returns: `true` if audio was extracted; `false` if the source has no
    ///   audio track at all (in which case `destinationURL` is left untouched).
    ///   The "no audio track" case is deliberately not an error — the Python
    ///   reference treats it as a deterministic empty result, and the
    ///   /v1/assets/{id}/transcript endpoint accepts an empty SRT to mark an
    ///   asset as "checked, no speech".
    /// - Throws: `AudioExtractionError` if a track exists but extraction fails.
    public static func extractToWAV(
        sourceURL: URL,
        destinationURL: URL
    ) async throws -> Bool {
        let asset = AVURLAsset(url: sourceURL)

        let tracks: [AVAssetTrack]
        do {
            tracks = try await asset.loadTracks(withMediaType: .audio)
        } catch {
            // No audio track is communicated as an empty result by some
            // codecs but as a load error by others — be lenient.
            return false
        }
        guard let track = tracks.first else {
            return false
        }

        let outputSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: channelCount,
            AVLinearPCMBitDepthKey: bitsPerSample,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]

        let reader: AVAssetReader
        do {
            reader = try AVAssetReader(asset: asset)
        } catch {
            throw AudioExtractionError.readerCreationFailed(error.localizedDescription)
        }
        let trackOutput = AVAssetReaderTrackOutput(
            track: track, outputSettings: outputSettings,
        )
        guard reader.canAdd(trackOutput) else {
            throw AudioExtractionError.readerCreationFailed("cannot add audio track output")
        }
        reader.add(trackOutput)

        // Open the destination file and write a placeholder header that
        // will be patched after we know the final PCM byte count.
        FileManager.default.createFile(atPath: destinationURL.path, contents: nil)
        let handle: FileHandle
        do {
            handle = try FileHandle(forWritingTo: destinationURL)
        } catch {
            throw AudioExtractionError.writeFailed(error.localizedDescription)
        }
        defer { try? handle.close() }

        do {
            try handle.write(contentsOf: placeholderWAVHeader())
        } catch {
            throw AudioExtractionError.writeFailed(error.localizedDescription)
        }

        guard reader.startReading() else {
            let detail = reader.error?.localizedDescription ?? "unknown"
            throw AudioExtractionError.readerStartFailed(detail)
        }

        var pcmByteCount: Int64 = 0
        while reader.status == .reading {
            guard let sampleBuffer = trackOutput.copyNextSampleBuffer() else {
                break
            }
            if let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) {
                let length = CMBlockBufferGetDataLength(blockBuffer)
                if length > 0 {
                    var chunk = Data(count: length)
                    let copyResult = chunk.withUnsafeMutableBytes { rawBuf -> OSStatus in
                        guard let base = rawBuf.baseAddress else { return -1 }
                        return CMBlockBufferCopyDataBytes(
                            blockBuffer,
                            atOffset: 0,
                            dataLength: length,
                            destination: base,
                        )
                    }
                    if copyResult == kCMBlockBufferNoErr {
                        do {
                            try handle.write(contentsOf: chunk)
                            pcmByteCount += Int64(length)
                        } catch {
                            CMSampleBufferInvalidate(sampleBuffer)
                            throw AudioExtractionError.writeFailed(error.localizedDescription)
                        }
                    }
                }
            }
            CMSampleBufferInvalidate(sampleBuffer)
        }

        if reader.status == .failed {
            let detail = reader.error?.localizedDescription ?? "unknown"
            throw AudioExtractionError.readFailed(detail)
        }

        // Patch the placeholder header with the real chunk sizes.
        do {
            try patchWAVHeaderSizes(handle: handle, pcmByteCount: pcmByteCount)
        } catch {
            throw AudioExtractionError.writeFailed(error.localizedDescription)
        }
        return true
    }

    /// Build a 44-byte WAV header with placeholder chunk-size fields.
    /// `patchWAVHeaderSizes` is called after PCM streaming completes to
    /// fill in the real values.
    private static func placeholderWAVHeader() -> Data {
        // Use 0 for the size fields; they get patched later. Everything
        // else is fixed for our canonical 16 kHz mono PCM_S16LE format.
        wrapPCMInWAVHeader(pcmData: Data())
    }

    /// Seek back to the size fields in the WAV header and write the real
    /// chunk sizes once `pcmByteCount` is known.
    ///
    /// Header layout:
    ///   offset 4  (u32 LE) — RIFF chunk size = 36 + dataSize
    ///   offset 40 (u32 LE) — data chunk size = dataSize
    private static func patchWAVHeaderSizes(handle: FileHandle, pcmByteCount: Int64) throws {
        let dataSize = UInt32(clamping: pcmByteCount)
        let riffSize = UInt32(clamping: pcmByteCount + 36)

        // Patch RIFF size at offset 4.
        try handle.seek(toOffset: 4)
        var riff = riffSize.littleEndian
        try handle.write(contentsOf: Data(bytes: &riff, count: 4))

        // Patch data chunk size at offset 40.
        try handle.seek(toOffset: 40)
        var data = dataSize.littleEndian
        try handle.write(contentsOf: Data(bytes: &data, count: 4))
    }

    /// Wrap raw 16 kHz mono PCM_S16LE samples in a 44-byte canonical WAV
    /// header. Public for testing purposes — production callers go through
    /// `extractToWAV`.
    public static func wrapPCMInWAVHeader(pcmData: Data) -> Data {
        let dataSize = UInt32(pcmData.count)
        let byteRate = UInt32(sampleRate * channelCount * bitsPerSample / 8)
        let blockAlign = UInt16(channelCount * bitsPerSample / 8)
        let chunkSize = UInt32(36 + Int(dataSize))

        var header = Data(capacity: 44)
        header.append(contentsOf: "RIFF".utf8)
        header.append(uint32LE(chunkSize))
        header.append(contentsOf: "WAVE".utf8)
        // fmt subchunk
        header.append(contentsOf: "fmt ".utf8)
        header.append(uint32LE(16))                       // PCM fmt chunk size
        header.append(uint16LE(1))                        // PCM format
        header.append(uint16LE(UInt16(channelCount)))
        header.append(uint32LE(UInt32(sampleRate)))
        header.append(uint32LE(byteRate))
        header.append(uint16LE(blockAlign))
        header.append(uint16LE(UInt16(bitsPerSample)))
        // data subchunk
        header.append(contentsOf: "data".utf8)
        header.append(uint32LE(dataSize))

        var result = Data(capacity: header.count + pcmData.count)
        result.append(header)
        result.append(pcmData)
        return result
    }

    private static func uint16LE(_ value: UInt16) -> Data {
        var v = value.littleEndian
        return Data(bytes: &v, count: 2)
    }

    private static func uint32LE(_ value: UInt32) -> Data {
        var v = value.littleEndian
        return Data(bytes: &v, count: 4)
    }
}
