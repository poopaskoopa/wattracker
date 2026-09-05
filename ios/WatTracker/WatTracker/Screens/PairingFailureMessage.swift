import Foundation

/// What the rider is told when a pairing attempt does not work, and the one
/// thing this file exists to guarantee: that a wrong code, an expired code and
/// an already-used code produce the *same sentence*.
///
/// The server goes to some trouble to make those three indistinguishable --
/// `DevicePairingRegistry` in `wattracker/cloud/security.py` answers a
/// redemption failure the same way whatever the reason, and the read plane
/// answers every authentication failure with 404 for the same purpose. A UI
/// that renders `CloudClient.Failure` verbatim hands that back: "HTTP 410"
/// against one code and "HTTP 404" against another is an oracle, and an
/// attacker holding a screen full of guesses would rather have it than not.
/// So the default here is the shared message, and a condition earns a
/// distinct one only by being *provably independent of the code's value*.
///
/// Which conditions clear that bar:
///
/// - **No connection.** The request never reached a server, so the answer
///   cannot depend on what was in it.
/// - **The server asked us to wait** (429, 503). Admission control runs before
///   the pairing registry is consulted; the same refusal arrives for a valid
///   code, and telling the rider to wait rather than to find a new code is the
///   difference between the app being usable and not.
/// - **This device could not sign / could not read the reply.** Both are
///   statements about this client, not about the code, and both need a
///   different action from the rider than "get a new code".
/// - **The device's clock is off.** Also a fact about the device. It cannot
///   arise from `pair` today -- pairing carries no signature, so no timestamp
///   is checked -- but `CloudSession.Failure` can carry it and falling through
///   to a message about the code would be a lie if it ever did.
///
/// Everything else, including *every* HTTP status that is not 429 or 503,
/// lands on `codeRefused`. That is the fail-safe direction: a new condition
/// nobody anticipated becomes one more thing that looks like a bad code,
/// rather than one more thing that leaks.
enum PairingFailureMessage {
    /// The one message. Wrong, expired, already used, redeemed on another
    /// phone, never minted at all -- all of it reads exactly like this.
    static let codeRefused =
        "That code did not work. Ask the desktop app for a new one and type it in again."

    static func text(for error: Error) -> String {
        if let failure = error as? CloudSession.Failure {
            return text(for: failure)
        }
        // `CloudSession.pair` wraps everything, so a bare client failure only
        // reaches here from a caller that skipped the session. Mapped the same
        // way regardless, so the two paths cannot say different things about
        // the same refusal.
        if let failure = error as? CloudClient.Failure {
            return text(for: failure)
        }
        if error is URLError { return offline }
        return codeRefused
    }

    private static let offline =
        "No connection. Check that this device is on the same network as the desktop, "
        + "then try again."

    private static func text(for failure: CloudSession.Failure) -> String {
        switch failure {
        case .offline:
            return offline
        case let .throttled(retryAfter):
            return busy(retryAfter: retryAfter)
        case let .clockSkew(seconds):
            return "This device's clock is \(Int(abs(seconds)))s away from the server's. "
                + "Set the date and time automatically, then try again."
        case let .server(underlying):
            return text(for: underlying)
        case .notPaired, .deviceRemoved:
            // Reachable only if the credential changed underneath this attempt
            // -- another pairing landed first, or the session was wiped
            // mid-request. Nothing about the code follows from either.
            return codeRefused
        }
    }

    private static func text(for failure: CloudClient.Failure) -> String {
        switch failure {
        case let .http(status, _, retryAfter, _):
            // 429 and 503 are the only statuses the pairing route can return
            // without having looked at the code. Every other status -- 400,
            // 403, 404, 409, 410, 422, a 500 from a server bug -- collapses
            // into the shared message on purpose.
            guard status == 429 || status == 503 else { return codeRefused }
            return busy(retryAfter: retryAfter)
        case .nonceUnavailable:
            return "This device could not generate a signing nonce. Restart the app and try again."
        case .malformedResponse:
            // The pairing route's error bodies never reach here -- a non-2xx
            // becomes `.http` first -- so this is a 2xx this build could not
            // decode, which is a version mismatch and not a code.
            return "The server's reply could not be read. This app may need updating."
        case .noProfilePublished:
            // Not reachable from pairing; folded in rather than given a
            // sentence of its own, because the fail-safe default is the point.
            return codeRefused
        }
    }

    private static func busy(retryAfter: TimeInterval?) -> String {
        guard let retryAfter, retryAfter > 0 else {
            return "The server is busy. Wait a moment and try again."
        }
        return "The server is busy. Try again in \(Int(retryAfter.rounded(.up)))s."
    }
}
