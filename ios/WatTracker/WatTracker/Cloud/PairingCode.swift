import Foundation

/// The pairing code's alphabet and shape, mirrored from the server.
///
/// This is a transcription of `normalize_pairing_code` and
/// `format_pairing_code` in `wattracker/cloud/security.py`, and it exists for
/// exactly two jobs: grouping a scanned code back into the `XXXX-XXXX-XXXX`
/// form the rider can read on screen, and deciding whether a QR that wandered
/// into the camera is a pairing code at all before the app spends a redemption
/// attempt on it.
///
/// What it is deliberately **not** is a validity check. A code that normalizes
/// cleanly here can still be wrong, expired or already used, and this type
/// cannot tell those apart any more than the UI is allowed to -- see
/// `PairingFailureMessage`. Typed input is never gated on it either: a
/// client-side rule that drifts from the server's would refuse a code the
/// server would have accepted, and the rider would have no way to tell that
/// from a rejection.
enum PairingCode {
    /// Crockford Base32: 32 symbols, with `I`, `L`, `O` and `U` removed.
    static let alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    static let symbolCount = 12
    static let groupSize = 4
    /// `_MAX_PAIRING_INPUT`. A bound on work, not on correctness.
    static let maximumInputLength = 64

    /// Generated codes never contain the folded letters, so folding them onto
    /// digits cannot merge two distinct codes or shrink the 2^60 space. `U` is
    /// not folded because it is not a legal symbol at all.
    private static let folds: [Character: Character] = ["I": "1", "L": "1", "O": "0"]
    private static let stripped: Set<Character> = ["-", " ", "\t"]

    /// The canonical, ungrouped code, or nil if this is not one.
    static func normalized(_ value: String) -> String? {
        guard !value.isEmpty, value.count <= maximumInputLength else { return nil }
        var symbols = ""
        symbols.reserveCapacity(symbolCount)
        for character in value.uppercased() {
            if stripped.contains(character) { continue }
            let folded = folds[character] ?? character
            guard alphabet.contains(folded) else { return nil }
            symbols.append(folded)
        }
        return symbols.count == symbolCount ? symbols : nil
    }

    /// The grouped display form, or nil if this is not a code.
    static func grouped(_ value: String) -> String? {
        guard let canonical = normalized(value) else { return nil }
        return stride(from: 0, to: symbolCount, by: groupSize).map { start in
            let lower = canonical.index(canonical.startIndex, offsetBy: start)
            let upper = canonical.index(lower, offsetBy: groupSize)
            return String(canonical[lower..<upper])
        }.joined(separator: "-")
    }
}
