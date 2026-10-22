// Autopilot: a floating macOS controller for Handle's page autopilot.
// It reads the autopilot state and sends controls through Handle's local
// bridge (127.0.0.1:4910/ext/cmd), so it works whichever app has focus.
// Global hotkeys: ⌃⌥Space play/pause · ⌃⌥→ next step · ⌃⌥← back a step
//                 ⌃⌥↓ next tab · ⌃⌥↑ previous tab
import AppKit
import Carbon.HIToolbox
import SwiftUI

// MARK: - Bridge

struct PanelTab: Decodable, Identifiable { let id: Int; let title: String?; let group: String?; let active: Bool?; let flow: Bool? }
struct LastPlay: Decodable { let src: String?; let at: Double? }
struct PanelState: Decodable {
    let title: String?; let group: String?; let flow: Bool?
    let steps: [String]?; let i: Int?; let n: Int?
    let status: String?; let error: String?
    let dwellEnd: Double?; let dwellMs: Double?
    let tabs: [PanelTab]?; let lastPlay: LastPlay?
}

enum Bridge {
    static let url = URL(string: "http://127.0.0.1:4910/ext/cmd")!

    static func call(_ method: String, _ params: [String: Any] = [:], timeout: Double = 20,
                     done: ((Data?) -> Void)? = nil) {
        var req = URLRequest(url: url, timeoutInterval: timeout + 5)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["method": method, "params": params, "timeout": timeout])
        URLSession.shared.dataTask(with: req) { data, _, _ in done?(data) }.resume()
    }

    static func send(_ action: String, _ extra: [String: Any] = [:]) {
        var p = extra; p["panel"] = action; p["src"] = "app"
        // play runs until the page is done, so give it a long timeout; the state poll shows progress
        call("panel_msg", p, timeout: action == "play" || action == "toggle" ? 600 : 30)
    }
}

// MARK: - Model

final class Model: ObservableObject {
    @Published var state: PanelState?
    @Published var connected = false
    @Published var now = Date().timeIntervalSince1970 * 1000
    private var timer: Timer?

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    func poll() {
        Bridge.call("panel_get", timeout: 3) { data in
            let s = data.flatMap { try? JSONDecoder().decode(PanelState.self, from: $0) }
            DispatchQueue.main.async {
                self.connected = s != nil
                if let s { self.state = s }
                self.now = Date().timeIntervalSince1970 * 1000
            }
        }
    }
}

// MARK: - View

struct ContentView: View {
    @ObservedObject var m: Model

    var status: String { m.state?.status ?? "none" }
    var playing: Bool { status == "playing" }

    func pill(_ s: String) -> (String, Color) {
        switch s {
        case "playing": return ("PLAYING", .green)
        case "paused": return ("PAUSED", .orange)
        case "ready": return ("READY", .orange)
        case "done": return ("DONE", .gray)
        case "error": return ("STOPPED", .red)
        default: return ("NO FLOW", .secondary)
        }
    }

    var body: some View {
        let s = m.state
        let n = s?.n ?? 0, i = s?.i ?? 0
        let (label, color) = pill(status)
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Text(label).font(.system(size: 12, weight: .bold, design: .monospaced))
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(color.opacity(status == "none" ? 0.15 : 0.9)).foregroundColor(.white)
                    .clipShape(Capsule())
                VStack(alignment: .leading, spacing: 1) {
                    Text(s?.title ?? "—").font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    Text(s?.group ?? "").font(.system(size: 11)).foregroundColor(.secondary)
                }
                Spacer()
                Circle().fill(m.connected ? Color.green : Color.red).frame(width: 8, height: 8)
                    .help(m.connected ? "Connected to Handle" : "Handle bridge not reachable")
            }

            VStack(alignment: .leading, spacing: 4) {
                if s?.flow != true {
                    Text("No flow on this page").font(.system(size: 12)).foregroundColor(.secondary)
                } else if i >= n {
                    Text("All \(n) steps done").font(.system(size: 12)).foregroundColor(.secondary)
                    Text("Next tab when you're ready").font(.system(size: 15, weight: .semibold))
                } else {
                    Text("Step \(i + 1) of \(n)\(playing ? "" : " · next up")").font(.system(size: 12)).foregroundColor(.secondary)
                    Text(s?.steps?[i] ?? "").font(.system(size: 15, weight: .semibold)).lineLimit(2)
                }
                let frac: Double = {
                    guard playing, let end = s?.dwellEnd, let ms = s?.dwellMs, ms > 0, end > 0 else { return 0 }
                    return max(0, min(1, 1 - (end - m.now) / ms))
                }()
                GeometryReader { g in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.secondary.opacity(0.2))
                        Capsule().fill(Color.accentColor).frame(width: g.size.width * frac)
                    }
                }.frame(height: 4)
                if let e = s?.error, !e.isEmpty {
                    Text("Stopped: \(e). Do it by hand, then Next or Play.").font(.system(size: 11)).foregroundColor(.red)
                }
            }
            .padding(10).background(Color.secondary.opacity(0.08)).cornerRadius(8)

            HStack(spacing: 6) {
                Button { Bridge.send("prev") } label: { Text("⏮ Back").frame(maxWidth: .infinity) }
                Button { Bridge.send(playing ? "pause" : "play") } label: {
                    Text(playing ? "⏸ Pause" : "▶ Play").bold().frame(maxWidth: .infinity)
                }.tint(playing ? .orange : .green).buttonStyle(.borderedProminent)
                Button { Bridge.send("next") } label: { Text("⏭ Next").frame(maxWidth: .infinity) }
                Button { Bridge.send("stop") } label: { Text("⟲ Reset").frame(maxWidth: .infinity) }
            }.controlSize(.large)
            HStack(spacing: 6) {
                Button { Bridge.send("tab", ["delta": -1]) } label: { Text("◀ Previous tab").frame(maxWidth: .infinity) }
                Button { Bridge.send("tab", ["delta": 1]) } label: { Text("Next tab ▶").frame(maxWidth: .infinity) }
            }

            Text("STEPS · click to jump").font(.system(size: 10, weight: .semibold)).foregroundColor(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array((s?.steps ?? []).enumerated()), id: \.offset) { k, t in
                        HStack(spacing: 8) {
                            Text("\(k + 1)").font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary).frame(width: 18)
                            Text(t).font(.system(size: 12)).lineLimit(1)
                            Spacer()
                        }
                        .padding(.vertical, 4).padding(.horizontal, 6)
                        .background(k == i ? Color.accentColor.opacity(0.18) : Color.clear).cornerRadius(5)
                        .foregroundColor(k < i ? .secondary : .primary)
                        .contentShape(Rectangle())
                        .onTapGesture { Bridge.send("jump", ["i": k]) }
                    }
                }
            }.frame(minHeight: 120)

            Text("TABS · click to switch").font(.system(size: 10, weight: .semibold)).foregroundColor(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(s?.tabs ?? []) { t in
                        HStack(spacing: 8) {
                            Text(shortTitle(t.title)).font(.system(size: 12)).lineLimit(1)
                            Spacer()
                            Text(t.group ?? "").font(.system(size: 10)).foregroundColor(.secondary)
                        }
                        .padding(.vertical, 4).padding(.horizontal, 6)
                        .background(t.active == true ? Color.accentColor.opacity(0.18) : Color.clear).cornerRadius(5)
                        .opacity(t.flow == true ? 1 : 0.5)
                        .contentShape(Rectangle())
                        .onTapGesture { Bridge.send("tab", ["tabId": t.id]) }
                    }
                }
            }.frame(minHeight: 120)

            Text(hotkeyLine).font(.system(size: 10)).foregroundColor(.secondary)
            if let lp = s?.lastPlay, let src = lp.src, !src.isEmpty, let at = lp.at {
                Text("Last play started by: \(src), \(Int((m.now - at) / 1000))s ago").font(.system(size: 10)).foregroundColor(.secondary)
            }
        }
        .padding(14)
        .frame(width: 380)
    }

    var hotkeyLine: String { "⌃⌥Space play/pause · ⌃⌥→ next · ⌃⌥← back · ⌃⌥↓ ⌃⌥↑ tabs" }

    func shortTitle(_ t: String?) -> String {
        guard let t else { return "—" }
        return t.replacingOccurrences(of: #" [-|] ServiceNow.*$"#, with: "", options: .regularExpression)
    }
}

// MARK: - Hotkeys

final class Hotkeys {
    private var refs: [EventHotKeyRef?] = []
    private static var actions: [UInt32: () -> Void] = [:]

    init() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hk = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hk)
            Hotkeys.actions[hk.id]?()
            return noErr
        }, 1, &spec, nil, nil)
        let mods = UInt32(controlKey | optionKey)
        add(1, kVK_Space, mods) { Bridge.send("toggle") }
        add(2, kVK_RightArrow, mods) { Bridge.send("next") }
        add(3, kVK_LeftArrow, mods) { Bridge.send("prev") }
        add(4, kVK_DownArrow, mods) { Bridge.send("tab", ["delta": 1]) }
        add(5, kVK_UpArrow, mods) { Bridge.send("tab", ["delta": -1]) }
    }

    private func add(_ id: UInt32, _ key: Int, _ mods: UInt32, _ f: @escaping () -> Void) {
        Hotkeys.actions[id] = f
        var ref: EventHotKeyRef?
        let st = RegisterEventHotKey(UInt32(key), mods, EventHotKeyID(signature: OSType(0x4150_4C54), id: id),
                                     GetApplicationEventTarget(), 0, &ref)
        if st != noErr { NSLog("Autopilot: hotkey \(id) not registered (\(st))") }
        refs.append(ref)
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: NSPanel!
    var item: NSStatusItem!
    var hotkeys: Hotkeys!
    let model = Model()

    func applicationDidFinishLaunching(_ note: Notification) {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 380, height: 640),
                        styleMask: [.titled, .closable, .resizable, .nonactivatingPanel, .utilityWindow],
                        backing: .buffered, defer: false)
        panel.title = "Autopilot"
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.contentView = NSHostingView(rootView: ContentView(m: model))
        panel.setFrameAutosaveName("AutopilotPanel")
        // first launch: top-right of the menu-bar display, never the shared demo screen
        if !panel.setFrameUsingName("AutopilotPanel"), let scr = NSScreen.screens.first {
            let f = scr.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: f.maxX - 400, y: f.maxY - 20))
        }
        panel.orderFrontRegardless()

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "AP"
        let menu = NSMenu()
        menu.addItem(withTitle: "Show Autopilot", action: #selector(show), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu

        hotkeys = Hotkeys()
    }

    @objc func show() { panel.orderFrontRegardless() }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { show(); return true }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
