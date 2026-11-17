// "Why is this slow?" as a Mac app: a window showing the local dashboard, plus a menu-bar item.
// All recording and serving stays in slow.py, which this app runs as a child process and watches.
// The app itself opens no listening sockets. Built by ../build_app.sh (swiftc, no Xcode project).
import AppKit
import ServiceManagement
import WebKit

let base = URL(string: "http://127.0.0.1:8765/")!

enum Probe { case ours([String: Any]), foreign, nothing }

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    var loginItem: NSMenuItem!
    var quitItem: NSMenuItem!

    var child: Process?        // the recorder, when this app started it (nil when attached to another one)
    let lifeline = Pipe()      // the child's stdin: it closes when this app dies, however it dies, and the child exits
    var stderrTail = Data()
    var started = Date()
    var lastError: String?     // why the recorder failed at start; stops the app retrying on its own
    var misses = 0             // status probes in a row without a recorder answering
    var quitting = false
    var marked = false

    // MARK: launch

    func applicationDidFinishLaunching(_ note: Notification) {
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())  // default store: localStorage keeps the theme
        web.navigationDelegate = self
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Why is this slow?"
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("main")
        buildMenus()
        showWindow()
        startRecorder()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in self.tick() }
    }

    func buildMenus() {
        func menu(_ title: String, _ items: [(String, Selector, String)]) -> NSMenuItem {
            let top = NSMenuItem(), sub = NSMenu(title: title)
            for (title, action, key) in items { sub.addItem(withTitle: title, action: action, keyEquivalent: key) }
            top.submenu = sub
            return top
        }
        let main = NSMenu()  // nil targets: actions go down the responder chain, which ends at this delegate
        main.addItem(menu("App", [("About Why is this slow?", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
                                  ("Hide Why is this slow?", #selector(NSApplication.hide(_:)), "h"),
                                  ("Quit Why is this slow?", #selector(NSApplication.terminate(_:)), "q")]))
        main.addItem(menu("File", [("Close Window", #selector(NSWindow.performClose(_:)), "w")]))
        main.addItem(menu("Edit", [("Copy", #selector(NSText.copy(_:)), "c"), ("Select All", #selector(NSText.selectAll(_:)), "a")]))
        main.addItem(menu("View", [("Reload", #selector(reload), "r")]))
        main.addItem(menu("Window", [("Show Dashboard", #selector(showWindow), "")]))
        NSApp.mainMenu = main

        let m = NSMenu()
        m.autoenablesItems = false
        m.delegate = self
        func add(_ title: String, _ action: Selector) -> NSMenuItem {
            let item = m.addItem(withTitle: title, action: action, keyEquivalent: "")
            item.target = self
            return item
        }
        _ = add("Open Dashboard", #selector(showWindow))
        _ = add("It's Slow Now", #selector(mark))
        statusLine.isEnabled = false
        m.addItem(statusLine)
        m.addItem(.separator())
        loginItem = add("Start at Login", #selector(toggleLogin))
        m.addItem(.separator())
        quitItem = m.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        status.menu = m
        status.button?.toolTip = "Free memory"
        setStatus("○", .secondaryLabelColor, "")
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        // a login item pointing into a build folder would break on the next rebuild or move
        let path = Bundle.main.bundlePath
        loginItem.isEnabled = path.hasPrefix("/Applications/") || path.hasPrefix(NSHomeDirectory() + "/Applications/")
        quitItem.title = child != nil ? "Quit (stops recording)" : "Quit"
    }

    // MARK: recorder

    func probe(_ done: @escaping (Probe) -> Void) {
        var request = URLRequest(url: base.appendingPathComponent("api/live"))
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, response, _ in
            var result = Probe.nothing
            if let http = response as? HTTPURLResponse {
                result = .foreign
                if http.statusCode == 200, let data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   json["samples"] != nil, json["last_sample_age"] != nil {
                    result = .ours(json)
                }
            }
            DispatchQueue.main.async { done(result) }
        }.resume()
    }

    /// Attach to a recorder that is already running (launchd agent, a terminal), else start our own.
    func startRecorder() {
        lastError = nil
        probe { result in
            if case .ours = result { return self.load() }
            if self.child == nil { self.spawn() }
        }
    }

    func spawn() {
        let p = Process(), err = Pipe()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")  // never PATH: Anaconda must not be picked up
        // -B: no __pycache__ in Resources, which would break the code signature
        p.arguments = ["-B", "-E", "-s", Bundle.main.resourcePath! + "/slow.py", "record"]
        p.environment = ProcessInfo.processInfo.environment.merging(["SLOW_STOP_ON_EOF": "1"]) { $1 }
        p.standardInput = lifeline
        p.standardOutput = FileHandle.nullDevice
        p.standardError = err
        stderrTail = Data()
        err.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { return handle.readabilityHandler = nil }
            DispatchQueue.main.async { self.stderrTail = (self.stderrTail + data).suffix(4096) }
        }
        // a moment later, so the last stderr lines have arrived
        p.terminationHandler = { _ in DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.childExited(p) } }
        do { try p.run() } catch { return fail("Can't start the recorder", error.localizedDescription) }
        child = p
        started = Date()
        waitForServer(p, until: Date() + 15)
    }

    func waitForServer(_ p: Process, until deadline: Date) {
        guard child === p else { return }  // it exited: childExited shows why
        probe { result in
            if case .ours = result {
                self.load()
            } else if Date() < deadline {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForServer(p, until: deadline) }
            } else if case .foreign = result {
                self.fail("Port 8765 is used by another program",
                          "Recording continues, but the dashboard can't be shown. Free the port, then View > Reload.")
            } else {
                self.fail("Can't reach the recorder", "It started but its dashboard is not answering. Try View > Reload.")
            }
        }
    }

    func childExited(_ p: Process) {
        guard child === p else { return }
        child = nil
        if quitting { return }
        if Date().timeIntervalSince(started) > 10 {  // it had been working: try again once, not in a tight loop
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { if self.child == nil && !self.quitting { self.startRecorder() } }
        } else {
            let tail = String(decoding: stderrTail, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            lastError = tail.split(separator: "\n").last.map(String.init) ?? "the recorder exited (status \(p.terminationStatus))"
            fail("The recorder stopped", tail.isEmpty ? lastError! : tail)
            tick()
        }
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        guard let p = child, p.isRunning else { return }  // an attached recorder is left running
        p.terminate()
        for _ in 0..<60 where p.isRunning { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    // MARK: menu-bar item

    func setStatus(_ dot: String, _ color: NSColor, _ text: String) {
        if marked { return }
        let title = NSMutableAttributedString(string: dot, attributes: [.foregroundColor: color])
        title.append(NSAttributedString(string: text))
        let font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.menuBarFont(ofSize: 0).pointSize, weight: .regular)
        title.addAttribute(.font, value: font, range: NSRange(location: 0, length: title.length))
        status.button?.attributedTitle = title
    }

    func tick() {
        probe { result in
            guard case .ours(let json) = result else {
                self.setStatus("○", .secondaryLabelColor, "")
                self.statusLine.title = self.child != nil ? "Recording (this app), dashboard unavailable"
                    : "Not recording: " + (self.lastError ?? "no recorder is answering")
                self.misses += 1
                if self.child == nil && self.lastError == nil && self.misses >= 3 {  // the other recorder went away: take over
                    self.misses = 0
                    self.startRecorder()
                }
                return
            }
            self.misses = 0
            let age = json["last_sample_age"] as? Double ?? 0  // null until the first sample
            let free = (json["samples"] as? [[String: Any]])?.last?["mem_free"] as? Double
            self.setStatus("●", age < 20 ? .systemGreen : .systemOrange,
                           " " + (free.map { String(format: "%.1f GB", $0 / 1_073_741_824) } ?? "…"))
            self.statusLine.title = age >= 20 ? "Stalled: last sample \(Int(age))s ago"
                : self.child != nil ? "Recording (this app)" : "Recording (another recorder)"
        }
    }

    @objc func mark() {
        var request = URLRequest(url: base.appendingPathComponent("api/mark"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(#"{"note":"menu bar"}"#.utf8)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            DispatchQueue.main.async {
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { return NSSound.beep() }
                self.status.button?.title = "MARKED"
                self.marked = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.marked = false; self.tick() }
            }
        }.resume()
    }

    @objc func toggleLogin() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled { try service.unregister() } else { try service.register() }
        } catch {
            NSAlert(error: error).runModal()
        }
        if service.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
    }

    // MARK: window

    @objc func showWindow() {
        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)  // the Dock icon goes; recording continues from the menu bar
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow()
        return false
    }

    // MARK: web view

    func load() {
        web.load(URLRequest(url: base))
        tick()
    }

    /// Reload the page; when this app has no recorder of its own, first look for one again or start one.
    @objc func reload() {
        if child == nil { startRecorder() } else { load() }
    }

    func fail(_ title: String, _ detail: String) {
        let text = detail.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        web.loadHTMLString("<body style='font:14px -apple-system;background:#2a1552;color:#fff;padding:40px'>"
                           + "<h2>\(title)</h2><pre style='white-space:pre-wrap'>\(text)</pre>", baseURL: nil)
    }

    func openInBrowser(_ url: URL?) {
        if let url, ["http", "https"].contains(url.scheme) { NSWorkspace.shared.open(url) }
    }

    /// The window only ever shows the local dashboard (or our own error page); links elsewhere go to the browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = action.request.url
        if url?.scheme == "about" || (url?.scheme == "http" && url?.host == "127.0.0.1" && url?.port == 8765) {
            return decisionHandler(.allow)
        }
        if action.navigationType == .linkActivated { openInBrowser(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        openInBrowser(action.request.url)
        return nil  // no popups
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { load() }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let e = error as NSError
        // not cancellations: those are our own policy above, or a newer load replacing this one
        if e.domain == NSURLErrorDomain && e.code != NSURLErrorCancelled { fail("Can't reach the recorder", e.localizedDescription) }
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
