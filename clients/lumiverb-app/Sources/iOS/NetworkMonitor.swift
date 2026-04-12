import Foundation
import Network
import SwiftUI

/// Observes the system's network path and exposes whether the
/// connection is constrained (low-data mode) or expensive (cellular).
/// Injected as `@EnvironmentObject` from `MainTabView` so any view
/// that wants to skip eager prefetches can read the current state.
@MainActor
final class NetworkMonitor: ObservableObject {
    @Published private(set) var isConstrained: Bool = false
    @Published private(set) var isExpensive: Bool = false

    /// Convenience: true when the user is on cellular OR has Low Data
    /// Mode enabled. Either signal means we should avoid eager
    /// prefetching of full-resolution proxies.
    var shouldConservebandwidth: Bool {
        isConstrained || isExpensive
    }

    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "io.lumiverb.app.networkmonitor")

    init() {
        monitor.pathUpdateHandler = { [weak self] path in
            let constrained = path.isConstrained
            let expensive = path.isExpensive
            Task { @MainActor [weak self] in
                self?.isConstrained = constrained
                self?.isExpensive = expensive
            }
        }
        monitor.start(queue: queue)
    }

    deinit {
        monitor.cancel()
    }
}
