"""pynacl vs cryptography の暗号化/復号速度ベンチマーク (E0 Q3 の Python 側)。

対象:
- AES-256-GCM (cryptography)
- ChaCha20-Poly1305 (cryptography)
- XChaCha20-Poly1305 (pynacl)

設計書 §3 で AES-256-GCM 暫定採用、XChaCha20 を再評価候補。Python クライアント
側 (client-py / client-tui) で大量仕訳の復号速度がどう変わるかを実測する。

実行: `python bench.py [--n 10000]`
"""

from __future__ import annotations

import argparse
import os
import secrets
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import (
    AESGCM,
    ChaCha20Poly1305,
)

try:
    from nacl.bindings import (
        crypto_aead_xchacha20poly1305_ietf_decrypt,
        crypto_aead_xchacha20poly1305_ietf_encrypt,
        crypto_aead_xchacha20poly1305_ietf_KEYBYTES,
        crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
    )
    HAVE_PYNACL = True
except ImportError:
    HAVE_PYNACL = False


PLAINTEXT = (
    b'{"date":"2026-05-22","account":"5110","debit":1234,"credit":0,'
    b'"desc":"\xe9\xa3\x9f\xe8\xb2\xbb"}'
)


@dataclass
class Result:
    name: str
    encrypt_ms: float
    decrypt_ms: float
    ciphertext_len: int

    def ops_per_sec(self, n: int) -> tuple[float, float]:
        return (n / (self.encrypt_ms / 1000), n / (self.decrypt_ms / 1000))


def bench_aes_gcm(n: int) -> Result:
    key = AESGCM.generate_key(bit_length=256)
    aead = AESGCM(key)
    nonces = [os.urandom(12) for _ in range(n)]
    cts: list[bytes] = []
    t0 = time.perf_counter()
    for nonce in nonces:
        cts.append(aead.encrypt(nonce, PLAINTEXT, None))
    t1 = time.perf_counter()
    for nonce, ct in zip(nonces, cts):
        aead.decrypt(nonce, ct, None)
    t2 = time.perf_counter()
    return Result("AES-256-GCM (cryptography)", (t1 - t0) * 1000, (t2 - t1) * 1000, len(cts[0]))


def bench_chacha20(n: int) -> Result:
    key = ChaCha20Poly1305.generate_key()
    aead = ChaCha20Poly1305(key)
    nonces = [os.urandom(12) for _ in range(n)]
    cts: list[bytes] = []
    t0 = time.perf_counter()
    for nonce in nonces:
        cts.append(aead.encrypt(nonce, PLAINTEXT, None))
    t1 = time.perf_counter()
    for nonce, ct in zip(nonces, cts):
        aead.decrypt(nonce, ct, None)
    t2 = time.perf_counter()
    return Result("ChaCha20-Poly1305 (cryptography)", (t1 - t0) * 1000, (t2 - t1) * 1000, len(cts[0]))


def bench_xchacha20_pynacl(n: int) -> Result:
    key = secrets.token_bytes(crypto_aead_xchacha20poly1305_ietf_KEYBYTES)
    nonces = [secrets.token_bytes(crypto_aead_xchacha20poly1305_ietf_NPUBBYTES) for _ in range(n)]
    cts: list[bytes] = []
    t0 = time.perf_counter()
    for nonce in nonces:
        cts.append(crypto_aead_xchacha20poly1305_ietf_encrypt(PLAINTEXT, None, nonce, key))
    t1 = time.perf_counter()
    for nonce, ct in zip(nonces, cts):
        crypto_aead_xchacha20poly1305_ietf_decrypt(ct, None, nonce, key)
    t2 = time.perf_counter()
    return Result("XChaCha20-Poly1305 (pynacl)", (t1 - t0) * 1000, (t2 - t1) * 1000, len(cts[0]))


def print_result(r: Result, n: int) -> None:
    enc_ops, dec_ops = r.ops_per_sec(n)
    print(
        f"  {r.name:40s} | "
        f"enc {r.encrypt_ms:8.1f} ms ({enc_ops:>9,.0f} ops/s) | "
        f"dec {r.decrypt_ms:8.1f} ms ({dec_ops:>9,.0f} ops/s) | "
        f"ct {r.ciphertext_len} B"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10_000, help="件数 (default: 10000)")
    args = parser.parse_args()

    print(f"Plaintext: {len(PLAINTEXT)} bytes (家計簿の 1 仕訳行相当)")
    print(f"N = {args.n:,} 件")
    print(f"{'-' * 110}")

    print_result(bench_aes_gcm(args.n), args.n)
    print_result(bench_chacha20(args.n), args.n)
    if HAVE_PYNACL:
        print_result(bench_xchacha20_pynacl(args.n), args.n)
    else:
        print("  XChaCha20-Poly1305 (pynacl) は pynacl 未インストールのためスキップ")
        print("  pip install pynacl で有効化")


if __name__ == "__main__":
    main()
