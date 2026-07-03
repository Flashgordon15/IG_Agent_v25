#!/usr/bin/env swift
/*
 IG Agent v41 — macOS-native one-click supervisor.
 Isolated pipeline: agent_kill → agent_start → agent_verify → agent_gui
 Splash polls logs/launcher_status.json for live stage updates.

 Build: macos/supervisor/build_swift.sh
*/

import AppKit
import Foundation
import UserNotifications

// MARK: - Notifications

/// UNUserNotificationCenter requires a real .app bundle; CLI runs must log only.
func notificationsAvailable() -> Bool {
    Bundle.main.bundlePath.hasSuffix(".app")
}

func requestNotificationAuth() {
    guard notificationsAvailable() else { return }
    let center = UNUserNotificationCenter.current()
    center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
}

func notify(_ title: String, _ body: String, critical: Bool = false) {
    fputs("[IGAgentSupervisor] \(title): \(body)\n", stderr)
    guard notificationsAvailable() else { return }
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
}

// MARK: - Launcher status (logs/launcher_status.json)

struct LauncherStatus {
    let stage: String
    let step: Int
    let totalSteps: Int
    let status: String
    let detail: String
    let ok: Bool
    let error: String?
    let bootTier: String
}

func readLauncherStatus(root: String) -> LauncherStatus? {
    let path = (root as NSString).appendingPathComponent("logs/launcher_status.json")
    guard let data = FileManager.default.contents(atPath: path),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return nil
    }
    return LauncherStatus(
        stage: json["stage"] as? String ?? "",
        step: json["step"] as? Int ?? 0,
        totalSteps: json["total_steps"] as? Int ?? 9,
        status: json["status"] as? String ?? "",
        detail: json["detail"] as? String ?? "",
        ok: json["ok"] as? Bool ?? true,
        error: json["error"] as? String,
        bootTier: json["boot_tier"] as? String ?? ""
    )
}

// MARK: - Progress UI (main thread only)

final class LaunchProgressPanel {
    private let window: NSWindow
    private let stepLabel: NSTextField
    private let statusLabel: NSTextField
    private let detailLabel: NSTextField
    private let progress: NSProgressIndicator

    init() {
        let rect = NSRect(x: 0, y: 0, width: 480, height: 200)
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

        let stack = NSStackView(frame: NSRect(x: 24, y: 24, width: 432, height: 152))
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10

        stepLabel = NSTextField(labelWithString: "Step 0 / 9")
        stepLabel.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        stepLabel.textColor = .tertiaryLabelColor

        statusLabel = NSTextField(labelWithString: "Launching IG Agent…")
        statusLabel.font = NSFont.boldSystemFont(ofSize: 16)

        detailLabel = NSTextField(labelWithString: "Preparing isolated clean boot")
        detailLabel.font = NSFont.systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byWordWrapping
        detailLabel.maximumNumberOfLines = 4
        detailLabel.preferredMaxLayoutWidth = 432

        progress = NSProgressIndicator()
        progress.style = .bar
        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 9
        progress.doubleValue = 0
        progress.frame.size = NSSize(width: 432, height: 8)

        stack.addArrangedSubview(stepLabel)
        stack.addArrangedSubview(statusLabel)
        stack.addArrangedSubview(detailLabel)
        stack.addArrangedSubview(progress)
        window.contentView = stack
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func update(status: String, detail: String, step: Int = 0, total: Int = 9, failed: Bool = false, bootTier: String = "") {
        stepLabel.stringValue = "Step \(step) / \(total)"
        statusLabel.stringValue = status
        detailLabel.stringValue = detail
        progress.maxValue = Double(total)
        progress.doubleValue = Double(min(step, total))
        if failed {
            statusLabel.textColor = .systemRed
            progress.isIndeterminate = false
        } else if bootTier == "amber" {
            statusLabel.textColor = .systemOrange
        } else if step >= total || bootTier == "green" {
            statusLabel.textColor = .systemGreen
        } else {
            statusLabel.textColor = .labelColor
        }
    }

    func applyStatus(_ s: LauncherStatus) {
        let failed = s.stage == "failed" || !s.ok
        let step = failed ? s.step : max(s.step, 1)
        update(
            status: s.status.isEmpty ? "Working…" : s.status,
            detail: s.error ?? s.detail,
            step: step,
            total: s.totalSteps,
            failed: failed,
            bootTier: s.bootTier
        )
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
func runScript(root: String, name: String, required: Bool, supervisorPid: Int32) -> Int32 {
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
    env["LAUNCHER_DESKTOP"] = "1"
    env["LAUNCHER_SUPERVISOR_PID"] = "\(supervisorPid)"
    env["IG_AGENT_FROM_LAUNCHER"] = "1"
    env["IG_AGENT_DESKTOP_LAUNCH"] = "1"
    env["IG_NON_BLOCKING_BOOT"] = "1"
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
let supervisorPid = ProcessInfo.processInfo.processIdentifier
let panel = LaunchProgressPanel()

let app = NSApplication.shared
app.setActivationPolicy(.regular)
requestNotificationAuth()

DispatchQueue.main.async {
    panel.show()
    panel.update(status: "Launching IG Agent…", detail: "Resolving project root", step: 0, total: 9)
}

var statusPollTimer: Timer?

func startStatusPoll(root: String) {
    DispatchQueue.main.async {
        statusPollTimer?.invalidate()
        statusPollTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { _ in
            if let s = readLauncherStatus(root: root) {
                panel.applyStatus(s)
            }
        }
    }
}

func stopStatusPoll() {
    DispatchQueue.main.async {
        statusPollTimer?.invalidate()
        statusPollTimer = nil
    }
}

func finishFailure(root: String?, message: String, detail: String) {
    stopStatusPoll()
    DispatchQueue.main.async {
        if let root, let s = readLauncherStatus(root: root), s.stage == "failed" {
            panel.applyStatus(s)
        } else {
            panel.update(status: message, detail: detail, step: 0, total: 9, failed: true)
        }
        notify("IG Agent", detail, critical: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { NSApp.terminate(1) }
    }
}

DispatchQueue.global(qos: .userInitiated).async {
    guard let root = findProjectRoot() else {
        finishFailure(root: nil, message: "Launch failed", detail: "Project root not found — set IG_AGENT_ROOT")
        return
    }

    startStatusPoll(root: root)
    notify("IG Agent", "Isolated clean launch starting…")

    if runScript(root: root, name: "agent_kill.sh", required: true, supervisorPid: supervisorPid) != 0 {
        finishFailure(root: root, message: "Launch failed", detail: "Clean shutdown failed — see logs/agent_kill.log")
        return
    }

    if runScript(root: root, name: "agent_start.sh", required: true, supervisorPid: supervisorPid) != 0 {
        finishFailure(root: root, message: "Launch failed", detail: "Agent start failed — see logs/agent_start.log")
        return
    }

    if runScript(root: root, name: "agent_verify.sh", required: true, supervisorPid: supervisorPid) != 0 {
        finishFailure(root: root, message: "Launch failed", detail: "Verification failed — see logs/agent_verify.log")
        return
    }

    DispatchQueue.main.async {
        panel.update(
            status: "Stage 9 — Opening cockpit",
            detail: "Launching dashboard and IG Cockpit",
            step: 9,
            total: 9
        )
    }
    _ = runScript(root: root, name: "agent_gui.sh", required: false, supervisorPid: supervisorPid)

    stopStatusPoll()
    DispatchQueue.main.async {
        let tier = readLauncherStatus(root: root)?.bootTier ?? "green"
        let readyTitle = tier == "amber" ? "Agent ready (degraded)" : "Agent ready"
        let readyDetail = tier == "amber"
            ? "Dashboard live on port \(port) — IG/feeds still hydrating"
            : "Operational on port \(port) — opening dashboard"
        panel.update(
            status: readyTitle,
            detail: readyDetail,
            step: 9,
            total: 9,
            bootTier: tier
        )
        openDashboard(port: port)
        notify("IG Agent", readyDetail)
        fputs("✅ IGAgentSupervisor complete\n", stderr)
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            panel.close()
            NSApp.terminate(0)
        }
    }
}

app.run()
