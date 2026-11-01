package com.wattracker.android.cloud

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * The credential store's rules, run against the real store with a software AES
 * key and a temp file.
 *
 * What this suite can pin is the store and the envelope's layout: that the file
 * never holds a secret in the clear, that the envelope really is `[IV][body]`
 * and decrypts through the same code path production uses, that a vault this
 * key did not write is refused rather than half-believed, that a refusal is
 * reported through `health` while the file stays put, that "there is no key"
 * and "the vault is bad" stay separate answers, that a read failure is not an
 * empty vault and may not be acted on, that a member this build cannot parse
 * survives a rewrite, that the key is taken before the store exists and only
 * once, that a launch reads the file once, that writes are all-or-nothing, and
 * that a refused write throws.
 *
 * What it cannot pin is the AndroidKeyStore itself -- the non-exportable key,
 * the provider-chosen IV on a real device -- which is device-only and stays a
 * device check (the same split as [DeviceKeyStore], whose JVM coverage is nil
 * for the same reason). The layout below is the same for both: the provider
 * draws the IV, and the cipher writes it in front.
 */
class KeystoreDeviceCredentialStoreTest {

    @get:Rule
    val folder = TemporaryFolder()

    private val device = PairedDevice(
        credentialId = "cred-1",
        signingNamespace = "ns-1",
        subscriptionKey = "sub-SECRET-9f3a",
        capabilities = listOf("read"),
        label = "phone",
    )

    private val local = LocalCredentials(
        serverUrl = "https://desktop.example:8443",
        token = "connector-SECRET-4c71",
        username = "rider",
        pairedAtMillis = 1_700_000_000_000L,
    )

    private fun vaultFile(name: String = "vault"): File = File(folder.root, name)

    private fun key(seed: Int): SecretKey = SecretKeySpec(ByteArray(32) { (it + seed).toByte() }, "AES")

    private fun storeOn(
        file: File,
        encryptionKey: SecretKey = key(0),
        associatedData: String = "com.wattracker.android.device-credentials-v1",
        cipher: CredentialCipher? = null,
    ): KeystoreDeviceCredentialStore = KeystoreDeviceCredentialStore(
        file,
        cipher ?: AesGcmCipher(
            keyProvider = { encryptionKey },
            associatedData = associatedData.toByteArray(Charsets.UTF_8),
        ),
    )

    // MARK: - The envelope's layout (the #459 B1 fix)

    /**
     * `doFinal` returns ciphertext and tag, not an IV: the provider draws the
     * IV and `getIV()` is the only place it appears. The earlier cipher returned
     * `doFinal` alone and decrypted without a `GCMParameterSpec`, so every real
     * vault was written with no IV and every later read threw -- a silent unpair
     * per relaunch. This pins the layout the store actually relies on.
     */
    @Test
    fun theEnvelopeIsIvThenCiphertextThenTag() {
        val cipher = AesGcmCipher(
            keyProvider = { key(0) },
            associatedData = "aad".toByteArray(Charsets.UTF_8),
        )
        val plaintext = "the vault body".toByteArray(Charsets.UTF_8)
        val envelope = cipher.encrypt(plaintext)
        assertEquals(
            "the envelope is a 12-byte IV, the body, and a 16-byte tag",
            plaintext.size + 12 + 16,
            envelope.size,
        )

        // The leading 12 bytes really are the IV: a raw cipher that is told
        // about them explicitly opens the rest. That is the same parameter
        // `decrypt` supplies, so this is not a second interpretation.
        val raw = Cipher.getInstance("AES/GCM/NoPadding")
        raw.init(Cipher.DECRYPT_MODE, key(0), GCMParameterSpec(128, envelope.copyOfRange(0, 12)))
        raw.updateAAD("aad".toByteArray(Charsets.UTF_8))
        assertArrayEquals(plaintext, raw.doFinal(envelope, 12, envelope.size - 12))

        assertArrayEquals(plaintext, cipher.decrypt(envelope))
    }

    @Test
    fun theIvIsDrawnFreshForEveryEncrypt() {
        val cipher = AesGcmCipher(
            keyProvider = { key(0) },
            associatedData = "aad".toByteArray(Charsets.UTF_8),
        )
        val plaintext = "same body every time".toByteArray(Charsets.UTF_8)
        val first = cipher.encrypt(plaintext)
        val second = cipher.encrypt(plaintext)
        assertFalse(
            "a repeated (key, IV) is the one fatal GCM failure",
            first.copyOfRange(0, 12).contentEquals(second.copyOfRange(0, 12)),
        )
        assertFalse("and the ciphertext differs too", first.contentEquals(second))
    }

    @Test
    fun anEnvelopeWithNoIvIsRefused() {
        val cipher = AesGcmCipher(
            keyProvider = { key(0) },
            associatedData = "aad".toByteArray(Charsets.UTF_8),
        )
        val envelope = cipher.encrypt("payload".toByteArray(Charsets.UTF_8))
        val bodyOnly = envelope.copyOfRange(12, envelope.size)

        val thrown = try {
            cipher.decrypt(bodyOnly)
            null
        } catch (e: Exception) {
            e
        }
        assertNotNull("an envelope without its IV cannot be opened", thrown)
    }

    // MARK: - Round trip and secrecy

    @Test
    fun theBindingAndTheDesktopTokenRoundTripThroughOneVault() {
        val file = vaultFile()
        val store = storeOn(file)
        store.save(device)
        store.saveLocal(local)
        assertEquals(device, store.load())
        assertEquals(local, store.loadLocal())
        assertEquals("a healthy vault is not an unhealthy store", StoreHealth.Durable, store.health)

        // A second store on the same file is what a relaunch is.
        val relaunched = storeOn(file)
        assertEquals(device, relaunched.load())
        assertEquals(local, relaunched.loadLocal())
    }

    @Test
    fun nothingInTheFileIsInTheClear() {
        val file = vaultFile()
        storeOn(file).apply {
            save(device)
            saveLocal(local)
        }
        val onDisk = file.readBytes().toString(Charsets.ISO_8859_1)
        for (secret in listOf("sub-SECRET-9f3a", "connector-SECRET-4c71", "cred-1", "credential_id", "serverUrl")) {
            assertFalse("the vault leaked \"$secret\" to disk", onDisk.contains(secret))
        }
    }

    @Test
    fun oneFlippedByteRefusesTheVaultAndTheFileStaysPut() {
        val file = vaultFile()
        storeOn(file).save(device)
        val bytes = file.readBytes()
        val middle = bytes.size / 2
        bytes[middle] = (bytes[middle].toInt() xor 0x01).toByte()
        file.writeBytes(bytes)

        val reader = storeOn(file)
        assertNull("a tampered vault is no credential", reader.load())
        assertEquals("and the app learns it was refused, not absent", StoreHealth.UnreadableVault, reader.health)
        assertTrue("the file is not deleted on the way to that answer", file.exists())
    }

    @Test
    fun aVaultWrittenByAnotherKeyIsRefused() {
        val file = vaultFile()
        storeOn(file, encryptionKey = key(1)).save(device)

        val other = storeOn(file, encryptionKey = key(2))
        assertNull(other.load())
        assertEquals(StoreHealth.UnreadableVault, other.health)
    }

    /**
     * The associated data is the vault's own purpose, so an envelope written
     * for a different purpose does not open even under the right key. That is
     * purpose separation, not installation binding: the binding is the
     * per-installation Keystore key, which is why a carried or restored file
     * fails on the device that lacks it.
     */
    @Test
    fun aVaultMeantForAnotherPurposeIsRefusedEvenUnderTheSameKey() {
        val file = vaultFile()
        storeOn(file, associatedData = "com.wattracker.android.device-credentials-v1").save(device)

        val renamedOver = storeOn(file, associatedData = "com.wattracker.android.something-else")
        assertNull(renamedOver.load())
        assertEquals(StoreHealth.UnreadableVault, renamedOver.health)
    }

    // MARK: - The two credentials are independent

    @Test
    fun clearingOneCredentialLeavesTheOtherAlone() {
        val file = vaultFile()
        val store = storeOn(file)
        store.save(device)
        store.saveLocal(local)

        store.clear()
        assertNull(store.load())
        assertEquals(local, store.loadLocal())

        store.clearLocal()
        assertNull(store.loadLocal())
    }

    @Test
    fun aVaultWithNothingInItIsNoFileAtAll() {
        val file = vaultFile()
        val store = storeOn(file)
        store.save(device)
        store.saveLocal(local)
        assertTrue(file.exists())

        store.clear()
        store.clearLocal()
        assertFalse("no empty vault is left lying around", file.exists())
        assertEquals(StoreHealth.Durable, store.health)
        assertNull(store.load())
    }

    @Test
    fun anAbsentFileIsUnpairedAndNotBroken() {
        val store = storeOn(vaultFile("never-written"))
        assertNull(store.load())
        assertNull(store.loadLocal())
        assertEquals("never paired is a different answer from unreadable", StoreHealth.Durable, store.health)
    }

    /**
     * The pairing-over-a-dead-vault case the #304 review made a point of: the
     * store must let the rider get back to work, and it must stop reporting a
     * problem it can no longer see.
     */
    @Test
    fun pairingOverADeadVaultReplacesItEntirely() {
        val file = vaultFile()
        val original = storeOn(file, encryptionKey = key(3))
        original.save(device)

        // The key behind it is gone (restore, Keystore reset): unreadable.
        val orphan = storeOn(file, encryptionKey = key(4))
        assertNull(orphan.load())
        assertEquals(StoreHealth.UnreadableVault, orphan.health)

        val replacement = device.copy(credentialId = "cred-2")
        orphan.save(replacement)
        assertEquals("the report clears once the vault opens again", StoreHealth.Durable, orphan.health)
        assertEquals(replacement, orphan.load())
        // A *fresh* store under the old key cannot open the file: the dead vault
        // was replaced, not merged. (The `original` instance is not used here --
        // it holds its own decoded cache, and production has one store per file.)
        val oldKeyReader = storeOn(file, encryptionKey = key(3))
        assertNull("the dead vault's bytes are gone, not merged in", oldKeyReader.load())
    }

    // MARK: - The #459 M1 fix: a failed read is not an empty vault

    /**
     * The old shape flattened every read failure to an empty vault, and the next
     * `clearLocal()` then deleted the file -- so one transient I/O error during
     * a desktop 401 could take a still-valid cloud pairing with it. A read the
     * filesystem refuses now refuses the mutation instead.
     */
    @Test
    fun aReadTheFilesystemRefusesIsNotAnEmptyVault() {
        val blocked = vaultFile("vault")
        assertTrue(blocked.mkdirs())
        val store = storeOn(blocked)

        assertNull("this attempt answers unpaired", store.load())
        assertEquals(
            "a failed read is not evidence the vault is broken",
            StoreHealth.Durable,
            store.health,
        )

        val clearFailure = try {
            store.clearLocal()
            null
        } catch (e: CredentialStoreException) {
            e
        }
        assertNotNull("a clear on top of a failed read must not act", clearFailure)

        val saveFailure = try {
            store.saveLocal(local)
            null
        } catch (e: CredentialStoreException) {
            e
        }
        assertNotNull("and neither must a save", saveFailure)

        assertTrue("nothing on disk was touched", blocked.isDirectory)
    }

    @Test
    fun aReadTheKeyRefusesIsNotAnEmptyVault() {
        val file = vaultFile()
        storeOn(file).save(device)

        val store = KeystoreDeviceCredentialStore(file, KeyDeadCipher())
        assertNull(store.load())

        val clearFailure = try {
            store.clear()
            null
        } catch (e: CredentialStoreException) {
            e
        }
        assertNotNull("a clear on top of a key failure must not act", clearFailure)
        assertTrue("and the vault is left where it is", file.exists())
        assertEquals(StoreHealth.Unavailable, store.health)
    }

    /**
     * An *authenticated* refusal is different: GCM has certified those bytes are
     * not a credential this installation wrote, so a save may replace them (a
     * fresh pairing is the only way back). A clear may not, because deleting on
     * that signal turns "unreadable" into "gone" and recovers nothing.
     */
    @Test
    fun clearingAnUnopenableVaultLeavesItOnDisk() {
        val file = vaultFile()
        storeOn(file, encryptionKey = key(1)).apply {
            save(device)
            saveLocal(local)
        }

        val orphan = storeOn(file, encryptionKey = key(2))
        assertNull(orphan.load())
        assertEquals(StoreHealth.UnreadableVault, orphan.health)

        orphan.clearLocal()
        assertTrue("a clear does not turn \"unreadable\" into \"gone\"", file.exists())

        orphan.save(device.copy(credentialId = "cred-9"))
        assertEquals("a save still replaces it", "cred-9", orphan.load()?.credentialId)
    }

    /**
     * A member this build cannot parse -- a field a newer server added, or a
     * shape it does not know -- is kept as the raw JSON it arrived as. The other
     * member is still served, and a write re-emits the unreadable one unchanged
     * instead of silently dropping it.
     */
    @Test
    fun aMemberThisBuildCannotParseIsKeptAsRawJson() {
        val file = vaultFile()
        // `paired_device` is missing `subscription_key`, so this build cannot
        // parse it; `local_credentials` is complete.
        val json = """
            {"format":1,
             "paired_device":{"credential_id":"cred-1","signing_namespace":"ns-1"},
             "local_credentials":{"serverUrl":"https://desktop.example:8443",
                                  "token":"connector-SECRET-4c71",
                                  "username":"rider",
                                  "pairedAtMillis":1700000000000}}
        """.trimIndent()
        file.writeBytes(json.toByteArray(Charsets.UTF_8))

        val store = KeystoreDeviceCredentialStore(file, PassthroughCipher())
        assertNull("the member it cannot read is not served", store.load())
        assertEquals("but the other member still is", local, store.loadLocal())
        assertEquals("a vault that opens is a healthy store", StoreHealth.Durable, store.health)

        store.saveLocal(local.copy(token = "connector-SECRET-new"))
        val rewritten = file.readText(Charsets.UTF_8)
        assertTrue(
            "the unreadable member survives the rewrite",
            rewritten.contains("\"signing_namespace\":\"ns-1\""),
        )
        assertTrue("and the written one is the new value", rewritten.contains("connector-SECRET-new"))
    }

    // MARK: - Writes

    /**
     * The failure the session's "cache first, credential second" ordering depends
     * on hearing about: a write the filesystem will not take. A vault under a
     * directory that is not there cannot be opened for writing, and the store
     * must report that rather than leave the caller holding a pairing that only
     * exists in memory.
     */
    @Test
    fun aWriteTheFilesystemRefusesIsThrownNotIgnored() {
        val store = storeOn(File(File(folder.root, "no-such-directory"), "vault"))

        val thrown = try {
            store.save(device)
            null
        } catch (e: CredentialStoreException) {
            e
        }
        assertNotNull("a refused credential write must be reported", thrown)
        assertFalse(
            "and no half-written envelope is left behind",
            File(folder.root, "no-such-directory").exists(),
        )
    }

    /**
     * The other half of "reported, not ignored": the vault's *replacement* cannot
     * land. The temp file writes fine and the rename onto a non-empty directory
     * cannot -- on either platform this suite runs on -- and the caller must hear
     * about it rather than go on holding a pairing that exists only in this
     * process.
     */
    @Test
    fun aVaultThatCannotBeReplacedIsReportedAndLeavesNoEnvelopeBehind() {
        val blocked = vaultFile("vault")
        assertTrue(blocked.mkdirs())
        File(blocked, "in-the-way").writeText("x")
        val store = storeOn(blocked)

        val thrown = try {
            store.saveLocal(local)
            null
        } catch (e: CredentialStoreException) {
            e
        }
        assertNotNull("a rename the filesystem refuses must be reported", thrown)
        assertFalse(
            "and no envelope is left beside the vault for the next launch to find",
            File(folder.root, "vault.tmp").exists(),
        )
    }

    // MARK: - Format and envelope opacity

    /**
     * A format this build does not describe is unreadable, not guessed at. The
     * passthrough cipher lets the test write the body directly, since the real
     * store's version constant is its own business.
     */
    @Test
    fun anUnknownFormatIsRefusedRatherThanGuessedAt() {
        val file = vaultFile()
        file.writeBytes("""{"format":2,"paired_device":{"credential_id":"x"}}""".toByteArray(Charsets.UTF_8))
        val store = KeystoreDeviceCredentialStore(file, PassthroughCipher())
        assertNull(store.load())
        assertEquals(StoreHealth.UnreadableVault, store.health)
    }

    @Test
    fun anEmptyWellFormedVaultIsUnpairedAndNotBroken() {
        val file = vaultFile()
        file.writeBytes("""{"format":1}""".toByteArray(Charsets.UTF_8))
        val store = KeystoreDeviceCredentialStore(file, PassthroughCipher())
        assertNull(store.load())
        assertEquals("a vault that reads and holds nothing is not a broken store", StoreHealth.Durable, store.health)
    }

    /**
     * The store treats the envelope as opaque bytes: it never looks inside for an
     * IV, a length or a magic prefix. The IV's position is the cipher's contract,
     * not the store's.
     */
    @Test
    fun theStoreNeverLooksInsideTheEnvelope() {
        val file = vaultFile()
        val store = KeystoreDeviceCredentialStore(file, OpaqueEnvelopeCipher())
        store.save(device)
        store.saveLocal(local)
        assertEquals(device, store.load())
        assertEquals(local, store.loadLocal())
    }

    // MARK: - The key and the read cache

    @Test
    fun theInMemoryFallbackSaysSoAboutItself() {
        // #195's pairing screen asks the store what it promises about the next
        // launch; the in-memory one must not be allowed to answer "durable".
        assertEquals(StoreHealth.Unavailable, InMemoryDeviceCredentialStore().health)
    }

    /**
     * The key is taken while the store is being built, and once. Both halves
     * matter: a Keystore that will not hand over a key has to fail where its
     * caller can still choose a fallback, not on the write that follows a spent
     * pairing code; and the alias is fixed for the life of the process, so a
     * Keystore round trip per `loadLocal()` is waste.
     */
    @Test
    fun theKeyIsTakenBeforeTheStoreExistsAndOnlyOnceAfterThat() {
        var reads = 0
        val provider = eagerKeyProvider {
            reads++
            key(7)
        }
        assertEquals("the load ran during construction, not on first use", 1, reads)
        repeat(4) { provider() }
        assertEquals("and one read covers every later operation", 1, reads)
    }

    @Test
    fun aKeystoreThatWantsNoPartFailsAtConstructionWhereTheCallerCanHearIt() {
        val thrown = try {
            eagerKeyProvider { throw CredentialKeyStore.CredentialKeyException("the Android Keystore is unavailable") }
            null
        } catch (e: CredentialKeyStore.CredentialKeyException) {
            e
        }
        assertNotNull("the store must not be built around a key that does not exist", thrown)
    }

    /**
     * `loadLocal()` is the desktop connector's per-request path, so the decoded
     * vault is cached under the store's lock and replaced by every successful
     * write (#459 review, L1). One launch reads the file once.
     */
    @Test
    fun aLaunchReadsTheVaultOnceNoMatterHowManyReads() {
        val file = vaultFile()
        // A real vault on disk first, written by a store the counter does not
        // see: the launch under test starts with something to read.
        storeOn(file).apply {
            save(device)
            saveLocal(local)
        }

        val counting = CountingCipher(AesGcmCipher(
            keyProvider = { key(0) },
            associatedData = "com.wattracker.android.device-credentials-v1".toByteArray(Charsets.UTF_8),
        ))
        val store = storeOn(file, cipher = counting)

        repeat(5) {
            assertEquals(device, store.load())
            assertEquals(local, store.loadLocal())
        }
        assertEquals("ten reads opened the file once", 1, counting.decrypts)

        store.save(device.copy(credentialId = "cred-2"))
        assertEquals("and a write on top of a cached vault does not re-read it", 1, counting.decrypts)
        assertEquals("while still wrapping exactly once", 1, counting.encrypts)
        assertEquals("cred-2", store.load()?.credentialId)
    }

    @Test
    fun theCacheSeesEveryWrite() {
        val store = storeOn(vaultFile())
        assertEquals(null, store.load())
        store.save(device)
        assertEquals(device, store.load())
        store.save(device.copy(credentialId = "cred-2"))
        assertEquals("cred-2", store.load()?.credentialId)
        store.clear()
        assertNull(store.load())
    }

    // MARK: - Failure typing

    /**
     * "No key" and "bad vault" are different advice: one means pair again and
     * the file is replaced, the other means this device cannot save a pairing at
     * all. If the cipher wraps a key failure into a cipher failure -- or the
     * store catches both alike -- the two collapse and the screen lies.
     */
    @Test
    fun aKeyFailureStaysAKeyFailureThroughTheCipher() {
        val cipher = AesGcmCipher(
            keyProvider = { throw CredentialKeyStore.CredentialKeyException("the credential key could not be read") },
            associatedData = "aad".toByteArray(Charsets.UTF_8),
        )

        val readFailure = try {
            cipher.decrypt(ByteArray(64))
            null
        } catch (e: Exception) {
            e
        }
        assertTrue(
            "a read must be able to say the key is gone, not that the vault is wrong",
            readFailure is CredentialKeyStore.CredentialKeyException,
        )

        val writeFailure = try {
            cipher.encrypt("x".toByteArray(Charsets.UTF_8))
            null
        } catch (e: Exception) {
            e
        }
        assertTrue(
            "and so must a write",
            writeFailure is CredentialKeyStore.CredentialKeyException,
        )
    }

    @Test
    fun aKeyThatCannotBeUsedSaysUnavailableRatherThanBlamingTheVault() {
        val file = vaultFile()
        storeOn(file).save(device)   // a real vault, written while the key still worked

        val orphan = KeystoreDeviceCredentialStore(file, KeyDeadCipher())
        assertNull(orphan.load())
        assertEquals(
            """"pair again replaces it" is wrong advice when nothing can be written""",
            StoreHealth.Unavailable,
            orphan.health,
        )
        assertTrue("and the vault is left where it is", file.exists())
    }

    @Test
    fun aWriteTheKeyRefusesIsARefusedWriteAndSaysSoAfterwards() {
        val store = KeystoreDeviceCredentialStore(vaultFile(), KeyDeadCipher())

        val thrown = try {
            store.save(device)
            null
        } catch (e: CredentialStoreException) {
            e
        }
        assertNotNull("the session has to learn the pairing did not persist", thrown)
        assertEquals(StoreHealth.Unavailable, store.health)
    }

    /** A cipher whose key is gone: no key to ask, rather than a bad envelope. */
    private class KeyDeadCipher : CredentialCipher {
        override fun encrypt(plaintext: ByteArray): ByteArray =
            throw CredentialKeyStore.CredentialKeyException("the Android Keystore is unavailable")

        override fun decrypt(envelope: ByteArray): ByteArray =
            throw CredentialKeyStore.CredentialKeyException("the credential key could not be read")
    }

    /** A cipher that adds nothing, so the test can address the store's own rules. */
    private class PassthroughCipher : CredentialCipher {
        override fun encrypt(plaintext: ByteArray): ByteArray = plaintext
        override fun decrypt(envelope: ByteArray): ByteArray = envelope
    }

    /** A cipher whose envelope has no structure at all: no IV prefix, no version. */
    private class OpaqueEnvelopeCipher : CredentialCipher {
        override fun encrypt(plaintext: ByteArray): ByteArray = PREFIX + plaintext.reversedArray()
        override fun decrypt(envelope: ByteArray): ByteArray =
            envelope.copyOfRange(PREFIX.size, envelope.size).reversedArray()

        private companion object {
            val PREFIX = byteArrayOf(7, 0, 0, 42, -1)
        }
    }

    /** Counts how often the store actually went to the cipher. */
    private class CountingCipher(private val inner: CredentialCipher) : CredentialCipher {
        var encrypts = 0
        var decrypts = 0

        override fun encrypt(plaintext: ByteArray): ByteArray {
            encrypts++
            return inner.encrypt(plaintext)
        }

        override fun decrypt(envelope: ByteArray): ByteArray {
            decrypts++
            return inner.decrypt(envelope)
        }
    }
}
