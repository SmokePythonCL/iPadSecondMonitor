import Foundation
import AVFoundation
import CoreMedia
import QuartzCore

@MainActor
final class H264StreamDecoder {
    private var sps: Data?
    private var pps: Data?
    private var formatDescription: CMVideoFormatDescription?
    private(set) var videoSize: CGSize = .zero

    func reset() {
        sps = nil
        pps = nil
        formatDescription = nil
        videoSize = .zero
    }

    /// Parse an Annex-B H.264 Access Unit and enqueue onto AVSampleBufferDisplayLayer.
    func decodeAndDisplay(_ annexBData: Data, on layer: AVSampleBufferDisplayLayer) -> CGSize? {
        let nalus = splitAnnexBNALUs(annexBData)
        guard !nalus.isEmpty else { return nil }

        var spsChanged = false
        var ppsChanged = false
        var vclNALUs: [Data] = []
        vclNALUs.reserveCapacity(nalus.count)

        for nalu in nalus {
            guard let firstByte = nalu.first else { continue }
            let nalType = firstByte & 0x1F
            switch nalType {
            case 7: // SPS
                if sps != nalu {
                    sps = nalu
                    spsChanged = true
                }
            case 8: // PPS
                if pps != nalu {
                    pps = nalu
                    ppsChanged = true
                }
            case 1, 5: // Non-IDR slice or IDR keyframe
                vclNALUs.append(nalu)
            default:
                break
            }
        }

        if (spsChanged || ppsChanged || formatDescription == nil),
           let spsData = sps,
           let ppsData = pps {
            if let desc = createFormatDescription(sps: spsData, pps: ppsData) {
                formatDescription = desc
                let dims = CMVideoFormatDescriptionGetDimensions(desc)
                videoSize = CGSize(width: CGFloat(dims.width), height: CGFloat(dims.height))
            }
        }

        guard let fmt = formatDescription, !vclNALUs.isEmpty else {
            return videoSize == .zero ? nil : videoSize
        }

        var avccData = Data()
        for nalu in vclNALUs {
            var bigLen = UInt32(nalu.count).bigEndian
            withUnsafeBytes(of: &bigLen) { avccData.append(contentsOf: $0) }
            avccData.append(nalu)
        }

        guard let sampleBuffer = createSampleBuffer(avccData: avccData, formatDescription: fmt) else {
            return nil
        }

        let renderer = layer.sampleBufferRenderer
        if renderer.status == .failed {
            renderer.flush()
        }
        renderer.enqueue(sampleBuffer)

        return videoSize == .zero ? nil : videoSize
    }

    private func splitAnnexBNALUs(_ data: Data) -> [Data] {
        var result: [Data] = []
        let count = data.count
        guard count > 4 else { return result }

        return data.withUnsafeBytes { rawBuf -> [Data] in
            guard let base = rawBuf.baseAddress?.assumingMemoryBound(to: UInt8.self) else {
                return []
            }
            var naluStart: Int? = nil
            var i = 0
            while i < count {
                var startCodeLen = 0
                if i + 3 < count && base[i] == 0 && base[i + 1] == 0 && base[i + 2] == 0 && base[i + 3] == 1 {
                    startCodeLen = 4
                } else if i + 2 < count && base[i] == 0 && base[i + 1] == 0 && base[i + 2] == 1 {
                    startCodeLen = 3
                }

                if startCodeLen > 0 {
                    if let prev = naluStart, i > prev {
                        result.append(Data(bytes: base + prev, count: i - prev))
                    }
                    naluStart = i + startCodeLen
                    i += startCodeLen
                } else {
                    i += 1
                }
            }
            if let prev = naluStart, count > prev {
                result.append(Data(bytes: base + prev, count: count - prev))
            }
            return result
        }
    }

    private func createFormatDescription(sps: Data, pps: Data) -> CMVideoFormatDescription? {
        var formatDesc: CMVideoFormatDescription?
        let status: OSStatus = sps.withUnsafeBytes { spsRaw in
            pps.withUnsafeBytes { ppsRaw in
                guard let spsPtr = spsRaw.baseAddress?.assumingMemoryBound(to: UInt8.self),
                      let ppsPtr = ppsRaw.baseAddress?.assumingMemoryBound(to: UInt8.self) else {
                    return kCMFormatDescriptionError_InvalidParameter
                }
                let pointers: [UnsafePointer<UInt8>] = [spsPtr, ppsPtr]
                let sizes: [Int] = [sps.count, pps.count]
                return pointers.withUnsafeBufferPointer { ptrBuf in
                    sizes.withUnsafeBufferPointer { sizeBuf in
                        guard let ptrBase = ptrBuf.baseAddress, let sizeBase = sizeBuf.baseAddress else {
                            return kCMFormatDescriptionError_InvalidParameter
                        }
                        return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                            allocator: kCFAllocatorDefault,
                            parameterSetCount: 2,
                            parameterSetPointers: ptrBase,
                            parameterSetSizes: sizeBase,
                            nalUnitHeaderLength: 4,
                            formatDescriptionOut: &formatDesc
                        )
                    }
                }
            }
        }
        return status == noErr ? formatDesc : nil
    }

    private func createSampleBuffer(avccData: Data, formatDescription: CMVideoFormatDescription) -> CMSampleBuffer? {
        var blockBuffer: CMBlockBuffer?
        let createStatus = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: avccData.count,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: avccData.count,
            flags: CMBlockBufferFlags(kCMBlockBufferAssureMemoryNowFlag),
            blockBufferOut: &blockBuffer
        )
        guard createStatus == kCMBlockBufferNoErr, let block = blockBuffer else { return nil }

        let copyStatus = avccData.withUnsafeBytes { rawBuf -> OSStatus in
            guard let base = rawBuf.baseAddress else { return kCMBlockBufferBadLengthParameterErr }
            return CMBlockBufferReplaceDataBytes(
                with: base,
                blockBuffer: block,
                offsetIntoDestination: 0,
                dataLength: avccData.count
            )
        }
        guard copyStatus == kCMBlockBufferNoErr else { return nil }

        var sampleBuffer: CMSampleBuffer?
        var timing = CMSampleTimingInfo(
            duration: .invalid,
            presentationTimeStamp: CMTime(seconds: CACurrentMediaTime(), preferredTimescale: 1_000_000_000),
            decodeTimeStamp: .invalid
        )
        var sampleSize = avccData.count
        let sbStatus = CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault,
            dataBuffer: block,
            formatDescription: formatDescription,
            sampleCount: 1,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 1,
            sampleSizeArray: &sampleSize,
            sampleBufferOut: &sampleBuffer
        )
        guard sbStatus == noErr, let sb = sampleBuffer else { return nil }

        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: true) {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(attachments, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(
                dict,
                Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                Unmanaged.passUnretained(kCFBooleanTrue).toOpaque()
            )
        }
        return sb
    }
}
