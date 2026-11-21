// Decryption in the browser with the built-in Web Crypto API (AES-256-GCM).
// Keys never leave this page; the server only ever sends ciphertext.

export function b64urlToBytes(s) {
  const t = s.replace(/-/g, '+').replace(/_/g, '/').replace(/=+$/, '');
  const bin = atob(t + '='.repeat((4 - (t.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export async function importKey(b64) {
  const raw = b64urlToBytes(b64);
  if (raw.length !== 32) throw new Error('bad key');
  return crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['decrypt']);
}

export async function openRow(key, row) {
  const aad = new TextEncoder().encode(`labtracker/v1|${row.user}|${row.id}|s`);
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64urlToBytes(row.nonce), additionalData: aad },
    key, b64urlToBytes(row.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}
