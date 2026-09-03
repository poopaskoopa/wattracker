import Foundation
import CryptoKit
import Security

#if WATTRACKER_SOFTWARE_KEYS_ALLOWED && !DEBUG
#error("""
WATTRACKER_SOFTWARE_KEYS_ALLOWED is a development affordance for the iOS \
simulator, which has no Secure Enclave. It puts a P-256 private key in the \
keychain as bytes instead of leaving it in hardware, and it must never reach \
a device build. It is set only by Config/Debug.xcconfig; if you are seeing \
this error, something has put it into a Release build.
""")
#endif

/// Where the device's signing key lives, and what it will sign.
///
/// The private half is a P-256 key because the Secure Enclave generates
/// nothing else, which is also why the server carries an
/// `ecdsa-p256-sha256` algorithm at all (see `security.py`).
protocol DeviceSigner: Sendable {
    /// Uncompressed SEC1 / X9.63: `0x04 || X || Y`, 65 bytes.  The only
    /// encoding `validate_public_key` accepts.
    var publicKeyX963: Data { get }

    /// Raw `r || s`, 64 bytes, which the client hex-encodes.  Never DER, and
    /// never normalised to low-s: the server accepts a malleable signature on
    /// purpose, and rewriting `s` would mean sending something other than
    /// what was signed.
    func signature(over data: Data) throws -> Data

    /// Whether the private half is in hardware.  Reported, never trusted for
    /// an authorization decision -- the server cannot tell the difference and
    /// must not be asked to.
    var isHardwareBacked: Bool { get }
}

enum DeviceKeyError: Error, CustomStringConvertible {
    case secureEnclaveUnavailable
    case keychain(OSStatus)
    case corruptStoredKey

    var description: String {
        switch self {
        case .secureEnclaveUnavailable:
            return """
            This device has no Secure Enclave and this build has no software \
            key fallback compiled in.
            """
        case let .keychain(status):
            return "Keychain error \(status)"
        case .corruptStoredKey:
            return "The stored device key could not be read back"
        }
    }
}

/// Loads the device's signing key, generating one on first run.
enum DeviceKeyStore {
    private static let service = "com.wattracker.ios.device-signing-key"
    private static let enclaveAccount = "secure-enclave-v1"
    private static let softwareAccount = "software-key-v1"

    /// The key this build is allowed to use, preferring hardware.
    ///
    /// The choice is made here and nowhere else, so there is exactly one place
    /// that decides whether a private key may exist as bytes.
    static func loadOrCreate() throws -> DeviceSigner {
        if SecureEnclave.isAvailable {
            return try enclaveKey()
        }
        #if WATTRACKER_SOFTWARE_KEYS_ALLOWED
        // The simulator has no Secure Enclave, and the walking skeleton has to
        // run somewhere.  Compiled in only by Config/Debug.xcconfig; the
        // `#error` at the top of this file is what makes shipping it a build
        // failure rather than a code review.
        return try softwareKey()
        #else
        throw DeviceKeyError.secureEnclaveUnavailable
        #endif
    }

    // MARK: - Secure Enclave

    private static func enclaveKey() throws -> DeviceSigner {
        if let stored = try readKeychain(account: enclaveAccount) {
            // An Enclave key's data representation is an opaque wrapped blob,
            // not the private scalar: the key material never leaves hardware,
            // and this is only the handle the Enclave needs to find it again.
            guard let key = try? SecureEnclave.P256.Signing.PrivateKey(
                dataRepresentation: stored
            ) else {
                throw DeviceKeyError.corruptStoredKey
            }
            return EnclaveSigner(key: key)
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey()
        try writeKeychain(account: enclaveAccount, value: key.dataRepresentation)
        return EnclaveSigner(key: key)
    }

    private struct EnclaveSigner: DeviceSigner {
        let key: SecureEnclave.P256.Signing.PrivateKey
        var publicKeyX963: Data { key.publicKey.x963Representation }
        var isHardwareBacked: Bool { true }
        func signature(over data: Data) throws -> Data {
            // CryptoKit hashes with SHA-256 for a P-256 signing key, which is
            // what `ecdsa-p256-sha256` names.  Do not pre-hash.
            try key.signature(for: data).rawRepresentation
        }
    }

    // MARK: - Software fallback (development only)

    #if WATTRACKER_SOFTWARE_KEYS_ALLOWED
    private static func softwareKey() throws -> DeviceSigner {
        if let stored = try readKeychain(account: softwareAccount) {
            guard let key = try? P256.Signing.PrivateKey(rawRepresentation: stored) else {
                throw DeviceKeyError.corruptStoredKey
            }
            return SoftwareSigner(key: key)
        }
        let key = P256.Signing.PrivateKey()
        try writeKeychain(account: softwareAccount, value: key.rawRepresentation)
        return SoftwareSigner(key: key)
    }

    private struct SoftwareSigner: DeviceSigner {
        let key: P256.Signing.PrivateKey
        var publicKeyX963: Data { key.publicKey.x963Representation }
        var isHardwareBacked: Bool { false }
        func signature(over data: Data) throws -> Data {
            try key.signature(for: data).rawRepresentation
        }
    }
    #endif

    // MARK: - Keychain

    private static func readKeychain(account: String) throws -> Data? {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = withUnsafeMutablePointer(to: &item) {
            SecItemCopyMatching(query as CFDictionary, $0)
        }
        query.removeAll()
        switch status {
        case errSecSuccess:
            guard let data = item as? Data else { throw DeviceKeyError.corruptStoredKey }
            return data
        case errSecItemNotFound:
            return nil
        default:
            throw DeviceKeyError.keychain(status)
        }
    }

    private static func writeKeychain(account: String, value: Data) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: value,
            // ThisDeviceOnly keeps the key out of every backup and out of any
            // restore onto another device: a device credential that could be
            // restored elsewhere would let a second phone read the rider's
            // data with the first phone's identity.
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        SecItemDelete(query as CFDictionary)
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw DeviceKeyError.keychain(status)
        }
    }
}
