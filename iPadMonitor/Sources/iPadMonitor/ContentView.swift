import SwiftUI
import UIKit

enum ConnectionTab: String, CaseIterable, Identifiable {
    case usb = "Cable USB"
    case wifi = "Wi-Fi (LAN)"
    var id: String { rawValue }
}

struct ContentView: View {
    @StateObject private var client = MonitorClient()
    @State private var selectedTab: ConnectionTab = .usb
    @State private var fitMode: Bool = true
    @State private var isMenuVisible: Bool = true
    @State private var pinInput: String = ""

    var nativeResolutionString: String {
        let b = UIScreen.main.nativeBounds
        return "\(Int(b.width))x\(Int(b.height))"
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            if client.isStreaming {
                streamingView
            } else {
                lobbyView
            }
        }
        .statusBarHidden(client.isStreaming)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            pinInput = client.savedPin
        }
        .alert("Autenticación Wi-Fi requerida", isPresented: $client.isPinPromptPresented) {
            TextField("PIN de 4 dígitos", text: $pinInput)
                .keyboardType(.numberPad)
            Button("Conectar") {
                client.submitPin(pinInput)
            }
            Button("Cancelar", role: .cancel) {
                client.cancelPinPrompt()
            }
        } message: {
            Text(
                client.authErrorMessage.isEmpty
                    ? "Introduce el PIN mostrado en la notificación o terminal de tu PC Linux."
                    : client.authErrorMessage
            )
        }
    }

    private var streamingView: some View {
        ZStack {
            StreamSurfaceView(
                client: client,
                fitMode: fitMode,
                touchMode: client.touchMode,
                onToggleMenu: {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.75)) {
                        isMenuVisible.toggle()
                    }
                }
            )
            .ignoresSafeArea()

            VStack {
                if isMenuVisible {
                    hudBar
                        .transition(.move(edge: .top).combined(with: .opacity))
                } else {
                    collapsedMenuButton
                        .transition(.opacity)
                }
                Spacer()
            }
            .padding(.top, 10)
        }
    }

    private var hudBar: some View {
        HStack(spacing: 12) {
            Button(action: {
                client.requestStopScreen()
            }) {
                HStack(spacing: 6) {
                    Image(systemName: "xmark.circle.fill")
                    Text("Desconectar")
                        .fontWeight(.semibold)
                }
                .font(.system(size: 13))
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.9))
                .foregroundColor(.white)
                .cornerRadius(10)
            }

            Button(action: {
                fitMode.toggle()
            }) {
                HStack(spacing: 6) {
                    Image(systemName: fitMode ? "arrow.up.left.and.arrow.down.right" : "aspectratio")
                    Text(fitMode ? "Llenar" : "Ajustar")
                        .fontWeight(.medium)
                }
                .font(.system(size: 13))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.white.opacity(0.18))
                .foregroundColor(.white)
                .cornerRadius(10)
            }

            Button(action: {
                client.isAudioEnabled.toggle()
            }) {
                HStack(spacing: 5) {
                    Image(systemName: client.isAudioEnabled ? "speaker.wave.2.fill" : "speaker.slash.fill")
                    Text("Audio")
                        .fontWeight(.medium)
                }
                .font(.system(size: 13))
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .background(client.isAudioEnabled ? Color.green.opacity(0.35) : Color.white.opacity(0.15))
                .foregroundColor(.white)
                .cornerRadius(10)
            }
            .help("Enviar audio de Linux al iPad")

            Button(action: {
                client.touchMode = (client.touchMode == .directTouch) ? .trackpad : .directTouch
            }) {
                HStack(spacing: 5) {
                    Image(systemName: client.touchMode == .directTouch ? "hand.tap.fill" : "computermouse.fill")
                    Text(client.touchMode.rawValue)
                        .fontWeight(.medium)
                }
                .font(.system(size: 13))
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .background(Color.white.opacity(0.15))
                .foregroundColor(.white)
                .cornerRadius(10)
            }
            .help("Alternar entre pantalla táctil directa y modo puntero")

            Button(action: {
                client.requestResetPortal()
            }) {
                Image(systemName: "rectangle.on.rectangle.angled")
                    .font(.system(size: 13, weight: .medium))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color.white.opacity(0.15))
                    .foregroundColor(.white)
                    .cornerRadius(10)
            }
            .help("Cambiar monitor compartido en KDE")

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text("\(Int(client.fps)) FPS")
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .foregroundColor(client.fps >= 45 ? .green : (client.fps >= 15 ? .yellow : .orange))
                    Text("• \(client.activeCodecLabel)")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundColor(.cyan)
                }
                Text("\(client.activeTransport.rawValue) • \(nativeResolutionString)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(.gray)
            }
            .frame(minWidth: 110)

            Button(action: {
                withAnimation(.spring(response: 0.3, dampingFraction: 0.75)) {
                    isMenuVisible = false
                }
            }) {
                HStack(spacing: 4) {
                    Image(systemName: "chevron.up")
                    Text("Ocultar")
                }
                .font(.system(size: 12, weight: .medium))
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .background(Color.white.opacity(0.12))
                .foregroundColor(.white.opacity(0.85))
                .cornerRadius(10)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 18)
                .fill(Color(white: 0.12).opacity(0.92))
                .overlay(
                    RoundedRectangle(cornerRadius: 18)
                        .stroke(Color.white.opacity(0.2), lineWidth: 1)
                )
                .shadow(color: .black.opacity(0.5), radius: 10, y: 5)
        )
    }

    private var collapsedMenuButton: some View {
        HStack {
            Spacer()
            Button(action: {
                withAnimation(.spring(response: 0.3, dampingFraction: 0.75)) {
                    isMenuVisible = true
                }
            }) {
                HStack(spacing: 6) {
                    Image(systemName: "slider.horizontal.3")
                    Text("Menú")
                        .font(.system(size: 12, weight: .bold))
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color(white: 0.15).opacity(0.90))
                .foregroundColor(.white)
                .cornerRadius(18)
                .overlay(
                    RoundedRectangle(cornerRadius: 18)
                        .stroke(Color.blue.opacity(0.8), lineWidth: 1.5)
                )
                .shadow(color: .black.opacity(0.4), radius: 6, y: 3)
            }
            .padding(.trailing, 16)
        }
    }

    private var lobbyView: some View {
        ScrollView {
            VStack(spacing: 22) {
                headerSection

                statusCard

                if !client.isHostConnected {
                    connectionSelectorSection
                } else {
                    connectedControlsSection
                }

                codecPickerSection

                actionButtonsSection
            }
            .padding(24)
            .frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
    }

    private var headerSection: some View {
        VStack(spacing: 10) {
            MonitorLogoView(
                isConnected: client.isHostConnected,
                isWifi: client.activeTransport == .wifi
            )
            .padding(.bottom, 2)

            Text("iPad Second Monitor")
                .font(.system(size: 30, weight: .bold))
                .foregroundColor(.white)

            Text("Resolución nativa: \(nativeResolutionString)")
                .font(.subheadline)
                .foregroundColor(.gray)
        }
    }

    private var statusCard: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(client.isHostConnected ? Color.green : Color.orange)
                .frame(width: 12, height: 12)
            VStack(alignment: .leading, spacing: 3) {
                if client.isHostConnected {
                    Text("Conectado por \(client.activeTransport.rawValue) (\(client.connectedHostName))")
                        .font(.caption.bold())
                        .foregroundColor(.green)
                }
                Text(client.statusMessage)
                    .font(.footnote)
                    .foregroundColor(.white)
                    .multilineTextAlignment(.leading)
            }
            Spacer()
            if client.isHostConnected && client.activeTransport == .wifi {
                Button("Salir") {
                    client.disconnectConnection()
                }
                .font(.caption.bold())
                .foregroundColor(.red)
            }
        }
        .padding(14)
        .background(Color.white.opacity(0.08))
        .cornerRadius(12)
    }

    private var connectionSelectorSection: some View {
        VStack(spacing: 16) {
            Picker("Modo", selection: $selectedTab) {
                ForEach(ConnectionTab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)

            if selectedTab == .usb {
                VStack(spacing: 10) {
                    Label("Modo Cable USB Automático", systemImage: "cable.connector")
                        .font(.subheadline.bold())
                        .foregroundColor(.white)
                    Text("Conecta el cable USB al PC Linux. El servicio en segundo plano lo detectará al instante sin PIN.")
                        .font(.caption)
                        .foregroundColor(.gray)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(16)
                .background(Color.white.opacity(0.06))
                .cornerRadius(12)
            } else {
                wifiPanel
            }
        }
    }

    private var wifiPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label("Equipos Linux detectados", systemImage: "bonjour")
                    .font(.subheadline.bold())
                    .foregroundColor(.white)
                Spacer()
                Button(action: { client.refreshWifiDiscovery() }) {
                    Image(systemName: "arrow.clockwise")
                        .font(.caption.bold())
                        .foregroundColor(.blue)
                }
            }

            if client.discoveredHosts.isEmpty {
                Text("Buscando equipos en la red local (_ipadmonitor._tcp)… Asegúrate de tener el puerto TCP 8889 permitido en tu firewall.")
                    .font(.caption)
                    .foregroundColor(.gray)
                    .padding(.vertical, 4)
            } else {
                ForEach(client.discoveredHosts) { host in
                    Button(action: {
                        client.connectBonjour(host)
                    }) {
                        HStack {
                            Image(systemName: "desktopcomputer")
                                .foregroundColor(.blue)
                            Text(host.name)
                                .font(.subheadline.weight(.medium))
                                .foregroundColor(.white)
                            Spacer()
                            Text("Conectar")
                                .font(.caption.bold())
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Color.blue.opacity(0.85))
                                .foregroundColor(.white)
                                .cornerRadius(8)
                        }
                        .padding(10)
                        .background(Color.white.opacity(0.08))
                        .cornerRadius(10)
                    }
                }
            }

            Divider().background(Color.white.opacity(0.15))

            Text("Conexión manual por IP")
                .font(.caption.bold())
                .foregroundColor(.gray)

            HStack(spacing: 10) {
                TextField("IP del PC (ej: 192.168.1.50)", text: $client.manualHostIP)
                    .textFieldStyle(.plain)
                    .keyboardType(.decimalPad)
                    .padding(10)
                    .background(Color.white.opacity(0.08))
                    .foregroundColor(.white)
                    .cornerRadius(8)

                TextField("Puerto", text: $client.manualWifiPort)
                    .textFieldStyle(.plain)
                    .keyboardType(.numberPad)
                    .frame(width: 72)
                    .padding(10)
                    .background(Color.white.opacity(0.08))
                    .foregroundColor(.white)
                    .cornerRadius(8)

                Button("Conectar") {
                    client.connectManualWifi()
                }
                .font(.subheadline.bold())
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color.blue)
                .foregroundColor(.white)
                .cornerRadius(8)
            }
        }
        .padding(16)
        .background(Color.white.opacity(0.06))
        .cornerRadius(12)
    }

    private var connectedControlsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Gestos en pantalla Linux:")
                .font(.caption.bold())
                .foregroundColor(.gray)
            HStack(spacing: 14) {
                Label("Táctil / Zoom multitáctil", systemImage: "hand.tap")
                Label("Mantener = Clic der.", systemImage: "hand.point.up.left.fill")
                Label("3 dedos = Menú", systemImage: "hand.raised.fingers.spread")
            }
            .font(.caption2)
            .foregroundColor(.white.opacity(0.8))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.white.opacity(0.06))
        .cornerRadius(12)
    }

    private var codecPickerSection: some View {
        HStack {
            Text("Códec de video:")
                .font(.caption.bold())
                .foregroundColor(.gray)
            Spacer()
            Picker("Códec", selection: $client.codecPreference) {
                ForEach(CodecPreference.allCases) { codec in
                    Text(codec.displayName).tag(codec)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 300)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.06))
        .cornerRadius(12)
    }

    private var actionButtonsSection: some View {
        VStack(spacing: 12) {
            Button(action: {
                client.requestStartScreen()
            }) {
                HStack(spacing: 12) {
                    Image(systemName: "display.and.arrow.down")
                        .font(.title3)
                    Text("Iniciar Pantalla en Linux")
                        .font(.headline)
                }
                .foregroundColor(.white)
                .frame(maxWidth: 360)
                .padding(.vertical, 16)
                .background(client.isHostConnected ? Color.blue : Color.gray.opacity(0.4))
                .cornerRadius(14)
                .shadow(color: client.isHostConnected ? Color.blue.opacity(0.35) : .clear, radius: 8, y: 4)
            }
            .disabled(!client.isHostConnected)

            if client.isHostConnected {
                Button(action: {
                    client.requestResetPortal()
                }) {
                    Label("Cambiar pantalla / reiniciar permisos KDE", systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
            }
        }
    }
}

private struct MonitorLogoView: View {
    let isConnected: Bool
    let isWifi: Bool

    private let size: CGFloat = 88

    var body: some View {
        let s = size / 1024.0
        let ipadAccent = isConnected
            ? [Color(red: 0.25, green: 0.90, blue: 0.65), Color(red: 0.12, green: 0.72, blue: 0.48)]
            : [Color(red: 0.40, green: 0.91, blue: 0.98), Color(red: 0.23, green: 0.51, blue: 0.96)]

        ZStack(alignment: .topLeading) {
            LinearGradient(
                colors: [
                    Color(red: 0.04, green: 0.075, blue: 0.14),
                    Color(red: 0.07, green: 0.135, blue: 0.245),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )

            // Host monitor stand
            Rectangle()
                .fill(Color(red: 0.31, green: 0.55, blue: 0.96).opacity(0.8))
                .frame(width: 44 * s, height: 58 * s)
                .offset(x: 384 * s, y: 516 * s)

            RoundedRectangle(cornerRadius: 11 * s, style: .continuous)
                .fill(Color(red: 0.38, green: 0.65, blue: 0.98).opacity(0.88))
                .frame(width: 176 * s, height: 22 * s)
                .offset(x: 318 * s, y: 566 * s)

            // Primary Linux display
            RoundedRectangle(cornerRadius: 28 * s, style: .continuous)
                .fill(Color(red: 0.06, green: 0.12, blue: 0.22))
                .frame(width: 480 * s, height: 300 * s)
                .overlay(
                    RoundedRectangle(cornerRadius: 28 * s, style: .continuous)
                        .strokeBorder(Color(red: 0.36, green: 0.58, blue: 0.96), lineWidth: 1.5)
                )
                .offset(x: 166 * s, y: 220 * s)

            // Cutout gap around overlapping iPad
            RoundedRectangle(cornerRadius: 44 * s, style: .continuous)
                .fill(Color(red: 0.045, green: 0.082, blue: 0.155))
                .frame(width: 452 * s, height: 344 * s)
                .offset(x: 422 * s, y: 420 * s)

            // Secondary iPad display
            RoundedRectangle(cornerRadius: 34 * s, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [
                            Color(red: 0.09, green: 0.31, blue: 0.53),
                            Color(red: 0.07, green: 0.18, blue: 0.35),
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .frame(width: 420 * s, height: 312 * s)
                .overlay(
                    RoundedRectangle(cornerRadius: 34 * s, style: .continuous)
                        .strokeBorder(
                            LinearGradient(colors: ipadAccent, startPoint: .topLeading, endPoint: .bottomTrailing),
                            lineWidth: 2.0
                        )
                )
                .overlay(
                    Image(systemName: isConnected ? (isWifi ? "wifi" : "cable.connector") : "arrow.right")
                        .font(.system(size: isConnected ? 13 : 16, weight: .bold))
                        .foregroundColor(.white)
                )
                .offset(x: 438 * s, y: 436 * s)
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: 228 * s, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 228 * s, style: .continuous)
                .stroke(Color.white.opacity(0.14), lineWidth: 1)
        )
        .shadow(color: (isConnected ? Color.green : Color.blue).opacity(0.28), radius: 12, y: 5)
    }
}
