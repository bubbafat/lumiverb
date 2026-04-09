import XCTest
import AVFoundation
import Foundation
@testable import LumiverbKit

/// Tests for `AudioExtraction.extractToWAV`.
///
/// Drives the real production entry point against fixtures generated at
/// test-runtime via `AVAssetWriter`. This avoids bundling binary video
/// fixtures in the repo while still exercising the full AVAssetReader →
/// CMSampleBuffer → PCM → WAV path that production uses.
final class AudioExtractionTests: XCTestCase {

    private var tempDir: URL!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-audioextraction-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    // MARK: - WAV header

    /// Hand-construct PCM bytes, run them through the production
    /// `wrapPCMInWAVHeader`, and assert each field of the resulting 44-byte
    /// canonical WAV header against its known value. Catches header math
    /// regressions deterministically without needing AVFoundation at all.
    func testWrapPCMInWAVHeaderProducesCanonicalHeader() throws {
        // 100 samples × 2 bytes/sample = 200 bytes of PCM payload.
        let pcm = Data(repeating: 0xAB, count: 200)
        let wav = AudioExtraction.wrapPCMInWAVHeader(pcmData: pcm)

        XCTAssertEqual(wav.count, 44 + pcm.count, "WAV file = 44-byte header + PCM payload")

        // Helpers for reading multi-byte little-endian fields.
        func u32(_ offset: Int) -> UInt32 {
            wav.subdata(in: offset..<(offset + 4)).withUnsafeBytes {
                $0.load(as: UInt32.self).littleEndian
            }
        }
        func u16(_ offset: Int) -> UInt16 {
            wav.subdata(in: offset..<(offset + 2)).withUnsafeBytes {
                $0.load(as: UInt16.self).littleEndian
            }
        }
        func ascii(_ offset: Int, _ length: Int) -> String {
            String(data: wav.subdata(in: offset..<(offset + length)), encoding: .ascii) ?? ""
        }

        // Master RIFF chunk
        XCTAssertEqual(ascii(0, 4), "RIFF")
        XCTAssertEqual(u32(4), 36 + UInt32(pcm.count), "RIFF chunk size = 36 + PCM bytes")
        XCTAssertEqual(ascii(8, 4), "WAVE")

        // fmt subchunk
        XCTAssertEqual(ascii(12, 4), "fmt ")
        XCTAssertEqual(u32(16), 16, "PCM fmt chunk is 16 bytes")
        XCTAssertEqual(u16(20), 1, "format = PCM")
        XCTAssertEqual(u16(22), 1, "mono")
        XCTAssertEqual(u32(24), 16000, "sample rate = 16 kHz")
        XCTAssertEqual(u32(28), 32000, "byte rate = sample rate * channels * bps/8")
        XCTAssertEqual(u16(32), 2, "block align = channels * bps/8")
        XCTAssertEqual(u16(34), 16, "16 bits per sample")

        // data subchunk
        XCTAssertEqual(ascii(36, 4), "data")
        XCTAssertEqual(u32(40), UInt32(pcm.count))

        // Payload bytes survive byte-for-byte.
        let payload = wav.subdata(in: 44..<wav.count)
        XCTAssertEqual(payload, pcm)
    }

    // MARK: - extractToWAV against synthesized fixtures

    /// A video file with no audio track should yield `false` and not write
    /// anything to the destination. The Python production reference treats
    /// this as a deterministic empty result, not an error.
    func testExtractToWAVOnVideoWithoutAudioReturnsFalse() async throws {
        let videoURL = tempDir.appendingPathComponent("silent.mp4")
        try await Self.writeVideoOnlyMP4(to: videoURL)

        let outURL = tempDir.appendingPathComponent("out.wav")
        let extracted = try await AudioExtraction.extractToWAV(
            sourceURL: videoURL, destinationURL: outURL,
        )
        XCTAssertFalse(extracted)
        XCTAssertFalse(
            FileManager.default.fileExists(atPath: outURL.path),
            "no WAV should be written when there is no audio track",
        )
    }

    /// An audio-only file (synthesized mono PCM at 44.1 kHz) should be
    /// successfully resampled to the canonical 16 kHz mono PCM_S16LE and
    /// produce a valid WAV with non-zero payload. This is the round-trip
    /// that whisper.cpp consumes in production.
    func testExtractToWAVOnAudioOnlyFileProducesValidWAV() async throws {
        let inputURL = tempDir.appendingPathComponent("tone.m4a")
        try await Self.writeMonoToneM4A(
            to: inputURL,
            sampleRate: 44_100,
            durationSeconds: 1.0,
            frequency: 440,
        )

        let outURL = tempDir.appendingPathComponent("out.wav")
        let extracted = try await AudioExtraction.extractToWAV(
            sourceURL: inputURL, destinationURL: outURL,
        )
        XCTAssertTrue(extracted)
        XCTAssertTrue(FileManager.default.fileExists(atPath: outURL.path))

        let wav = try Data(contentsOf: outURL)
        XCTAssertGreaterThan(wav.count, 44, "must contain WAV header + PCM payload")

        // Re-parse the header — we trust testWrapPCMInWAVHeaderProducesCanonicalHeader
        // for the field-by-field assertions and only check the discriminating
        // bits here.
        XCTAssertEqual(String(data: wav.subdata(in: 0..<4), encoding: .ascii), "RIFF")
        XCTAssertEqual(String(data: wav.subdata(in: 8..<12), encoding: .ascii), "WAVE")
        let sampleRate = wav.subdata(in: 24..<28).withUnsafeBytes {
            $0.load(as: UInt32.self).littleEndian
        }
        XCTAssertEqual(sampleRate, 16000, "AVAssetReader must resample to 16 kHz")

        // 16 kHz mono s16le for ~1 second = ~32 KB of PCM. Allow generous
        // headroom for AVAssetReader silence padding at file boundaries.
        let pcmBytes = wav.count - 44
        XCTAssertGreaterThan(pcmBytes, 8000, "expected at least ~0.25s of audio")
        XCTAssertLessThan(pcmBytes, 200_000, "expected at most ~6s of audio for a 1s source")
    }

    // MARK: - Fixture writers (AVAssetWriter)

    /// Write a single-frame H.264 MP4 with no audio track.
    private static func writeVideoOnlyMP4(to url: URL) async throws {
        try? FileManager.default.removeItem(at: url)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: 64,
            AVVideoHeightKey: 64,
        ]
        let videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        videoInput.expectsMediaDataInRealTime = false

        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: 64,
            kCVPixelBufferHeightKey as String: 64,
        ]
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: videoInput, sourcePixelBufferAttributes: attrs,
        )

        XCTAssertTrue(writer.canAdd(videoInput))
        writer.add(videoInput)
        writer.startWriting()
        writer.startSession(atSourceTime: .zero)

        // Append a single solid-color pixel buffer at t=0.
        var pixelBufferOpt: CVPixelBuffer?
        CVPixelBufferCreate(kCFAllocatorDefault, 64, 64, kCVPixelFormatType_32BGRA,
                            attrs as CFDictionary, &pixelBufferOpt)
        guard let pixelBuffer = pixelBufferOpt else {
            throw NSError(domain: "AudioExtractionTests", code: 1)
        }
        CVPixelBufferLockBaseAddress(pixelBuffer, [])
        if let base = CVPixelBufferGetBaseAddress(pixelBuffer) {
            memset(base, 0x80, 64 * 64 * 4)
        }
        CVPixelBufferUnlockBaseAddress(pixelBuffer, [])

        // Wait for the input to be ready, then append.
        while !videoInput.isReadyForMoreMediaData {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        adaptor.append(pixelBuffer, withPresentationTime: .zero)
        videoInput.markAsFinished()
        await writer.finishWriting()
    }

    /// Write a mono PCM tone to an .m4a (AAC). The exact codec doesn't matter —
    /// AVAssetReader handles AAC → PCM resampling at read time, which is the
    /// real production code path.
    private static func writeMonoToneM4A(
        to url: URL,
        sampleRate: Double,
        durationSeconds: Double,
        frequency: Double,
    ) async throws {
        try? FileManager.default.removeItem(at: url)
        let writer = try AVAssetWriter(outputURL: url, fileType: .m4a)

        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 64_000,
        ]
        let audioInput = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        audioInput.expectsMediaDataInRealTime = false
        XCTAssertTrue(writer.canAdd(audioInput))
        writer.add(audioInput)

        writer.startWriting()
        writer.startSession(atSourceTime: .zero)

        // Synthesize a 1-second mono float32 buffer at 44.1 kHz, then convert
        // to a CMSampleBuffer that the AAC encoder will accept.
        let frameCount = AVAudioFrameCount(sampleRate * durationSeconds)
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false,
        )!
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else {
            throw NSError(domain: "AudioExtractionTests", code: 2)
        }
        buffer.frameLength = frameCount
        if let channel = buffer.floatChannelData?[0] {
            for i in 0..<Int(frameCount) {
                let t = Double(i) / sampleRate
                channel[i] = Float(sin(2 * .pi * frequency * t) * 0.5)
            }
        }

        guard let sampleBuffer = sampleBuffer(from: buffer) else {
            throw NSError(domain: "AudioExtractionTests", code: 3)
        }

        while !audioInput.isReadyForMoreMediaData {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        audioInput.append(sampleBuffer)
        audioInput.markAsFinished()
        await writer.finishWriting()
    }

    /// Convert an AVAudioPCMBuffer into a CMSampleBuffer suitable for
    /// AVAssetWriterInput.append.
    private static func sampleBuffer(from buffer: AVAudioPCMBuffer) -> CMSampleBuffer? {
        var formatDescription: CMAudioFormatDescription?
        let fmtStatus = CMAudioFormatDescriptionCreate(
            allocator: kCFAllocatorDefault,
            asbd: buffer.format.streamDescription,
            layoutSize: 0,
            layout: nil,
            magicCookieSize: 0,
            magicCookie: nil,
            extensions: nil,
            formatDescriptionOut: &formatDescription,
        )
        guard fmtStatus == noErr, let formatDescription else { return nil }

        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: CMTimeScale(buffer.format.sampleRate)),
            presentationTimeStamp: .zero,
            decodeTimeStamp: .invalid,
        )

        var sampleBuffer: CMSampleBuffer?
        let createStatus = withUnsafePointer(to: &timing) { timingPtr -> OSStatus in
            CMSampleBufferCreate(
                allocator: kCFAllocatorDefault,
                dataBuffer: nil,
                dataReady: false,
                makeDataReadyCallback: nil,
                refcon: nil,
                formatDescription: formatDescription,
                sampleCount: CMItemCount(buffer.frameLength),
                sampleTimingEntryCount: 1,
                sampleTimingArray: timingPtr,
                sampleSizeEntryCount: 0,
                sampleSizeArray: nil,
                sampleBufferOut: &sampleBuffer,
            )
        }
        guard createStatus == noErr, let sampleBuffer else { return nil }

        let setStatus = CMSampleBufferSetDataBufferFromAudioBufferList(
            sampleBuffer,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0,
            bufferList: buffer.audioBufferList,
        )
        return setStatus == noErr ? sampleBuffer : nil
    }
}
