import AVFoundation
import Foundation

final class PCMStreamAudioPlayer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.SmokePythonCL.iPadMonitor.audio", qos: .userInteractive)
    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private let audioFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 48_000,
        channels: 2,
        interleaved: false
    )!

    private var isRunning = false
    private var pendingBuffers = 0
    private let maxPendingBuffers = 6

    init() {
        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: audioFormat)
    }

    func start() {
        queue.async {
            guard !self.isRunning else { return }
            do {
                let session = AVAudioSession.sharedInstance()
                try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
                try session.setPreferredSampleRate(48_000)
                try session.setPreferredIOBufferDuration(0.01)
                try session.setActive(true)
                try self.engine.start()
                self.playerNode.play()
                self.pendingBuffers = 0
                self.isRunning = true
            } catch {
                self.isRunning = false
            }
        }
    }

    func stop() {
        queue.async {
            guard self.isRunning else { return }
            self.playerNode.stop()
            self.engine.stop()
            self.pendingBuffers = 0
            self.isRunning = false
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    func enqueuePCM(_ data: Data) {
        queue.async { [weak self] in
            guard let strongSelf = self, strongSelf.isRunning, data.count >= 4 else { return }
            if strongSelf.pendingBuffers >= strongSelf.maxPendingBuffers {
                // Drop oldest/excess chunk to keep audio tightly synced with video
                return
            }

            let frameCount = data.count / 4  // 2 channels * 2 bytes (S16LE)
            guard frameCount > 0,
                  let buffer = AVAudioPCMBuffer(pcmFormat: strongSelf.audioFormat, frameCapacity: AVAudioFrameCount(frameCount)),
                  let leftChannel = buffer.floatChannelData?[0],
                  let rightChannel = buffer.floatChannelData?[1]
            else {
                return
            }

            buffer.frameLength = AVAudioFrameCount(frameCount)
            data.withUnsafeBytes { rawPtr in
                let samples = rawPtr.bindMemory(to: Int16.self)
                let scale: Float = 1.0 / 32768.0
                for i in 0..<frameCount {
                    leftChannel[i] = Float(Int16(littleEndian: samples[i * 2])) * scale
                    rightChannel[i] = Float(Int16(littleEndian: samples[i * 2 + 1])) * scale
                }
            }

            strongSelf.pendingBuffers += 1
            strongSelf.playerNode.scheduleBuffer(buffer) { [weak strongSelf] in
                guard let cbSelf = strongSelf else { return }
                cbSelf.queue.async { [cbSelf] in
                    cbSelf.pendingBuffers = max(0, cbSelf.pendingBuffers - 1)
                }
            }
        }
    }
}
