import Foundation
import Security

/// The durable half of a pairing: what the device is, never what it holds.
///
/// There is deliberately no reader context in this type.  A reader context is
/// a five-minute bearer token, and a type that can carry one is a type
/// somebody will eventually persist one in.  Keeping the token out of the only
/// shape that reaches storage is what makes "no bearer token is ever written
/// to disk" a property of the code rather than a habit.
///
/// `signingNamespace` is the server's opaque signing context.  The device
/// reproduces it inside every canonical request and can neither choose nor
/// change it; it is not a storage partition key and nothing here treats it as
/// an identifier for anything.
struct PairedDevice: Codable, Sendable, Equatable {
    let credentialID: String
    let subscriptionKey: String
    let signatureAlgorithm: String
    let signingNamespace: String
    let capabilities: [String]

    init(credentialID: String, subscriptionKey: String, signatureAlgorithm: String,
         signingNamespace: String, capabilities: [String] = ["read"]) {
        self.credentialID = credentialID
        self.subscriptionKey = subscriptionKey
        self.signatureAlgorithm = signatureAlgorithm
        self.signingNamespace = signingNamespace
        self.capabilities = capabilities
    }
}

/// What pairing produces: the durable credential, plus the context the pairing
/// response already minted so the first read does not have to refresh.
struct PairingResult: Sendable {
    let device: PairedDevice
    let readerContext: String
    let expiresIn: TimeInterval
}

/// Where the device credential lives between launches.
protocol DeviceCredentialStore: Sendable {
    func load() -> PairedDevice?
    func save(_ device: PairedDevice) throws
    func clear()
}

/// The keychain, with the same accessibility class as the signing key.
///
/// `AfterFirstUnlockThisDeviceOnly` matches `DeviceKeyStore` on purpose. The
/// credential and the key are two halves of one identity: storing them under
/// different classes would mean a state where one is readable and the other is
/// not, and every such state is a bug somebody has to reason about.
/// `ThisDeviceOnly` is the part that matters -- a credential restorable onto a
/// second phone would let that phone read the rider's data under the first
/// phone's identity, and revoking the first would not touch it.
struct KeychainDeviceCredentialStore: DeviceCredentialStore {
    enum Failure: Error, CustomStringConvertible {
        case keychain(OSStatus)

        var description: String {
            switch self {
            case let .keychain(status): return "Keychain error \(status)"
            }
        }
    }

    private let service: String
    private let account: String

    init(service: String = "com.wattracker.ios.device-credential",
         account: String = "paired-device-v1") {
        self.service = service
        self.account = account
    }

    func load() -> PairedDevice? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = withUnsafeMutablePointer(to: &item) {
            SecItemCopyMatching(query as CFDictionary, $0)
        }
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        // A credential that no longer decodes is a credential this build
        // cannot use. Answering nil sends the rider through pairing again,
        // which works; surfacing an error would only offer them a decision
        // they cannot act on.
        return try? JSONDecoder().decode(PairedDevice.self, from: data)
    }

    func save(_ device: PairedDevice) throws {
        let data = try JSONEncoder().encode(device)
        var query = baseQuery()
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemDelete(baseQuery() as CFDictionary)
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw Failure.keychain(status) }
    }

    func clear() {
        SecItemDelete(baseQuery() as CFDictionary)
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

/// The store the tests use.  A unit-test bundle has no keychain access group,
/// so a real keychain round-trip there would be testing the entitlement rather
/// than the client.
final class MemoryDeviceCredentialStore: DeviceCredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var device: PairedDevice?
    private(set) var clearCount = 0

    init(device: PairedDevice? = nil) {
        self.device = device
    }

    func load() -> PairedDevice? {
        lock.lock()
        defer { lock.unlock() }
        return device
    }

    func save(_ device: PairedDevice) throws {
        lock.lock()
        defer { lock.unlock() }
        self.device = device
    }

    func clear() {
        lock.lock()
        defer { lock.unlock() }
        device = nil
        clearCount += 1
    }
}

/// The durable local-server pairing. The token is a connector credential, not
/// a reader context, and is kept out of the snapshot cache and UserDefaults.
struct LocalCredential: Codable, Sendable, Equatable {
    let baseURL: String
    let token: String
    let label: String?

    private enum CodingKeys: String, CodingKey {
        case baseURL, token, label
    }

    init(baseURL: String, token: String, label: String? = nil) throws {
        guard let url = URL(string: baseURL),
              url.scheme?.lowercased() == "https",
              url.host != nil,
              url.user == nil,
              url.password == nil,
              url.query == nil,
              url.fragment == nil,
              url.path.isEmpty || url.path == "/"
        else { throw LocalClient.Failure.insecureOrInvalidBaseURL }
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty else { throw LocalClient.Failure.missingToken }
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.path = ""
        self.baseURL = components.url!.absoluteString
        self.token = trimmedToken
        if let label {
            let value = label.trimmingCharacters(in: .whitespacesAndNewlines)
            self.label = value.isEmpty ? nil : String(value.prefix(64))
        } else {
            self.label = nil
        }
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            baseURL: values.decode(String.self, forKey: .baseURL),
            token: values.decode(String.self, forKey: .token),
            label: values.decodeIfPresent(String.self, forKey: .label)
        )
    }
}

protocol LocalCredentialStore: Sendable {
    func load() -> LocalCredential?
    func save(_ credential: LocalCredential) throws
    func clear()
}

/// The local token is the authority to open a full desktop session, so it has
/// the same device-only protection as the cloud credential.
struct KeychainLocalCredentialStore: LocalCredentialStore {
    enum Failure: Error, CustomStringConvertible {
        case keychain(OSStatus)

        var description: String {
            switch self {
            case let .keychain(status): return "Keychain error \(status)"
            }
        }
    }

    private let service: String
    private let account: String

    init(service: String = "com.wattracker.ios.local-credential",
         account: String = "paired-local-v1") {
        self.service = service
        self.account = account
    }

    func load() -> LocalCredential? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = withUnsafeMutablePointer(to: &item) {
            SecItemCopyMatching(query as CFDictionary, $0)
        }
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(LocalCredential.self, from: data)
    }

    func save(_ credential: LocalCredential) throws {
        let data = try JSONEncoder().encode(credential)
        var query = baseQuery()
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemDelete(baseQuery() as CFDictionary)
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw Failure.keychain(status) }
    }

    func clear() {
        SecItemDelete(baseQuery() as CFDictionary)
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

final class MemoryLocalCredentialStore: LocalCredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var credential: LocalCredential?
    private(set) var clearCount = 0

    init(credential: LocalCredential? = nil) {
        self.credential = credential
    }

    func load() -> LocalCredential? {
        lock.lock()
        defer { lock.unlock() }
        return credential
    }

    func save(_ credential: LocalCredential) throws {
        lock.lock()
        defer { lock.unlock() }
        self.credential = credential
    }

    func clear() {
        lock.lock()
        defer { lock.unlock() }
        credential = nil
        clearCount += 1
    }
}
