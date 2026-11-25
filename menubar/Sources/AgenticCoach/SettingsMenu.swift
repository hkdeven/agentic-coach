import AppKit
import ServiceManagement
import SwiftUI

/// The two settings, behind one small icon button at the end of the panel's header:
/// Compact and Launch at login. Quit has its own button at the foot of the panel.
struct SettingsMenu: View {
    @Binding var compact: Bool
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled

    var body: some View {
        Menu {
            Toggle("Compact", isOn: $compact)
            Toggle("Launch at login", isOn: Binding(get: { launchAtLogin }, set: setLaunchAtLogin))
        } label: {
            Label("Settings", systemImage: "gearshape").labelStyle(.iconOnly).font(.system(size: 12)).foregroundStyle(Theme.ink2)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .accessibilityLabel("Settings")
        .help("Settings: show the mark alone in the menu bar, or launch at login.")
        .onAppear { launchAtLogin = SMAppService.mainApp.status == .enabled }
    }

    private func setLaunchAtLogin(_ on: Bool) {
        launchAtLogin = on // Shows the pick at once.
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("Agentic Coach: launch at login could not be changed: \(error.localizedDescription)")
        }
        // What macOS reports is the truth; a refused change puts the old value back.
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }
}
