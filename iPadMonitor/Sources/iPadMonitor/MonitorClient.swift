import Foundation
import Network
import UIKit
import QuartzCore

struct DiscoveredHost: Identifiable, Hashable, @unchecked Sendable {
    let id: String
    let name: String
    let endpoint: NWEndpoint
}

enum VideoFramePayload: Sendable {
    case h264(Data)
    case mjpeg(SendableBox<UIImage>)
}

private final class NetworkTransportEngine: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.SmokePythonCL.iPadMonitor.net", qos: .userInteractive)
    private var usbListener: NWListener?
    private var wifiBrowser: NWBrowser?
    private var activeConnection: NWConnection?
    private var activeTransport: ConnectionTransport = .none
    private var rxBuffer = Data()
    private var frameCounter: Int = 0
    private var lastFpsTime: TimeInterval = 0

    var onStatus: (@Sendable (String) -> Void)?
    var onConnectionState: (@Sendable (ConnectionTransport, Bool, String?) -> Void)?
    var onAuthRequired: (@Sendable (Data) -> Void)?
    var onAuthResult: (@Sendable (Bool) -> Void)?
    var onHostsChanged: (@Sendable ([DiscoveredHost]) -> Void)?
    var onFrame: (@Sendable (VideoFramePayload) -> Void)?
    var onAudioPCM: (@Sendable (Data) -> Void)?
    var onFps: (@Sendable (Double) -> Void)?

    func startPassiveServices(usbPort: UInt16) {
        queue.async {
            self.startUSBListener(port: NWEndpoint.Port(rawValue: usbPort)!)
            self.startBonjourBrowser()
        }
    }

    func restartWifiBrowser() {
        queue.async {
            self.wifiBrowser?.cancel()
            self.wifiBrowser = nil
            self.startBonjourBrowser()
        }
    }

    func connectToEndpoint(_ endpoint: NWEndpoint, label: String) {
        queue.async {
            let tcpOptions = NWProtocolTCP.Options()
            tcpOptions.noDelay = true
            let params = NWParameters(tls: nil, tcp: tcpOptions)
            let conn = NWConnection(to: endpoint, using: params)
            self.attachConnection(conn, transport: .wifi, label: label)
        }
    }

    func disconnectActive(keepUSBListening: Bool = true) {
        queue.async {
            self.activeConnection?.cancel()
            self.activeConnection = nil
            self.activeTransport = .none
            self.rxBuffer = Data()
            self.onConnectionState?(.none, false, "Desconectado.")
            if keepUSBListening && self.usbListener == nil {
                self.startUSBListener(port: 8888)
            }
        }
    }

    func sendRaw(_ data: Data) {
        queue.async {
            guard let conn = self.activeConnection else { return }
            conn.send(content: data, completion: .idempotent)
        }
    }

    private func startUSBListener(port: NWEndpoint.Port) {
        do {
            let tcpOptions = NWProtocolTCP.Options()
            tcpOptions.noDelay = true
            let params = NWParameters(tls: nil, tcp: tcpOptions)
            let listener = try NWListener(using: params, on: port)

            listener.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    self?.onStatus?("Esperando conexión USB o selecciona un PC por Wi-Fi.")
                case .failed(let err):
                    self?.onStatus?("Error listener USB: \(err.localizedDescription)")
                default:
                    break
                }
            }

            listener.newConnectionHandler = { [weak self] newConn in
                guard let self = self else { return }
                // Accept incoming USB connection (or replace idle connection)
                self.attachConnection(newConn, transport: .usb, label: "Cable USB")
            }

            listener.start(queue: queue)
            usbListener = listener
        } catch {
            onStatus?("No se pudo iniciar listener USB: \(error.localizedDescription)")
        }
    }

    private func startBonjourBrowser() {
        let descriptor = NWBrowser.Descriptor.bonjour(type: "_ipadmonitor._tcp", domain: nil)
        let params = NWParameters()
        params.includePeerToPeer = false
        let browser = NWBrowser(for: descriptor, using: params)

        browser.browseResultsChangedHandler = { [weak self] results, _ in
            let hosts: [DiscoveredHost] = results.compactMap { result in
                if case let .service(name: name, type: _, domain: _, interface: _) = result.endpoint {
                    return DiscoveredHost(id: name, name: name, endpoint: result.endpoint)
                }
                return nil
            }
            .sorted { $0.name < $1.name }
            self?.onHostsChanged?(hosts)
        }

        browser.start(queue: queue)
        wifiBrowser = browser
    }

    private func attachConnection(_ connection: NWConnection, transport: ConnectionTransport, label: String) {
        activeConnection?.cancel()
        activeConnection = connection
        activeTransport = transport
        rxBuffer = Data()
        frameCounter = 0
        lastFpsTime = 0

        connection.stateUpdateHandler = { [weak self] state in
            guard let self = self, self.activeConnection === connection else { return }
            switch state {
            case .ready:
                let ack = Data([MonitorOpcode.cmdAck])
                connection.send(content: ack, completion: .idempotent)
                let readyImmediately = (transport == .usb)
                self.onConnectionState?(transport, readyImmediately, label)
                self.readLoop(connection)
            case .waiting(let err):
                self.onStatus?("Conectando (\(label)): \(err.localizedDescription)")
            case .failed(let err):
                self.activeConnection = nil
                self.activeTransport = .none
                self.onConnectionState?(.none, false, "Error de conexión: \(err.localizedDescription)")
            case .cancelled:
                break
            default:
                break
            }
        }

        connection.start(queue: queue)
    }

    private func readLoop(_ connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 131_072) { [weak self] content, _, isComplete, error in
            guard let self = self, self.activeConnection === connection else { return }
            if let error = error {
                self.activeConnection = nil
                self.activeTransport = .none
                self.onConnectionState?(.none, false, "Conexión perdida: \(error.localizedDescription)")
                return
            }
            if let chunk = content, !chunk.isEmpty {
                self.rxBuffer.append(chunk)
                self.parseBuffer()
            }
            if isComplete {
                self.activeConnection = nil
                self.activeTransport = .none
                self.onConnectionState?(.none, false, "PC desconectado.")
                return
            }
            self.readLoop(connection)
        }
    }

    private func parseBuffer() {
        while !rxBuffer.isEmpty {
            let opcode = rxBuffer[rxBuffer.startIndex]

            if opcode == MonitorOpcode.frameMJPEG {
                guard rxBuffer.count >= 5 else { break }
                let length = Int(rxBuffer.readUInt32BE(offset: 1))
                guard length > 0 && length < 25_000_000 else {
                    rxBuffer.removeFirst()
                    continue
                }
                let totalNeeded = 5 + length
                guard rxBuffer.count >= totalNeeded else { break }

                let s = rxBuffer.startIndex
                let frameData = Data(rxBuffer[(s + 5)..<(s + totalNeeded)])
                rxBuffer.removeSubrange(s..<(s + totalNeeded))

                if let decoded = UIImage(data: frameData)?.preparingForDisplay() {
                    trackFrameRate()
                    onFrame?(.mjpeg(SendableBox(value: decoded)))
                }
            } else if opcode == MonitorOpcode.frameH264 {
                guard rxBuffer.count >= 6 else { break }
                let length = Int(rxBuffer.readUInt32BE(offset: 2))
                guard length > 0 && length < 25_000_000 else {
                    rxBuffer.removeFirst()
                    continue
                }
                let totalNeeded = 6 + length
                guard rxBuffer.count >= totalNeeded else { break }

                let s = rxBuffer.startIndex
                let auData = Data(rxBuffer[(s + 6)..<(s + totalNeeded)])
                rxBuffer.removeSubrange(s..<(s + totalNeeded))

                trackFrameRate()
                onFrame?(.h264(auData))
            } else if opcode == MonitorOpcode.frameAudioPCM {
                guard rxBuffer.count >= 5 else { break }
                let length = Int(rxBuffer.readUInt32BE(offset: 1))
                guard length > 0 && length < 1_000_000 else {
                    rxBuffer.removeFirst()
                    continue
                }
                let totalNeeded = 5 + length
                guard rxBuffer.count >= totalNeeded else { break }

                let s = rxBuffer.startIndex
                let pcmData = Data(rxBuffer[(s + 5)..<(s + totalNeeded)])
                rxBuffer.removeSubrange(s..<(s + totalNeeded))
                onAudioPCM?(pcmData)
            } else if opcode == MonitorOpcode.statusText {
                guard rxBuffer.count >= 3 else { break }
                let length = Int(rxBuffer.readUInt16BE(offset: 1))
                let totalNeeded = 3 + length
                guard rxBuffer.count >= totalNeeded else { break }

                let s = rxBuffer.startIndex
                let msgData = Data(rxBuffer[(s + 3)..<(s + totalNeeded)])
                rxBuffer.removeSubrange(s..<(s + totalNeeded))

                if let text = String(data: msgData, encoding: .utf8) {
                    onStatus?(text)
                }
            } else if opcode == MonitorOpcode.authChallengeOrResult {
                guard rxBuffer.count >= 2 else { break }
                let s = rxBuffer.startIndex
                let authStatus = rxBuffer[s + 1]
                if authStatus == MonitorOpcode.authRequired {
                    guard rxBuffer.count >= 34 else { break }
                    let nonce = Data(rxBuffer[(s + 2)..<(s + 34)])
                    rxBuffer.removeSubrange(s..<(s + 34))
                    onAuthRequired?(nonce)
                } else {
                    rxBuffer.removeSubrange(s..<(s + 2))
                    onAuthResult?(authStatus == MonitorOpcode.authOK)
                }
            } else {
                rxBuffer.removeFirst()
            }
        }

        if rxBuffer.isEmpty {
            rxBuffer = Data()
        }
    }

    private func trackFrameRate() {
        frameCounter += 1
        let now = CACurrentMediaTime()
        if lastFpsTime == 0 {
            lastFpsTime = now
        } else if now - lastFpsTime >= 1.0 {
            let measured = Double(frameCounter) / (now - lastFpsTime)
            frameCounter = 0
            lastFpsTime = now
            onFps?(measured)
        }
    }
}

@MainActor
final class MonitorClient: ObservableObject {
    @Published var isHostConnected: Bool = false
    @Published var isStreaming: Bool = false
    @Published var activeTransport: ConnectionTransport = .none
    @Published var connectedHostName: String = ""
    @Published var fps: Double = 0.0
    @Published var statusMessage: String = "Iniciando detector USB y Wi-Fi…"
    @Published var activeCodecLabel: String = "H.264"

    @Published var discoveredHosts: [DiscoveredHost] = []
    @Published var manualHostIP: String {
        didSet { UserDefaults.standard.set(manualHostIP, forKey: "lastWifiIP") }
    }
    @Published var manualWifiPort: String {
        didSet { UserDefaults.standard.set(manualWifiPort, forKey: "lastWifiPort") }
    }
    @Published var codecPreference: CodecPreference {
        didSet { UserDefaults.standard.set(Int(codecPreference.rawValue), forKey: "codecPreference") }
    }
    @Published var savedPin: String {
        didSet { UserDefaults.standard.set(savedPin, forKey: "savedWifiPin") }
    }
    @Published var isAudioEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isAudioEnabled, forKey: "audioEnabled")
            syncAudioStreamingState()
        }
    }
    @Published var touchMode: TouchInteractionMode {
        didSet { UserDefaults.standard.set(touchMode.rawValue, forKey: "touchMode") }
    }

    @Published var isPinPromptPresented: Bool = false
    @Published var authErrorMessage: String = ""

    var onVideoFrame: ((VideoFramePayload) -> Void)?

    private let engine = NetworkTransportEngine()
    private let audioPlayer = PCMStreamAudioPlayer()
    private var pendingNonce: Data?
    private var triedSavedPinOnCurrentConn = false

    init() {
        self.manualHostIP = UserDefaults.standard.string(forKey: "lastWifiIP") ?? ""
        self.manualWifiPort = UserDefaults.standard.string(forKey: "lastWifiPort") ?? "8889"
        let rawCodec = UInt8(UserDefaults.standard.integer(forKey: "codecPreference"))
        self.codecPreference = CodecPreference(rawValue: rawCodec) ?? .autoH264
        self.savedPin = UserDefaults.standard.string(forKey: "savedWifiPin") ?? ""
        self.isAudioEnabled = UserDefaults.standard.bool(forKey: "audioEnabled")
        let rawTouch = UserDefaults.standard.string(forKey: "touchMode") ?? TouchInteractionMode.directTouch.rawValue
        self.touchMode = TouchInteractionMode(rawValue: rawTouch) ?? .directTouch

        bindEngineCallbacks()
        engine.startPassiveServices(usbPort: 8888)
    }

    private func bindEngineCallbacks() {
        engine.onStatus = { [weak self] msg in
            Task { @MainActor [weak self] in
                self?.statusMessage = msg
            }
        }

        engine.onHostsChanged = { [weak self] hosts in
            Task { @MainActor [weak self] in
                self?.discoveredHosts = hosts
            }
        }

        engine.onConnectionState = { [weak self] transport, readyImmediately, label in
            Task { @MainActor [weak self] in
                guard let self = self else { return }
                self.activeTransport = transport
                self.triedSavedPinOnCurrentConn = false
                if transport == .none {
                    self.isHostConnected = false
                    self.isStreaming = false
                    self.audioPlayer.stop()
                    self.fps = 0.0
                    self.connectedHostName = ""
                    self.isPinPromptPresented = false
                    self.pendingNonce = nil
                    if let label { self.statusMessage = label }
                } else {
                    self.connectedHostName = label ?? transport.rawValue
                    self.isHostConnected = readyImmediately
                    if !readyImmediately {
                        self.statusMessage = "Conectando a \(self.connectedHostName)…"
                    }
                }
            }
        }

        engine.onAuthRequired = { [weak self] nonce in
            Task { @MainActor [weak self] in
                guard let self = self else { return }
                self.pendingNonce = nonce
                if !self.savedPin.isEmpty && !self.triedSavedPinOnCurrentConn {
                    self.triedSavedPinOnCurrentConn = true
                    self.submitPin(self.savedPin)
                } else {
                    self.isPinPromptPresented = true
                    self.statusMessage = "Introduce el PIN mostrado en tu PC Linux."
                }
            }
        }

        engine.onAuthResult = { [weak self] ok in
            Task { @MainActor [weak self] in
                guard let self = self else { return }
                if ok {
                    self.isHostConnected = true
                    self.isPinPromptPresented = false
                    self.authErrorMessage = ""
                    self.pendingNonce = nil
                    self.statusMessage = "Conectado (\(self.connectedHostName)). Pulsa 'Iniciar Pantalla en Linux'."
                } else {
                    self.isHostConnected = false
                    self.authErrorMessage = "PIN incorrecto. Revisa el PIN en tu PC."
                    self.isPinPromptPresented = true
                }
            }
        }

        engine.onFps = { [weak self] measuredFps in
            Task { @MainActor [weak self] in
                self?.fps = measuredFps
            }
        }

        engine.onAudioPCM = { [weak self] pcmData in
            self?.audioPlayer.enqueuePCM(pcmData)
        }

        engine.onFrame = { [weak self] payload in
            Task { @MainActor [weak self] in
                guard let self = self else { return }
                if !self.isStreaming {
                    self.isStreaming = true
                    self.syncAudioStreamingState()
                }
                switch payload {
                case .h264:
                    self.activeCodecLabel = "H.264"
                case .mjpeg:
                    self.activeCodecLabel = "MJPEG"
                }
                self.onVideoFrame?(payload)
            }
        }
    }

    func connectBonjour(_ host: DiscoveredHost) {
        authErrorMessage = ""
        statusMessage = "Conectando por Wi-Fi a \(host.name)…"
        engine.connectToEndpoint(host.endpoint, label: host.name)
    }

    func connectManualWifi() {
        let trimmedIP = manualHostIP.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedIP.isEmpty else {
            statusMessage = "Ingresa la dirección IP de tu PC Linux."
            return
        }
        let portNum = UInt16(manualWifiPort.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 8889
        let host = NWEndpoint.Host(trimmedIP)
        let port = NWEndpoint.Port(rawValue: portNum) ?? 8889
        authErrorMessage = ""
        statusMessage = "Conectando a \(trimmedIP):\(portNum)…"
        engine.connectToEndpoint(.hostPort(host: host, port: port), label: trimmedIP)
    }

    func disconnectConnection() {
        if isStreaming {
            requestStopScreen()
        }
        engine.disconnectActive(keepUSBListening: true)
    }

    func refreshWifiDiscovery() {
        engine.restartWifiBrowser()
    }

    func submitPin(_ pin: String) {
        let cleaned = pin.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let nonce = pendingNonce, !cleaned.isEmpty else { return }
        savedPin = cleaned
        let proof = MonitorCrypto.hmacProof(pin: cleaned, nonce: nonce)
        var packet = Data(capacity: 1 + proof.count)
        packet.append(MonitorOpcode.authResponse)
        packet.append(proof)
        engine.sendRaw(packet)
    }

    func cancelPinPrompt() {
        isPinPromptPresented = false
        authErrorMessage = ""
        engine.disconnectActive(keepUSBListening: true)
    }

    func requestStartScreen() {
        guard isHostConnected else { return }
        let bounds = UIScreen.main.nativeBounds
        var w = UInt32(bounds.width).bigEndian
        var h = UInt32(bounds.height).bigEndian
        var scale = Float(UIScreen.main.scale).bitPattern.bigEndian

        var packet = Data(capacity: 14)
        packet.append(MonitorOpcode.cmdStart)
        withUnsafeBytes(of: &w) { packet.append(contentsOf: $0) }
        withUnsafeBytes(of: &h) { packet.append(contentsOf: $0) }
        withUnsafeBytes(of: &scale) { packet.append(contentsOf: $0) }
        packet.append(codecPreference.rawValue)

        engine.sendRaw(packet)
        statusMessage = "Solicitando pantalla virtual en Linux…"
    }

    func requestStopScreen() {
        engine.sendRaw(Data([MonitorOpcode.cmdStop]))
        audioPlayer.stop()
        isStreaming = false
        fps = 0.0
        statusMessage = "Transmisión detenida."
    }

    func requestResetPortal() {
        guard isHostConnected else { return }
        engine.sendRaw(Data([MonitorOpcode.cmdResetPortal]))
        audioPlayer.stop()
        if isStreaming {
            isStreaming = false
            fps = 0.0
        }
        statusMessage = "Selección olvidada. Pulsa Iniciar para elegir otra pantalla."
    }

    func syncAudioStreamingState() {
        guard isHostConnected && isStreaming else {
            audioPlayer.stop()
            return
        }
        if isAudioEnabled {
            audioPlayer.start()
        } else {
            audioPlayer.stop()
        }
        engine.sendRaw(Data([MonitorOpcode.cmdAudio, isAudioEnabled ? 1 : 0]))
    }

    func sendTouchEvent(type: UInt8, normX: Float, normY: Float, pressure: Float = 1.0) {
        guard isHostConnected else { return }
        var packet = Data(capacity: 14)
        var x = normX.bitPattern.bigEndian
        var y = normY.bitPattern.bigEndian
        var p = pressure.bitPattern.bigEndian

        packet.append(MonitorOpcode.touchEvent)
        packet.append(type)
        withUnsafeBytes(of: &x) { packet.append(contentsOf: $0) }
        withUnsafeBytes(of: &y) { packet.append(contentsOf: $0) }
        withUnsafeBytes(of: &p) { packet.append(contentsOf: $0) }

        engine.sendRaw(packet)
    }

    func sendMultiTouchEvent(slot: UInt8, phase: UInt8, normX: Float, normY: Float, pressure: Float = 1.0) {
        guard isHostConnected else { return }
        var packet = Data(capacity: 15)
        var x = normX.bitPattern.bigEndian
        var y = normY.bitPattern.bigEndian
        var p = pressure.bitPattern.bigEndian

        packet.append(MonitorOpcode.mtEvent)
        packet.append(slot)
        packet.append(phase)
        withUnsafeBytes(of: &x) { packet.append(contentsOf: $0) }
        withUnsafeBytes(of: &y) { packet.append(contentsOf: $0) }
        withUnsafeBytes(of: &p) { packet.append(contentsOf: $0) }

        engine.sendRaw(packet)
    }
}
