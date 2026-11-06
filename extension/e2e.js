// End-to-end encryption for the Handle relay: the browser half of e2e.py.
// AES-256-CTR + HMAC-SHA256 (encrypt-then-MAC), keys derived from the shared
// pairing key, wire format base64url(iv[16] || ciphertext || tag[32]).
// The IV's low 8 bytes are zero so WebCrypto's 64-bit counter and OpenSSL's
// 128-bit counter agree. The relay never sees the key.
const E2E = (() => {
  const te = new TextEncoder();
  const subtle = (globalThis.crypto || {}).subtle;
  const b64u = (buf) => {
    let s = "";
    const b = new Uint8Array(buf);
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const unb64u = (s) => {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    s += "=".repeat((4 - (s.length % 4)) % 4);
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };
  const cache = new Map();
  async function keys(rawB64u) {
    if (cache.has(rawB64u)) return cache.get(rawB64u);
    const raw = unb64u(rawB64u);
    const k = await subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const ek = new Uint8Array(await subtle.sign("HMAC", k, te.encode("handle-enc")));
    const mk = new Uint8Array(await subtle.sign("HMAC", k, te.encode("handle-mac")));
    const out = {
      enc: await subtle.importKey("raw", ek, { name: "AES-CTR" }, false, ["encrypt", "decrypt"]),
      mac: await subtle.importKey("raw", mk, { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]),
    };
    cache.set(rawB64u, out);
    return out;
  }
  async function seal(keyB64u, text) {
    const { enc, mac } = await keys(keyB64u);
    const iv = new Uint8Array(16);
    crypto.getRandomValues(iv.subarray(0, 8));
    const ct = new Uint8Array(await subtle.encrypt({ name: "AES-CTR", counter: iv, length: 64 }, enc, te.encode(text)));
    const body = new Uint8Array(16 + ct.length);
    body.set(iv, 0); body.set(ct, 16);
    const tag = new Uint8Array(await subtle.sign("HMAC", mac, body));
    const out = new Uint8Array(body.length + 32);
    out.set(body, 0); out.set(tag, body.length);
    return b64u(out);
  }
  async function open(keyB64u, sealed) {
    const { enc, mac } = await keys(keyB64u);
    const raw = unb64u(sealed);
    if (raw.length < 48) throw new Error("e2e: too short");
    const body = raw.subarray(0, raw.length - 32), tag = raw.subarray(raw.length - 32);
    if (!(await subtle.verify("HMAC", mac, tag, body))) throw new Error("e2e: bad tag");
    const pt = await subtle.decrypt({ name: "AES-CTR", counter: body.subarray(0, 16), length: 64 }, enc, body.subarray(16));
    return new TextDecoder().decode(pt);
  }
  return { seal, open, b64u, unb64u };
})();
if (typeof module !== "undefined") module.exports = E2E;
