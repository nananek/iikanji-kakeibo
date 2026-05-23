// BIP-39 24 単語実装の Node 単体テスト。
//
// 公式テストベクトル (BIP-39 仕様の Trezor reference vectors) で検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const BIP39_URL = new URL(
  "../../../app/static/js/crypto/bip39.js",
  import.meta.url,
);
const {
  entropyToMnemonic,
  mnemonicToEntropy,
  generateMnemonic,
  deriveKeyFromMnemonic,
} = await import(BIP39_URL.href);


function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return out;
}

function bytesToHex(b) {
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}


// BIP-39 公式テストベクトル (https://github.com/trezor/python-mnemonic/blob/master/vectors.json より)
const VECTORS_256_BIT = [
  {
    entropy: "0000000000000000000000000000000000000000000000000000000000000000",
    mnemonic:
      "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art",
  },
  {
    entropy: "7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f",
    mnemonic:
      "legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth title",
  },
  {
    entropy: "8080808080808080808080808080808080808080808080808080808080808080",
    mnemonic:
      "letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic bless",
  },
  {
    entropy: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    mnemonic:
      "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo vote",
  },
];


for (const [i, v] of VECTORS_256_BIT.entries()) {
  test(`BIP-39 公式テストベクトル #${i + 1}: entropy → mnemonic`, async () => {
    const entropy = hexToBytes(v.entropy);
    const m = await entropyToMnemonic(entropy);
    assert.equal(m, v.mnemonic);
  });

  test(`BIP-39 公式テストベクトル #${i + 1}: mnemonic → entropy`, async () => {
    const e = await mnemonicToEntropy(v.mnemonic);
    assert.equal(bytesToHex(e), v.entropy);
  });
}


test("generateMnemonic は 24 単語を返す + 自己整合", async () => {
  const m = await generateMnemonic();
  const words = m.split(" ");
  assert.equal(words.length, 24);
  // 自分でデコードできる = 内部チェックサム整合
  const e = await mnemonicToEntropy(m);
  assert.equal(e.byteLength, 32);
});


test("entropyToMnemonic は 32B 以外を弾く", async () => {
  await assert.rejects(
    () => entropyToMnemonic(new Uint8Array(16)),
    /must be Uint8Array of 32 bytes/,
  );
});


test("mnemonicToEntropy: 単語数不一致は reject", async () => {
  await assert.rejects(
    () => mnemonicToEntropy("abandon abandon"),
    /must be 24 words/,
  );
});


test("mnemonicToEntropy: 未知の単語は reject", async () => {
  const bad =
    "notaword abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";
  await assert.rejects(
    () => mnemonicToEntropy(bad),
    /unknown word/,
  );
});


test("mnemonicToEntropy: チェックサム不一致は reject", async () => {
  // 公式ベクトル #1 の末尾を別の正規ワードに差し替えてチェックサムを壊す
  const bad =
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon";
  await assert.rejects(
    () => mnemonicToEntropy(bad),
    /checksum mismatch/,
  );
});


test("mnemonicToEntropy は大文字・連続空白を許容", async () => {
  const m =
    "  ABANDON  abandon ABANDON ABANDON abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon ART  ";
  const e = await mnemonicToEntropy(m);
  assert.equal(
    bytesToHex(e),
    "0000000000000000000000000000000000000000000000000000000000000000",
  );
});


test("deriveKeyFromMnemonic は 32B を返し、決定的", async () => {
  const m =
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";
  const k1 = await deriveKeyFromMnemonic(m);
  const k2 = await deriveKeyFromMnemonic(m);
  assert.equal(k1.byteLength, 32);
  assert.deepEqual([...k1], [...k2]);
});


test("deriveKeyFromMnemonic は異なる mnemonic で異なる鍵を返す", async () => {
  const m1 =
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";
  const m2 =
    "legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth title";
  const k1 = await deriveKeyFromMnemonic(m1);
  const k2 = await deriveKeyFromMnemonic(m2);
  assert.notDeepEqual([...k1], [...k2]);
});


test("deriveKeyFromMnemonic は不正 mnemonic で reject (チェックサム検証経由)", async () => {
  await assert.rejects(
    () => deriveKeyFromMnemonic("abandon abandon abandon"),
    /must be 24 words/,
  );
});


test("deriveKeyFromMnemonic は正規化 (大文字・空白) しても同じ鍵を返す", async () => {
  const a =
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";
  const b =
    "  ABANDON   abandon ABANDON  ABANDON abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon  ART  ";
  const k1 = await deriveKeyFromMnemonic(a);
  const k2 = await deriveKeyFromMnemonic(b);
  assert.deepEqual([...k1], [...k2]);
});
