import Foundation

/// Deployment settings that come from the build configuration, never from a
/// literal in the client.
///
/// The app is distributed to two devices through TestFlight.  If the host ever
/// moves, changing an xcconfig and rebuilding is tolerable; grepping the
/// source for hardcoded URLs is not.  The same mechanism is what lets the
/// Debug configuration point at a server on the developer's own machine
/// without an editing detour before every run.
///
/// The scheme and host are two settings rather than one URL because `//`
/// begins a comment in an xcconfig file, so a full URL cannot be written
/// there without an escaping trick that the next reader has to decode.
enum AppConfiguration {
    static let apiBaseURL: URL = {
        let scheme = infoValue("WATTRACKERAPIScheme")
        let host = infoValue("WATTRACKERAPIHost")
        guard let url = URL(string: "\(scheme)://\(host)"), url.host != nil else {
            // A build whose base URL is unusable cannot do anything useful and
            // must not look as though it might.
            fatalError("WATTRACKERAPIScheme/Host are not a usable URL: \(scheme)://\(host)")
        }
        return url
    }()

    /// True when this build compiled the software-key fallback in.  Surfaced
    /// on screen so a build that is quietly using a keychain key instead of
    /// the Secure Enclave cannot be mistaken for a shippable one.
    static var allowsSoftwareKeys: Bool {
        #if WATTRACKER_SOFTWARE_KEYS_ALLOWED
        return true
        #else
        return false
        #endif
    }

    private static func infoValue(_ key: String) -> String {
        guard let value = Bundle.main.object(forInfoDictionaryKey: key) as? String,
              !value.isEmpty else {
            fatalError("Info.plist is missing \(key); check Config/*.xcconfig")
        }
        return value
    }
}
