package com.windroida.audiohide

import java.security.SecureRandom
import javax.crypto.AEADBadTagException
import javax.crypto.Cipher
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec

/**
 * AES-256-GCM encryption with a passphrase-derived key (PBKDF2WithHmacSHA256).
 *
 * This is deliberately a well-known, publicly reviewed construction, not a
 * secret home-made cipher -- that is what makes it strong. A made-up
 * algorithm cannot be checked by anyone and is almost always breakable in
 * practice; AES-GCM has been attacked by the whole world for 20+ years and
 * has held up. What keeps a message private here is the passphrase, which
 * only exists in your head and the receiver's, never in the app or on the
 * wire. Treat it like a real password: long, random if possible, and never
 * reused, and share it with the other side out of band (in person, etc.),
 * not over the same hidden channel.
 *
 * Wire format of the packed blob: 16-byte salt, then 12-byte IV, then the
 * GCM ciphertext (which already includes its own 16-byte authentication
 * tag at the end).
 */
object Crypto {
    private const val SALT_BYTES = 16
    private const val IV_BYTES = 12
    private const val GCM_TAG_BITS = 128
    private const val PBKDF2_ITERATIONS = 20_000
    private const val KEY_BITS = 256

    /** Thrown when the passphrase is wrong or the data was corrupted/tampered with. */
    class WrongPassphraseException : Exception("رمز اشتباه است یا داده خراب شده")

    private fun deriveKey(passphrase: CharArray, salt: ByteArray): SecretKeySpec {
        val spec = PBEKeySpec(passphrase, salt, PBKDF2_ITERATIONS, KEY_BITS)
        val factory = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        val keyBytes = factory.generateSecret(spec).encoded
        return SecretKeySpec(keyBytes, "AES")
    }

    /** Encrypts [plaintext] with [passphrase]; returns salt + iv + ciphertext (with its tag). */
    fun encrypt(plaintext: ByteArray, passphrase: String): ByteArray {
        val random = SecureRandom()
        val salt = ByteArray(SALT_BYTES).also { random.nextBytes(it) }
        val iv = ByteArray(IV_BYTES).also { random.nextBytes(it) }
        val key = deriveKey(passphrase.toCharArray(), salt)

        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(GCM_TAG_BITS, iv))
        val ciphertext = cipher.doFinal(plaintext)

        val out = ByteArray(SALT_BYTES + IV_BYTES + ciphertext.size)
        System.arraycopy(salt, 0, out, 0, SALT_BYTES)
        System.arraycopy(iv, 0, out, SALT_BYTES, IV_BYTES)
        System.arraycopy(ciphertext, 0, out, SALT_BYTES + IV_BYTES, ciphertext.size)
        return out
    }

    /** Reverses [encrypt]; throws [WrongPassphraseException] on a wrong passphrase or corrupt/tampered data. */
    fun decrypt(packed: ByteArray, passphrase: String): ByteArray {
        if (packed.size < SALT_BYTES + IV_BYTES) throw WrongPassphraseException()
        val salt = packed.copyOfRange(0, SALT_BYTES)
        val iv = packed.copyOfRange(SALT_BYTES, SALT_BYTES + IV_BYTES)
        val ciphertext = packed.copyOfRange(SALT_BYTES + IV_BYTES, packed.size)
        val key = deriveKey(passphrase.toCharArray(), salt)

        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(GCM_TAG_BITS, iv))
            cipher.doFinal(ciphertext)
        } catch (e: AEADBadTagException) {
            throw WrongPassphraseException()
        } catch (e: Exception) {
            throw WrongPassphraseException()
        }
    }
}
