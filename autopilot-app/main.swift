// Autopilot: a floating macOS navigator for a presentation that mixes slides and Handle's page autopilot.
// It reads a run of show (~/.config/autopilot/run.json): sections, each a list of items. An item is a
// slide or a talk cue (lines to say) or a demo tab (a pinned autopilot flow). Next walks the whole run
// of show and switches Chrome tabs for you; the autopilot plays each demo tab.
// Left pane: what to do and what to say now. Right pane: the run of show, the step controls, the slide.
// It talks to Handle's local bridge (127.0.0.1:4910/ext/cmd), so it works whichever app has focus.
// Global hotkeys: ⌥Space or ⌃⌥Space play/pause · ⌃⌥↓ next item · ⌃⌥↑ previous item
//                 ⌃⌥→ next step · ⌃⌥← back a step
import AppKit
import Carbon.HIToolbox
import SwiftUI

// MARK: - Bridge

struct PanelTab: Decodable, Identifiable { let id: Int; let title: String?; let group: String?; let active: Bool?; let flow: Bool?; let flowIdx: Int? }
struct SlideInfo: Decodable { let n: Int; let title: String; let image: String }
struct LastPlay: Decodable { let src: String?; let at: Double? }
struct PanelState: Decodable {
    let title: String?; let group: String?; let flow: Bool?; let tabId: Int?
    let steps: [String]?; let says: [String]?; let notes: [String]?; let slides: [SlideInfo?]?; let intro: String?; let i: Int?; let n: Int?
    let status: String?; let error: String?; let pausedBy: String?
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
        call("panel_msg", p, timeout: ["play", "toggle", "jumpplay"].contains(action) ? 600 : action == "resetall" ? 120 : 30)
    }
}

// MARK: - Run of show

struct ShowItem: Decodable {
    let kind: String            // "slide", "talk" or "tab"
    let title: String
    let flow: Int?              // pinned flow index of the tab to bring forward
    let cue: String?            // what to do
    let say: String?            // what to say (slides and talk cues; demo tabs carry their own lines)
    let note: String?           // "If asked" material
    let slide: Int?; let image: String?
}
struct ShowSection: Decodable { let title: String; let items: [ShowItem] }
struct RunOfShow: Decodable { let title: String?; let sections: [ShowSection] }

struct Entry { let section: Int; let sectionTitle: String; let item: ShowItem }

// MARK: - Model

final class Model: ObservableObject {
    static let runPath = NSString(string: "~/.config/autopilot/run.json").expandingTildeInPath
    @Published var state: PanelState?
    @Published var connected = false
    @Published var now = Date().timeIntervalSince1970 * 1000
    @Published var entries: [Entry] = []
    @Published var showTitle = ""
    @Published var pos = UserDefaults.standard.integer(forKey: "runPos")
    private var timer: Timer?
    private var runStamp: Date?
    private var lastGo = Date.distantPast

    init() {
        loadRun()
        timer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    var current: Entry? { entries.indices.contains(pos) ? entries[pos] : nil }
    var next: Entry? { entries.indices.contains(pos + 1) ? entries[pos + 1] : nil }

    // reload the run of show when the file changes
    func loadRun() {
        let stamp = (try? FileManager.default.attributesOfItem(atPath: Model.runPath)[.modificationDate]) as? Date
        guard stamp != runStamp else { return }
        runStamp = stamp
        guard let d = FileManager.default.contents(atPath: Model.runPath),
              let r = try? JSONDecoder().decode(RunOfShow.self, from: d) else { entries = []; return }
        showTitle = r.title ?? ""
        entries = r.sections.enumerated().flatMap { k, s in s.items.map { Entry(section: k, sectionTitle: s.title, item: $0) } }
        if pos >= entries.count { pos = 0 }
    }

    func tabId(forFlow f: Int?) -> Int? {
        guard let f else { return nil }
        return state?.tabs?.first { $0.flowIdx == f }?.id
    }

    // the flow index of the tab in front
    var activeFlow: Int? { state?.tabs?.first { $0.active == true }?.flowIdx }

    // the state shown is for this item's tab
    var onItemTab: Bool { current?.item.flow == nil || activeFlow == current?.item.flow }

    func go(_ k: Int) {
        guard entries.indices.contains(k) else { return }
        pos = k
        UserDefaults.standard.set(k, forKey: "runPos")
        lastGo = Date()
        if let id = tabId(forFlow: entries[k].item.flow) { Bridge.send("tab", ["tabId": id]) }
    }

    // the one button: play the demo on this tab, pause it, or move on
    func primary() {
        guard let c = current else { return }
        if c.item.kind == "tab" && onItemTab {
            switch state?.status {
            case "playing": Bridge.send("pause"); return
            case "ready", "paused": Bridge.send("play"); return
            default: break
            }
        }
        go(pos + 1)
    }

    func poll() {
        loadRun()
        Bridge.call("panel_get", timeout: 3) { data in
            let s = data.flatMap { try? JSONDecoder().decode(PanelState.self, from: $0) }
            DispatchQueue.main.async {
                self.connected = s != nil
                if let s { self.state = s }
                self.now = Date().timeIntervalSince1970 * 1000
                self.follow()
            }
        }
    }

    // when you switch tabs yourself, move the run of show to that demo tab
    // (only when the tab in front changes, so a VS Code or phone item is not pulled back to the last demo tab)
    private var seenFlow: Int?
    private func follow() {
        let f = activeFlow
        defer { seenFlow = f }
        guard Date().timeIntervalSince(lastGo) > 2.5, seenFlow != nil, f != seenFlow, let f, f != current?.item.flow else { return }
        let hits = entries.indices.filter { entries[$0].item.kind == "tab" && entries[$0].item.flow == f }
        if let k = hits.min(by: { abs($0 - pos) < abs($1 - pos) }) {
            pos = k
            UserDefaults.standard.set(k, forKey: "runPos")
        }
    }
}

// MARK: - Views

extension Array {
    subscript(safe k: Int) -> Element? { k >= 0 && k < count ? self[k] : nil }
}

func pill(_ s: String) -> (String, Color) {
    switch s {
    case "playing": return ("PLAYING", .green)
    case "paused": return ("PAUSED", .orange)
    case "ready": return ("READY", .orange)
    case "done": return ("DONE", .gray)
    case "error": return ("STOPPED", .red)
    default: return ("", .secondary)
    }
}

struct ContentView: View {
    @ObservedObject var m: Model
    var body: some View {
        HStack(spacing: 0) {
            Presenter(m: m).frame(minWidth: 460, idealWidth: 560, maxWidth: .infinity)
            Divider()
            Navigator(m: m).frame(width: 400)
        }
    }
}

// Left: where you are, what to do, what to say, and the one button
struct Presenter: View {
    @ObservedObject var m: Model

    var body: some View {
        let c = m.current
        VStack(alignment: .leading, spacing: 10) {
            if let c {
                HStack(alignment: .firstTextBaseline) {
                    Text(c.sectionTitle).font(.system(size: 22, weight: .bold))
                    Text(c.item.title).font(.system(size: 15)).foregroundColor(.secondary).lineLimit(1)
                    Spacer()
                    Text("\(m.pos + 1) / \(m.entries.count)").font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
                }
                if let cue = c.item.cue, !cue.isEmpty {
                    HStack(alignment: .top, spacing: 8) {
                        Text("DO").font(.system(size: 10, weight: .bold)).foregroundColor(.white)
                            .padding(.horizontal, 6).padding(.vertical, 3).background(Color.orange).cornerRadius(4)
                        Text(cue).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.orange.opacity(0.12)).cornerRadius(8)
                }
                if c.item.kind == "tab" {
                    if m.onItemTab { TabLines(m: m) } else { Text("Switching to the tab…").foregroundColor(.secondary); Spacer() }
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 12) {
                            Text(c.item.say ?? "").font(.system(size: 18)).fixedSize(horizontal: false, vertical: true)
                            if let n = c.item.note, !n.isEmpty {
                                Text(n).font(.system(size: 12)).italic().foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            } else {
                Text("No run of show at ~/.config/autopilot/run.json").foregroundColor(.secondary)
                Spacer()
            }
            BigButtons(m: m)
        }
        .padding(16)
    }
}

// the talk track lines of the demo tab in front, the current step's line highlighted
struct TabLines: View {
    @ObservedObject var m: Model

    var lines: [(step: Int, text: String, note: String)] {
        guard let s = m.state, s.flow == true else { return [] }
        var out: [(step: Int, text: String, note: String)] = []
        if let t = s.intro, !t.isEmpty { out.append((-1, t, "")) }
        let notes = s.notes ?? []
        for (k, t) in (s.says ?? []).enumerated() where !t.isEmpty { out.append((k, t, notes[safe: k] ?? "")) }
        return out
    }

    var body: some View {
        let s = m.state
        let i = s?.i ?? 0, n = s?.n ?? 0
        let ls = lines
        let cur = s?.status == "done" || i >= n ? ls.count - 1 : (ls.lastIndex { $0.step <= i } ?? 0)
        if ls.isEmpty {
            Text("No lines on this tab: let the screen show it.").font(.system(size: 14)).foregroundColor(.secondary)
            Spacer()
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(ls.enumerated()), id: \.offset) { k, l in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(l.step < 0 ? "Before you press Play" : "Step \(l.step + 1) · \(s?.steps?[safe: l.step] ?? "")")
                                    .font(.system(size: 10, weight: .semibold)).foregroundColor(.secondary).lineLimit(1)
                                Text(l.text).font(.system(size: k == cur ? 18 : 15, weight: k == cur ? .medium : .regular))
                                    .fixedSize(horizontal: false, vertical: true)
                                    .foregroundColor(k < cur ? .secondary : .primary)
                                if !l.note.isEmpty {
                                    Text(l.note).font(.system(size: 12)).italic().foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                            .background(k == cur ? Color.accentColor.opacity(0.16) : Color.clear)
                            .overlay(alignment: .leading) { if k == cur { Rectangle().fill(Color.accentColor).frame(width: 3) } }
                            .cornerRadius(6).id(k)
                            .contentShape(Rectangle())
                            .onTapGesture { Bridge.send("jumpplay", ["i": max(0, l.step)]) }
                            .help("Play from this step")
                        }
                    }
                }
                .onChange(of: cur) { c in withAnimation(.easeInOut(duration: 0.3)) { proxy.scrollTo(c, anchor: .top) } }
            }
        }
    }
}

// Back · the one button (Play the demo / Pause / Resume / Next: …) · Skip
struct BigButtons: View {
    @ObservedObject var m: Model

    var body: some View {
        let c = m.current
        let st = m.state?.status ?? ""
        let isTab = c?.item.kind == "tab" && m.onItemTab
        let live = isTab && ["playing", "ready", "paused"].contains(st)
        let (label, tint): (String, Color) = {
            if isTab && st == "playing" { return ("⏸  Pause", .orange) }
            if isTab && st == "ready" { return ("▶  Play the demo", .green) }
            if isTab && st == "paused" { return ("▶  Resume", .green) }
            if let n = m.next {
                let name = n.section == c?.section ? n.item.title : "\(n.sectionTitle) · \(n.item.title)"
                return ("Next:  \(name)  ▶", .accentColor)
            }
            return ("End of the run of show", .gray)
        }()
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Button { m.go(m.pos - 1) } label: { Text("◀ Back").frame(width: 80, height: 34) }
                    .controlSize(.large).disabled(m.pos == 0)
                Button { m.primary() } label: {
                    Text(label).font(.system(size: 15, weight: .semibold)).lineLimit(1).frame(maxWidth: .infinity, minHeight: 34)
                }
                .buttonStyle(.borderedProminent).tint(tint).controlSize(.large)
                .disabled(m.next == nil && !live)
                if live, m.next != nil {
                    Button { m.go(m.pos + 1) } label: { Text("Skip ▶").frame(width: 70, height: 34) }.controlSize(.large)
                        .help("Go to the next item without finishing this demo")
                }
            }
            Text("⌥Space play/pause · ⌃⌥↓ next item · ⌃⌥↑ back · ⌃⌥→ ⌃⌥← step").font(.system(size: 10)).foregroundColor(.secondary)
        }
    }
}

// Right: status and step controls, the slide, the run of show
struct Navigator: View {
    @ObservedObject var m: Model

    var body: some View {
        let s = m.state
        let c = m.current
        let isTab = c?.item.kind == "tab" && m.onItemTab
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(m.showTitle.isEmpty ? "RUN OF SHOW" : m.showTitle.uppercased())
                    .font(.system(size: 10, weight: .semibold)).foregroundColor(.secondary).lineLimit(1)
                Spacer()
                Circle().fill(m.connected ? Color.green : Color.red).frame(width: 8, height: 8)
                    .help(m.connected ? "Connected to Handle" : "Handle bridge not reachable")
            }

            if isTab {
                let (label, color) = pill(s?.status ?? "")
                let n = s?.n ?? 0, i = s?.i ?? 0
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        if !label.isEmpty {
                            Text(label).font(.system(size: 11, weight: .bold, design: .monospaced))
                                .padding(.horizontal, 8).padding(.vertical, 4).background(color.opacity(0.9)).foregroundColor(.white).clipShape(Capsule())
                        }
                        Text(i >= n ? "All \(n) steps done" : "Step \(i + 1) of \(n) · \(s?.steps?[safe: i] ?? "")")
                            .font(.system(size: 12)).lineLimit(1)
                    }
                    let frac: Double = {
                        guard s?.status == "playing", let end = s?.dwellEnd, let ms = s?.dwellMs, ms > 0, end > 0 else { return 0 }
                        return max(0, min(1, 1 - (end - m.now) / ms))
                    }()
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.secondary.opacity(0.2))
                            Capsule().fill(Color.accentColor).frame(width: g.size.width * frac)
                        }
                    }.frame(height: 4)
                    if s?.status == "paused", let p = s?.pausedBy, !p.isEmpty {
                        Text("Paused by your \(p).").font(.system(size: 11)).foregroundColor(.orange)
                    }
                    if let e = s?.error, !e.isEmpty {
                        Text("Stopped: \(e). Do it by hand, then Step ⏭ or Resume.").font(.system(size: 11)).foregroundColor(.red)
                    }
                    HStack(spacing: 4) {
                        Button("⏮ Step") { Bridge.send("prev") }
                        Button("Step ⏭") { Bridge.send("next") }
                        Button("⟲ Restart this tab") { Bridge.send("stop") }
                    }.controlSize(.small)
                }
                .padding(10).background(Color.secondary.opacity(0.08)).cornerRadius(8)
            }

            if let c, c.item.kind == "slide" {
                VStack(alignment: .leading, spacing: 6) {
                    Text(c.item.title).font(.system(size: 13, weight: .semibold)).lineLimit(2)
                    if let p = c.item.image, let img = NSImage(contentsOfFile: p) {
                        Image(nsImage: img).resizable().aspectRatio(contentMode: .fit)
                            .cornerRadius(4).overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
                    }
                }
                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.secondary.opacity(0.08)).cornerRadius(8)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(m.entries.enumerated()), id: \.offset) { k, e in
                            if k == 0 || m.entries[k - 1].section != e.section {
                                Text(e.sectionTitle).font(.system(size: 13, weight: .bold))
                                    .padding(.top, k == 0 ? 0 : 8).padding(.bottom, 2)
                                    .foregroundColor(e.section == m.current?.section ? .primary : .secondary)
                            }
                            HStack(spacing: 8) {
                                Text(e.item.kind == "slide" ? "▭" : e.item.kind == "tab" ? "▶" : "✎")
                                    .font(.system(size: 11)).foregroundColor(.secondary).frame(width: 14)
                                Text(e.item.title).font(.system(size: 12)).lineLimit(1)
                                Spacer()
                            }
                            .padding(.vertical, 4).padding(.horizontal, 6)
                            .background(k == m.pos ? Color.accentColor.opacity(0.2) : Color.clear).cornerRadius(5)
                            .foregroundColor(k < m.pos ? .secondary : .primary)
                            .contentShape(Rectangle())
                            .onTapGesture { m.go(k) }
                            .id(k)
                        }
                    }
                }
                .onChange(of: m.pos) { p in withAnimation { proxy.scrollTo(p, anchor: .center) } }
            }

            HStack(spacing: 6) {
                Button("⟲ Reset all tabs") { Bridge.send("resetall") }
                    .help("Every demo tab back to its start page and step 1")
                Button("⇤ Start from the top") { Bridge.send("resetall"); m.go(0) }
                    .help("Reset all tabs, then go to the first item")
            }.controlSize(.small)
        }
        .padding(14)
    }
}

// MARK: - Hotkeys

final class Hotkeys {
    private var refs: [EventHotKeyRef?] = []
    private static var actions: [UInt32: () -> Void] = [:]

    init(_ m: Model) {
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
        add(6, kVK_Space, UInt32(optionKey)) { Bridge.send("toggle") }  // ⌥Space: pause, press again to resume
        add(2, kVK_RightArrow, mods) { Bridge.send("next") }
        add(3, kVK_LeftArrow, mods) { Bridge.send("prev") }
        add(4, kVK_DownArrow, mods) { DispatchQueue.main.async { m.go(m.pos + 1) } }
        add(5, kVK_UpArrow, mods) { DispatchQueue.main.async { m.go(m.pos - 1) } }
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

// the panel takes key status on a click (without activating the app), so the first click on a button counts
final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class FirstClickHostingView<V: View>: NSHostingView<V> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: NSPanel!
    var item: NSStatusItem!
    var hotkeys: Hotkeys!
    let model = Model()

    func applicationDidFinishLaunching(_ note: Notification) {
        panel = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 720),
                         styleMask: [.titled, .closable, .resizable, .nonactivatingPanel, .utilityWindow],
                         backing: .buffered, defer: false)
        panel.title = "Autopilot"
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.sharingType = .none  // left out of screenshots and screen sharing that honour it
        panel.contentView = FirstClickHostingView(rootView: ContentView(m: model))
        panel.setFrameAutosaveName("AutopilotPanel2")
        // keep the whole window on one screen; if it hangs off an edge, put it top-right of the menu-bar display
        if !NSScreen.screens.contains(where: { $0.visibleFrame.contains(panel.frame) }), let scr = NSScreen.screens.first {
            let f = scr.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: f.maxX - panel.frame.width - 20, y: f.maxY - 20))
        }
        panel.orderFrontRegardless()

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "AP"
        let menu = NSMenu()
        menu.addItem(withTitle: "Show Autopilot", action: #selector(show), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu

        hotkeys = Hotkeys(model)
    }

    @objc func show() { panel.orderFrontRegardless() }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { show(); return true }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
