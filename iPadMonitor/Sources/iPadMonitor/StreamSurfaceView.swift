import SwiftUI
import UIKit
import AVFoundation

struct StreamSurfaceView: UIViewRepresentable {
    @ObservedObject var client: MonitorClient
    let fitMode: Bool
    let touchMode: TouchInteractionMode
    var onToggleMenu: (() -> Void)?

    func makeUIView(context: Context) -> StreamDisplayView {
        let view = StreamDisplayView(frame: .zero)
        view.updateFitMode(fitMode)
        view.updateTouchMode(touchMode)
        view.onToggleMenu = onToggleMenu
        view.onTouch = { [weak client] type, nx, ny, pressure in
            client?.sendTouchEvent(type: type, normX: nx, normY: ny, pressure: pressure)
        }
        view.onMultiTouch = { [weak client] slot, phase, nx, ny, pressure in
            client?.sendMultiTouchEvent(slot: slot, phase: phase, normX: nx, normY: ny, pressure: pressure)
        }
        client.onVideoFrame = { [weak view] payload in
            view?.render(payload: payload)
        }
        return view
    }

    func updateUIView(_ uiView: StreamDisplayView, context: Context) {
        uiView.updateFitMode(fitMode)
        uiView.updateTouchMode(touchMode)
        uiView.onToggleMenu = onToggleMenu
        client.onVideoFrame = { [weak uiView] payload in
            uiView?.render(payload: payload)
        }
    }
}

@MainActor
final class StreamDisplayView: UIView {
    private let imageView = UIImageView()
    private let h264Layer = AVSampleBufferDisplayLayer()
    private let decoder = H264StreamDecoder()
    private let haptics = UIImpactFeedbackGenerator(style: .medium)

    private var currentVideoSize: CGSize = .zero
    private var fitMode: Bool = true
    private var touchMode: TouchInteractionMode = .directTouch
    private var suppressCurrentTouch = false
    private var activeSlots: [ObjectIdentifier: UInt8] = [:]
    private var twoFingerScrollRecognizer: UIPanGestureRecognizer?

    var onTouch: ((UInt8, Float, Float, Float) -> Void)?
    var onMultiTouch: ((UInt8, UInt8, Float, Float, Float) -> Void)?
    var onToggleMenu: (() -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = true
        isMultipleTouchEnabled = true
        clipsToBounds = true
        backgroundColor = .black

        h264Layer.videoGravity = .resizeAspect
        h264Layer.backgroundColor = UIColor.black.cgColor
        h264Layer.isHidden = true
        layer.addSublayer(h264Layer)

        imageView.frame = bounds
        imageView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        imageView.clipsToBounds = true
        imageView.contentMode = .scaleAspectFit
        imageView.isHidden = true
        addSubview(imageView)

        setupGestures()
        haptics.prepare()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        h264Layer.frame = bounds
        CATransaction.commit()
    }

    private func setupGestures() {
        // Three-finger single tap toggles HUD menu (keeps 1-finger double-tap & 2-finger gestures free for Linux)
        let threeFingerTap = UITapGestureRecognizer(target: self, action: #selector(handleToggleGesture))
        threeFingerTap.numberOfTapsRequired = 1
        threeFingerTap.numberOfTouchesRequired = 3
        threeFingerTap.cancelsTouchesInView = false
        addGestureRecognizer(threeFingerTap)

        // Long press (0.45s) triggers Linux right-click cleanly with haptic feedback
        let longPress = UILongPressGestureRecognizer(target: self, action: #selector(handleLongPress(_:)))
        longPress.minimumPressDuration = 0.45
        longPress.allowableMovement = 12.0
        addGestureRecognizer(longPress)

        // Two-finger pan performs smooth scrolling in Trackpad mode only
        let twoFingerScroll = UIPanGestureRecognizer(target: self, action: #selector(handleTwoFingerScroll(_:)))
        twoFingerScroll.minimumNumberOfTouches = 2
        twoFingerScroll.maximumNumberOfTouches = 2
        twoFingerScroll.isEnabled = (touchMode == .trackpad)
        addGestureRecognizer(twoFingerScroll)
        twoFingerScrollRecognizer = twoFingerScroll
    }

    func updateTouchMode(_ mode: TouchInteractionMode) {
        guard touchMode != mode else { return }
        releaseAllActiveSlots()
        touchMode = mode
        twoFingerScrollRecognizer?.isEnabled = (mode == .trackpad)
    }

    func updateFitMode(_ fit: Bool) {
        fitMode = fit
        imageView.contentMode = fit ? .scaleAspectFit : .scaleAspectFill
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        h264Layer.videoGravity = fit ? .resizeAspect : .resizeAspectFill
        CATransaction.commit()
    }

    func render(payload: VideoFramePayload) {
        switch payload {
        case .h264(let data):
            if !imageView.isHidden {
                imageView.isHidden = true
                imageView.image = nil
            }
            if h264Layer.isHidden {
                h264Layer.isHidden = false
            }
            if let size = decoder.decodeAndDisplay(data, on: h264Layer) {
                currentVideoSize = size
            }
        case .mjpeg(let box):
            let img = box.value
            if !h264Layer.isHidden {
                h264Layer.isHidden = true
                h264Layer.sampleBufferRenderer.flush()
                decoder.reset()
            }
            if imageView.isHidden {
                imageView.isHidden = false
            }
            imageView.image = img
            currentVideoSize = img.size
        }
    }

    @objc private func handleToggleGesture() {
        suppressCurrentTouch = true
        releaseAllActiveSlots()
        onTouch?(TouchActionType.up, 0, 0, 0)
        onToggleMenu?()
    }

    @objc private func handleLongPress(_ recognizer: UILongPressGestureRecognizer) {
        guard recognizer.state == .began else { return }
        suppressCurrentTouch = true
        releaseAllActiveSlots()
        let loc = recognizer.location(in: self)
        let (nx, ny) = normalizedPoint(for: loc)
        // Release initial touch-down before issuing right click
        onTouch?(TouchActionType.up, nx, ny, 0)
        onTouch?(TouchActionType.rightClick, nx, ny, 1.0)
        haptics.impactOccurred()
    }

    @objc private func handleTwoFingerScroll(_ recognizer: UIPanGestureRecognizer) {
        if recognizer.state == .began {
            suppressCurrentTouch = true
            onTouch?(TouchActionType.up, 0, 0, 0)
        }
        guard recognizer.state == .changed else { return }
        let delta = recognizer.translation(in: self)
        recognizer.setTranslation(.zero, in: self)
        // Negative/scaled axis mapping matches natural trackpad feel
        let dx = Float(-delta.x * 0.15)
        let dy = Float(-delta.y * 0.15)
        if abs(dx) > 0.01 || abs(dy) > 0.01 {
            onTouch?(TouchActionType.scroll, dx, dy, 0)
        }
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesBegan(touches, with: event)
        if touchMode == .directTouch {
            suppressCurrentTouch = false
            for touch in touches {
                if let slot = allocateSlot(for: touch) {
                    sendMultiTouch(touch, slot: slot, phase: TouchActionType.down)
                }
            }
            return
        }
        guard touches.count == 1, (event?.allTouches?.count ?? 1) == 1, let touch = touches.first else {
            suppressCurrentTouch = true
            onTouch?(TouchActionType.up, 0, 0, 0)
            return
        }
        suppressCurrentTouch = false
        sendTouch(touch, type: TouchActionType.down)
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesMoved(touches, with: event)
        guard !suppressCurrentTouch else { return }
        if touchMode == .directTouch {
            for touch in touches {
                let key = ObjectIdentifier(touch)
                if let slot = activeSlots[key] {
                    sendMultiTouch(touch, slot: slot, phase: TouchActionType.move)
                }
            }
            return
        }
        guard touches.count == 1, let touch = touches.first else { return }
        sendTouch(touch, type: TouchActionType.move)
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesEnded(touches, with: event)
        if touchMode == .directTouch {
            for touch in touches {
                let key = ObjectIdentifier(touch)
                if let slot = activeSlots.removeValue(forKey: key) {
                    sendMultiTouch(touch, slot: slot, phase: TouchActionType.up)
                }
            }
            if activeSlots.isEmpty {
                suppressCurrentTouch = false
            }
            return
        }
        if suppressCurrentTouch {
            suppressCurrentTouch = false
            return
        }
        guard let touch = touches.first else { return }
        sendTouch(touch, type: TouchActionType.up)
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesCancelled(touches, with: event)
        if touchMode == .directTouch {
            for touch in touches {
                let key = ObjectIdentifier(touch)
                if let slot = activeSlots.removeValue(forKey: key) {
                    sendMultiTouch(touch, slot: slot, phase: TouchActionType.up)
                }
            }
            if activeSlots.isEmpty {
                suppressCurrentTouch = false
            }
            return
        }
        if suppressCurrentTouch {
            suppressCurrentTouch = false
            return
        }
        guard let touch = touches.first else { return }
        sendTouch(touch, type: TouchActionType.up)
    }

    private func allocateSlot(for touch: UITouch) -> UInt8? {
        let key = ObjectIdentifier(touch)
        if let existing = activeSlots[key] {
            return existing
        }
        let used = Set(activeSlots.values)
        for candidate in UInt8(0)..<UInt8(10) {
            if !used.contains(candidate) {
                activeSlots[key] = candidate
                return candidate
            }
        }
        return nil
    }

    private func releaseAllActiveSlots() {
        for (_, slot) in activeSlots {
            onMultiTouch?(slot, TouchActionType.up, 0, 0, 0)
        }
        activeSlots.removeAll()
    }

    private func sendMultiTouch(_ touch: UITouch, slot: UInt8, phase: UInt8) {
        let loc = touch.location(in: self)
        let (nx, ny) = normalizedPoint(for: loc)
        let pressure = Float(touch.maximumPossibleForce > 0 ? touch.force / touch.maximumPossibleForce : 1.0)
        onMultiTouch?(slot, phase, nx, ny, pressure)
    }

    private func sendTouch(_ touch: UITouch, type: UInt8) {
        let loc = touch.location(in: self)
        let (nx, ny) = normalizedPoint(for: loc)
        let pressure = Float(touch.maximumPossibleForce > 0 ? touch.force / touch.maximumPossibleForce : 1.0)
        onTouch?(type, nx, ny, pressure)
    }

    /// Map touch location accurately onto rendered video bounds (compensating aspect letterboxing/cropping).
    private func normalizedPoint(for point: CGPoint) -> (Float, Float) {
        let targetRect = renderedVideoRect(for: currentVideoSize, in: bounds, fitMode: fitMode)
        guard targetRect.width > 0, targetRect.height > 0 else { return (0.5, 0.5) }
        let nx = Float(max(0.0, min(1.0, (point.x - targetRect.minX) / targetRect.width)))
        let ny = Float(max(0.0, min(1.0, (point.y - targetRect.minY) / targetRect.height)))
        return (nx, ny)
    }

    private func renderedVideoRect(for videoSize: CGSize, in bounds: CGRect, fitMode: Bool) -> CGRect {
        guard videoSize.width > 0, videoSize.height > 0, bounds.width > 0, bounds.height > 0 else {
            return bounds
        }
        let scaleX = bounds.width / videoSize.width
        let scaleY = bounds.height / videoSize.height
        let scale = fitMode ? min(scaleX, scaleY) : max(scaleX, scaleY)
        let w = videoSize.width * scale
        let h = videoSize.height * scale
        return CGRect(x: bounds.midX - w / 2.0, y: bounds.midY - h / 2.0, width: w, height: h)
    }
}
