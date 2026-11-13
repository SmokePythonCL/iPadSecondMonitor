import Foundation
import CryptoKit

enum MonitorOpcode {
    // Host -> iPad
    static let frameMJPEG: UInt8 = 0x01
    static let statusText: UInt8 = 0x02
    static let frameH264: UInt8 = 0x04
    static let authChallengeOrResult: UInt8 = 0x06

    // Auth statuses
    static let authRequired: UInt8 = 0x00
    static let authOK: UInt8 = 0x01
    static let authFailed: UInt8 = 0x02

    // iPad -> Host
    static let cmdStart: UInt8 = 0x01
    static let cmdStop: UInt8 = 0x02
    static let touchEvent: UInt8 = 0x03
    static let cmdAck: UInt8 = 0x05
    static let authResponse: UInt8 = 0x06
    static let cmdResetPortal: UInt8 = 0x07
}

enum TouchActionType {
    static let down: UInt8 = 0
    static let move: UInt8 = 1
    static let up: UInt8 = 2
    static let rightClick: UInt8 = 3
    static let scroll: UInt8 = 4
}

enum CodecPreference: UInt8, CaseIterable, Identifiable {
    case autoH264 = 0
    case mjpeg = 1

    var id: UInt8 { rawValue }

    var displayName: String {
        switch self {
        case .autoH264: return "H.264 (Hardware)"
        case .mjpeg: return "MJPEG (Clásico)"
        }
    }
}

enum ConnectionTransport: String {
    case none = "Sin conexión"
    case usb = "Cable USB"
    case wifi = "Wi-Fi"
}

enum MonitorCrypto {
    static func hmacProof(pin: String, nonce: Data) -> Data {
        let key = SymmetricKey(data: Data(pin.utf8))
        let mac = HMAC<SHA256>.authenticationCode(for: nonce, using: key)
        return Data(mac)
    }
}

struct SendableBox<T>: @unchecked Sendable {
    let value: T
}

extension Data {
    func readUInt16BE(offset: Int) -> UInt16 {
        let i = startIndex + offset
        return (UInt16(self[i]) << 8) | UInt16(self[i + 1])
    }

    func readUInt32BE(offset: Int) -> UInt32 {
        let i = startIndex + offset
        return (UInt32(self[i]) << 24) |
               (UInt32(self[i + 1]) << 16) |
               (UInt32(self[i + 2]) << 8) |
               UInt32(self[i + 3])
    }
}
