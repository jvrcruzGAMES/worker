import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("worker.crypto")

HKDF_INFO = b"mithril-envelope-encryption"


class WorkerCryptoService:
    def __init__(self):
        # Generate X25519 private key for this worker instance
        self._private_key = x25519.X25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()
        self._public_key_bytes = self._public_key.public_bytes_raw()
        self._public_key_b64 = base64.b64encode(self._public_key_bytes).decode("utf-8")
        
        # In-memory single-use token replay protection: jti -> exp timestamp
        self._consumed_jtis: Dict[str, float] = {}

    @property
    def public_key_b64(self) -> str:
        """Returns the base64-encoded raw 32-byte X25519 public key."""
        return self._public_key_b64

    def decrypt_envelope(
        self,
        client_public_key_b64: str,
        nonce_b64: str,
        ciphertext_b64: str,
    ) -> Dict[str, Any]:
        """
        Decrypts an envelope-encrypted payload using the worker's X25519 private key
        and the client's ephemeral public key via ChaCha20-Poly1305.
        """
        try:
            client_pub_bytes = base64.b64decode(client_public_key_b64)
            client_public_key = x25519.X25519PublicKey.from_public_bytes(client_pub_bytes)

            # Perform X25519 ECDH key exchange
            shared_secret = self._private_key.exchange(client_public_key)

            # Derive 32-byte symmetric AEAD key
            derived_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=HKDF_INFO,
            ).derive(shared_secret)

            aead = ChaCha20Poly1305(derived_key)
            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(ciphertext_b64)

            plaintext_bytes = aead.decrypt(nonce, ciphertext, None)
            return json.loads(plaintext_bytes.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to decrypt envelope payload: {e}")
            raise ValueError(f"Envelope decryption failed: {e}") from e

    @staticmethod
    def encrypt_envelope(
        recipient_public_key_b64: str,
        payload_dict: Dict[str, Any],
    ) -> Dict[str, str]:
        """
        Client-side helper: Encrypts a payload dictionary using recipient's X25519 public key
        with an ephemeral keypair and ChaCha20-Poly1305.
        """
        recipient_pub_bytes = base64.b64decode(recipient_public_key_b64)
        recipient_public_key = x25519.X25519PublicKey.from_public_bytes(recipient_pub_bytes)

        # Generate ephemeral keypair
        ephemeral_priv = x25519.X25519PrivateKey.generate()
        ephemeral_pub = ephemeral_priv.public_key()
        ephemeral_pub_b64 = base64.b64encode(ephemeral_pub.public_bytes_raw()).decode("utf-8")

        # Perform ECDH key exchange
        shared_secret = ephemeral_priv.exchange(recipient_public_key)
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=HKDF_INFO,
        ).derive(shared_secret)

        aead = ChaCha20Poly1305(derived_key)
        nonce = os.urandom(12)
        plaintext = json.dumps(payload_dict).encode("utf-8")
        ciphertext = aead.encrypt(nonce, plaintext, None)

        return {
            "client_public_key": ephemeral_pub_b64,
            "nonce": base64.b64encode(nonce).decode("utf-8"),
            "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
        }

    def verify_and_consume_token(
        self,
        token: Optional[str],
        expected_worker_id: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> bool:
        """
        Verifies an orchestrator-signed single-use token and enforces single-use replay protection.
        """
        if not token or "." not in token:
            return False

        self._cleanup_expired_jtis()

        try:
            parts = token.split(".", 1)
            payload_b64, signature = parts[0], parts[1]

            # 1. Verify HMAC signature if auth_token is configured
            if auth_token:
                expected_sig = hmac.new(
                    auth_token.encode("utf-8"),
                    payload_b64.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()

                if not secrets.compare_digest(signature, expected_sig):
                    logger.warning("Single-use token signature mismatch.")
                    return False

            # 2. Decode and validate claims
            # Pad base64 if necessary
            padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
            payload_bytes = base64.urlsafe_b64decode(padded.encode("utf-8"))
            payload = json.loads(payload_bytes.decode("utf-8"))

            jti = payload.get("jti")
            worker_id = payload.get("worker_id")
            exp = payload.get("exp", 0)

            if not jti or not exp:
                return False

            # Check expiration
            if time.time() > exp:
                logger.warning(f"Single-use token expired (exp={exp}).")
                return False

            # Check worker ID binding if provided
            if expected_worker_id and worker_id and worker_id != expected_worker_id:
                logger.warning(
                    f"Single-use token worker_id mismatch (got {worker_id}, expected {expected_worker_id})."
                )
                return False

            # 3. Check replay protection (single-use enforcement)
            if jti in self._consumed_jtis:
                logger.warning(f"Single-use token jti '{jti}' has already been consumed.")
                return False

            # Mark as consumed
            self._consumed_jtis[jti] = float(exp)
            return True

        except Exception as e:
            logger.error(f"Error parsing single-use token: {e}")
            return False

    def _cleanup_expired_jtis(self):
        now = time.time()
        expired = [jti for jti, exp in self._consumed_jtis.items() if exp < now]
        for jti in expired:
            self._consumed_jtis.pop(jti, None)


worker_crypto = WorkerCryptoService()
