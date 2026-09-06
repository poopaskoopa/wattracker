import XCTest

/// The pairing code's shape, and the promise that a failed pairing says
/// nothing about why.
final class PairingTests: XCTestCase {
    // MARK: - The code's shape

    func testTheGroupedFormMatchesTheServersDisplayForm() {
        XCTAssertEqual(PairingCode.grouped("abcdefghjkmn"), "ABCD-EFGH-JKMN")
        XCTAssertEqual(PairingCode.grouped("ABCD-EFGH-JKMN"), "ABCD-EFGH-JKMN")
        XCTAssertEqual(PairingCode.grouped(" abcd efgh jkmn "), "ABCD-EFGH-JKMN")
    }

    func testTheFoldedLettersMatchTheServers() {
        // I and L fold to 1, O folds to 0. Generated codes contain none of the
        // three, so folding cannot merge two distinct codes.
        XCTAssertEqual(PairingCode.normalized("ILOABCDEFGHJ"), "110ABCDEFGHJ")
        // U is not folded; it is not a symbol at all.
        XCTAssertNil(PairingCode.normalized("UBCDEFGHJKMN"))
    }

    func testAnythingThatIsNotACodeIsRejected() {
        XCTAssertNil(PairingCode.normalized(""))
        XCTAssertNil(PairingCode.normalized("ABCD-EFGH"))
        XCTAssertNil(PairingCode.normalized("ABCD-EFGH-JKMN-PQRS"))
        XCTAssertNil(PairingCode.normalized("WIFI:S:home;T:WPA;P:hunter2;;"))
        XCTAssertNil(
            PairingCode.normalized(String(repeating: "A", count: 65))
        )
    }

    // MARK: - One message, whatever went wrong

    /// The property the whole file exists for: a wrong code, an expired code
    /// and an already-used code are one sentence on screen.
    ///
    /// The server answers all three the same way on purpose. This asserts the
    /// client cannot undo that by rendering the status it happened to receive
    /// -- the statuses below are every plausible way the pairing route can
    /// refuse a redemption, plus a couple it should never send.
    func testEveryRefusalReadsIdentically() {
        let statuses = [400, 401, 403, 404, 409, 410, 422, 500, 502]
        for status in statuses {
            let failure = CloudSession.Failure.server(
                .http(status: status, path: "/api/v1/devices/pair",
                      retryAfter: nil, serverDate: nil)
            )
            XCTAssertEqual(
                PairingFailureMessage.text(for: failure),
                PairingFailureMessage.codeRefused,
                "HTTP \(status) must not be distinguishable"
            )
        }
    }

    func testARefusalNeverLeaksTheUnderlyingFailure() {
        let failure = CloudSession.Failure.server(
            .http(status: 410, path: "/api/v1/devices/pair",
                  retryAfter: nil, serverDate: nil)
        )
        let message = PairingFailureMessage.text(for: failure)
        // `CloudSession.Failure.description` would put "HTTP 410" and the path
        // straight on screen; that is the mistake this mapping exists to stop.
        XCTAssertFalse(message.contains("410"))
        XCTAssertFalse(message.contains("HTTP"))
        XCTAssertFalse(message.contains("/api/"))
    }

    func testAnUnrecognisedErrorFallsBackToTheSharedMessage() {
        struct Surprise: Error {}
        XCTAssertEqual(
            PairingFailureMessage.text(for: Surprise()),
            PairingFailureMessage.codeRefused
        )
        XCTAssertEqual(
            PairingFailureMessage.text(for: CloudSession.Failure.notPaired),
            PairingFailureMessage.codeRefused
        )
    }

    // MARK: - The conditions that are allowed to be different

    func testConditionsIndependentOfTheCodeSayWhatTheyAre() {
        // None of these can be produced by one code and not another, so
        // reporting them costs no indistinguishability and telling the rider
        // to go and find a new code instead would be wrong.
        let offline = PairingFailureMessage.text(for: CloudSession.Failure.offline)
        XCTAssertNotEqual(offline, PairingFailureMessage.codeRefused)
        XCTAssertEqual(
            PairingFailureMessage.text(for: URLError(.notConnectedToInternet)),
            offline
        )

        let busy = PairingFailureMessage.text(
            for: CloudSession.Failure.server(
                .http(status: 429, path: "/api/v1/devices/pair",
                      retryAfter: 30, serverDate: nil)
            )
        )
        XCTAssertNotEqual(busy, PairingFailureMessage.codeRefused)
        XCTAssertTrue(busy.contains("30"))

        let skewed = PairingFailureMessage.text(
            for: CloudSession.Failure.clockSkew(seconds: -600)
        )
        XCTAssertNotEqual(skewed, PairingFailureMessage.codeRefused)
        XCTAssertTrue(skewed.contains("600"))
    }

    // MARK: - What the camera says when it is not looking at a code

    @MainActor
    func testABarcodeThatIsNotAPairingCodeSaysSoRatherThanNothing() {
        let model = PairingModel()

        model.scanned("WIFI:S=router;T=WPA;P=hunter2;;", gate: PairingTests.gate())

        XCTAssertNotNil(
            model.scanHint,
            "a scan that is dropped without feedback looks like a broken scanner"
        )
        // The hint is about the barcode in front of the camera, never about a
        // code's fate on the server: nothing was sent, so there is nothing to
        // leak, and it must not be mistaken for a verdict either.
        XCTAssertNil(model.message)
        XCTAssertEqual(model.code, "")
    }

    @MainActor
    func testAWellShapedScanClearsTheHintAndIsAccepted() {
        let model = PairingModel()
        model.scanned("not-a-code", gate: PairingTests.gate())
        XCTAssertNotNil(model.scanHint)

        model.scanned("abcd-efgh-jkmn", gate: PairingTests.gate())

        XCTAssertNil(model.scanHint)
        XCTAssertEqual(model.code, "ABCD-EFGH-JKMN")
    }

    @MainActor
    private static func gate() -> SessionGate {
        SessionGate(makeSession: { CloudSession(
            client: CloudClient(
                baseURL: URL(string: "https://api.example.invalid")!,
                signer: StubSigner(),
                transport: ScriptedTransport { _, _ in
                    throw URLError(.notConnectedToInternet)
                }
            ),
            credentials: MemoryDeviceCredentialStore(device: nil),
            cache: MemorySnapshotCache()
        ) })
    }
}

