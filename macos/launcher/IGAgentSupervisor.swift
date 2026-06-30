#!/usr/bin/env swift
/*
 IG Agent v41 — macOS-native one-click supervisor.
 Runs: agent_kill → agent_start → agent_verify → open dashboard.
 Work runs off the main thread so double-click never shows "Not Responding".

 Build: macos/supervisor/build_swift.sh
*/

import AppKit
import Foundation
import UserNotifications

// MARK: - Notifications

func requestNotificationAuth() {
    let center = UNUserNotificationCenter.current()
    center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
}

func notify(_ title: String, _ body: String, critical: Bool = false) {
    let content = UNMutableNotificationContent()
    content.title = title
    content.body = body
    content.sound = critical ? .defaultCritical : .default
    let req = UNNotificationRequest(
        identifier: UUID().uuidString,
        content: content,
        trigger: nil
    )
    UNUserNotificationCenter.current().add(req)
    fputs("[IGAgentSupervisor] \(title): \(body)\n", stderr)
}

// MARK: - Progress UI (main thread only)

final class LaunchProgressPanel {
    private let window: NSWindow
    private let statusLabel: NSTextField
    private let detailLabel: NSTextField

    init() {
        let rect = NSRect(x: 0, y: 0, width: 420, height: 140)
        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "IG Agent"
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.center()

        let stack = NSStackView(frame: NSRect(x: 20, y: 20, width: 380, height: 100))
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8

        statusLabel = NSTextField(labelWithString: "Launching IG Agent…")
        statusLabel.font = NSFont.boldSystemFont(ofSize: 15)

        detailLabel = NSTextField(labelWithString: "Preparing clean start")
        detailLabel.font = NSFont.systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byWordWrapping
        detailLabel.maximumNumberOfLines = 3
        detailLabel.preferredMaxLayoutWidth = 380

        stack.addArrangedSubview(statusLabel)
        stack.addArrangedSubview(detailLabel)
        window.contentView = stack
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func update(status: String, detail: String) {
        statusLabel.stringValue = status
        detailLabel.stringValue = detail
    }

    func close() {
        window.orderOut(nil)
    }
}

// MARK: - Project root

func isProjectRoot(_ path: String) -> Bool {
    let fm = FileManager.default
    return fm.fileExists(atPath: "\(path)/scripts/start.sh")
        && fm.fileExists(atPath: "\(path)/src/main.py")
}

func findProjectRoot() -> String? {
    if let env = ProcessInfo.processInfo.environment["IG_AGENT_ROOT"], isProjectRoot(env) {
        return (env as NSString).standardizingPath
    }

    if let exe = Bundle.main.executablePath {
        var dir = (exe as NSString).deletingLastPathComponent
        for _ in 0..<10 {
            if isProjectRoot(dir) { return dir }
            if dir.hasSuffix("/Contents/MacOS") {
                let candidate = (dir as NSString).appendingPathComponent("../../../../")
                let resolved = (candidate as NSString).standardizingPath
                if isProjectRoot(resolved) { return resolved }
            }
            let parent = (dir as NSString).deletingLastPathComponent
            if parent == dir { break }
            dir = parent
        }
    }

    let cwd = FileManager.default.currentDirectoryPath
    var dir = cwd
    for _ in 0..<10 {
        if isProjectRoot(dir) { return dir }
        let parent = (dir as NSString).deletingLastPathComponent
        if parent == dir { break }
        dir = parent
    }

    let home = NSHomeDirectory()
    for d in ["\(home)/Projects/IG_Agent_v25", "/Users/chrisgordon/Projects/IG_Agent_v25"]
        where isProjectRoot(d) { return d }
    return nil
}

// MARK: - Script runner

@discardableResult
func runScript(root: String, name: String, required: Bool) -> Int32 {
    let launcher = (root as NSString).appendingPathComponent("macos/launcher")
    let script = (launcher as NSString).appendingPathComponent(name)
    let fm = FileManager.default
    guard fm.isExecutableFile(atPath: script) else {
        fputs("ERROR: missing script \(script)\n", stderr)
        return 127
    }

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/bash")
    proc.arguments = [script]
    proc.currentDirectoryURL = URL(fileURLWithPath: root)
    var env = ProcessInfo.processInfo.environment
    env["IG_AGENT_ROOT"] = root
    env["PYTHONPATH"] = (root as NSString).appendingPathComponent("src")
    env["APP_MODE"] = env["APP_MODE"] ?? "DEMO"
    proc.environment = env

    let logPath = (root as NSString).appendingPathComponent("logs/supervisor_swift.log")
    fm.createFile(atPath: logPath, contents: nil)
    if let handle = FileHandle(forWritingAtPath: logPath) {
        handle.seekToEndOfFile()
        proc.standardOutput = handle
        proc.standardError = handle
    }

    fputs("==> \(name)\n", stderr)
    do {
        try proc.run()
        proc.waitUntilExit()
    } catch {
        fputs("ERROR running \(name): \(error)\n", stderr)
        return 127
    }

    if proc.terminationStatus != 0 && required {
        notify("IG Agent", "Failed at \(name)", critical: true)
    }
    return proc.terminationStatus
}

func openDashboard(port: Int) {
    let url = URL(string: "http://127.0.0.1:\(port)/")!
    NSWorkspace.shared.open(url)
}

// MARK: - Main

let port = Int(ProcessInfo.processInfo.environment["IG_API_PORT"] ?? "8080") ?? 8080
let panel = LaunchProgressPanel()

let app = NSApplication.shared
app.setActivationPolicy(.regular)
requestNotificationAuth()

DispatchQueue.main.async {
    panel.show()
    panel.update(status: "Launching IG Agent…", detail: "Resolving project root")
}

DispatchQueue.global(qos: .userInitiated).async {
    guard let root = findProjectRoot() else {
        DispatchQueue.main.async {
            panel.update(status: "Launch failed", detail: "Project root not found — set IG_AGENT_ROOT")
            notify("IG Agent", "Project root not found — set IG_AGENT_ROOT", critical: true)
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { NSApp.terminate(nil) }
        }
        return
    }

    func ui(_ status: String, _ detail: String) {
        DispatchQueue.main.async { panel.update(status: status, detail: detail) }
    }

    notify("IG Agent", "Clean launch starting…")
    ui("Stopping old processes…", "Freeing port \(port) and clearing locks")
    if runScript(root: root, name: "agent_kill.sh", required: true) != 0 {
        DispatchQueue.main.async { NSApp.terminate(2) }
        return
    }

    notify("IG Agent", "Running tests and starting agent…")
    ui("Running test gate…", "This can take up to 10 minutes on first launch today")
    if runScript(root: root, name: "agent_start.sh", required: true) != 0 {
        DispatchQueue.main.async { NSApp.terminate(3) }
        return
    }

    notify("IG Agent", "Verifying GUI status…")
    ui("Verifying dashboard…", "Checking /api/gui_status")
    if runScript(root: root, name: "agent_verify.sh", required: true) != 0 {
        DispatchQueue.main.async { NSApp.terminate(4) }
        return
    }

    ui("Opening cockpit…", "Launching dashboard")
    _ = runScript(root: root, name: "agent_gui.sh", required: false)

    DispatchQueue.main.async {
        openDashboard(port: port)
        notify("IG Agent", "Agent ready on port \(port)")
        fputs("✅ IGAgentSupervisor complete\n", stderr)
        panel.close()
        NSApp.terminate(0)
    }
}

app.run()
