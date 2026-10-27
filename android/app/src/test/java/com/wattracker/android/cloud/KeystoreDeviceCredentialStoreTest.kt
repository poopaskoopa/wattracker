package com.wattracker.android.cloud

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import javax.crypto.SecretKey
import javax.crypto.spec.SecretKeySpec

/**
 * The credential store's rules, run against the real store with a software AES
 * key and a temp file.
 *
 * What this suite can pin is the store: that the file never holds a secret in
 * the clear, that a vault this key did not write is refused rather
 * than half-believed, that a refusal is reported through `health` while the
 * file stays put, that writes are all-or-nothing, and that a refused write
 * throws. What it cannot pin is the AndroidKeyStore itself -- the non-exportable
 * key, the provider-chosen IV -- which is device-only and stays a device check
 * (the same split as [DeviceKeyStore], whose JVM coverage is nil for the same
 * reason).
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
    ): KeystoreDeviceCredentialStore = KeystoreDeviceCredentialStore(
        file,
        AesGcmCipher(
            keyProvider = { encryptionKey },
            associatedData = associatedData.toByteArray(Charsets.UTF_8),
        ),
    )

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
     * The envelope is bound to the vault it was written for, so a credential
     * file renamed over another one -- or a second vault smuggled into the same
     * installation -- does not open even on the device whose key it would
     * otherwise serve. That is the Auto Backup / restore / `adb push` case, and
     * the associated data is what closes it rather than hoping the key differs.
     */
    @Test
    fun aVaultMeantForAnotherFileIsRefusedEvenUnderTheSameKey() {
        val file = vaultFile()
        storeOn(file, associatedData = "com.wattracker.android.device-credentials-v1").save(device)

        val renamedOver = storeOn(file, associatedData = "com.wattracker.android.something-else")
        assertNull(renamedOver.load())
        assertEquals(StoreHealth.UnreadableVault, renamedOver.health)
    }

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
        assertNull("and the dead vault's bytes are gone, not merged in", original.load())
    }

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
     * The other half of "reported, not ignored": a vault whose *replacement* the
     * filesystem will not take. The temp file writes fine and the rename onto a
     * non-empty directory cannot -- on either platform this suite runs on -- and
     * the caller must hear about it rather than go on holding a pairing that
     * exists only in this process.
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
     * IV, a length or a magic prefix, because who writes the IV is the cipher's
     * choice (a software key here, the Keystore in production) and the store must
     * work with either. This cipher keeps nothing of the shape it is given.
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

    @Test
    fun theInMemoryFallbackSaysSoAboutItself() {
        // #195's pairing screen asks the store what it promises about the next
        // launch; the in-memory one must not be allowed to answer "durable".
        assertEquals(StoreHealth.Unavailable, InMemoryDeviceCredentialStore().health)
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
}
