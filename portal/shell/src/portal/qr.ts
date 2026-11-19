// Dependency-free QR Model 2 byte encoder, ECC L, versions 10/15, mask 0.
// Data block sizes and alignment positions are from ISO/IEC 18004 tables.
// Fixed versions bound memory and cover authenticator enrollment URIs (up to 520 UTF-8 bytes).
export function qrMatrix(text: string): boolean[][] {
  const bytes = new TextEncoder().encode(text);
  if (bytes.length > 520) throw new Error('Authenticator URI is too long to render as a QR code.');
  const version = bytes.length <= 271 ? 10 : 15;
  const dataSize = version === 10 ? 274 : 523;
  const blockSizes = version === 10 ? [68, 68, 69, 69] : [87, 87, 87, 87, 87, 88];
  const eccSize = version === 10 ? 18 : 22;
  const bits: number[] = [];
  const append = (value: number, count: number) => { for (let i = count - 1; i >= 0; i--) bits.push((value >>> i) & 1); };
  append(4, 4); append(bytes.length, 16);
  for (const byte of bytes) append(byte, 8);
  append(0, Math.min(4, dataSize * 8 - bits.length));
  while (bits.length % 8) bits.push(0);
  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((n, b) => n * 2 + b, 0));
  for (let pad = 0; data.length < dataSize; pad++) data.push(pad % 2 ? 0x11 : 0xec);
  const mul = (a: number, b: number) => {
    let product = 0;
    for (let i = 7; i >= 0; i--) { product = (product << 1) ^ ((product >>> 7) * 0x11d); product ^= ((b >>> i) & 1) * a; }
    return product;
  };
  const divisor = Array<number>(eccSize).fill(0); divisor[eccSize - 1] = 1;
  for (let i = 0, root = 1; i < eccSize; i++, root = mul(root, 2)) {
    for (let j = 0; j < eccSize; j++) { divisor[j] = mul(divisor[j], root); if (j + 1 < eccSize) divisor[j] ^= divisor[j + 1]; }
  }
  let offset = 0;
  const blocks = blockSizes.map((size) => {
    const block = data.slice(offset, offset += size);
    const ecc = Array<number>(eccSize).fill(0);
    for (const byte of block) {
      const factor = byte ^ ecc.shift()!; ecc.push(0);
      for (let i = 0; i < eccSize; i++) ecc[i] ^= mul(divisor[i], factor);
    }
    return { block, ecc };
  });
  const codewords: number[] = [];
  for (let i = 0; i < Math.max(...blockSizes); i++) for (const { block } of blocks) if (i < block.length) codewords.push(block[i]);
  for (let i = 0; i < eccSize; i++) for (const { ecc } of blocks) codewords.push(ecc[i]);
  const size = version * 4 + 17;
  const matrix = Array.from({ length: size }, () => Array<boolean>(size).fill(false));
  const reserved = matrix.map((row) => row.map(() => false));
  const set = (x: number, y: number, dark: boolean) => { matrix[y][x] = dark; reserved[y][x] = true; };
  for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const x = cx + dx, y = cy + dy, distance = Math.max(Math.abs(dx), Math.abs(dy));
      if (x >= 0 && y >= 0 && x < size && y < size) set(x, y, distance !== 2 && distance !== 4);
    }
  }
  const centers = version === 10 ? [6, 28, 50] : [6, 26, 48, 70];
  for (let iy = 0; iy < centers.length; iy++) for (let ix = 0; ix < centers.length; ix++) {
    if ((ix === 0 && iy === 0) || (ix === 0 && iy === centers.length - 1) || (ix === centers.length - 1 && iy === 0)) continue;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(centers[ix] + dx, centers[iy] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }
  let rem = version;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  const versionBits = (version << 12) | rem;
  for (let i = 0; i < 18; i++) { const a = size - 11 + i % 3, b = Math.floor(i / 3), dark = ((versionBits >>> i) & 1) !== 0; set(a, b, dark); set(b, a, dark); }
  const format = 0x77c4; // L, mask 0, BCH encoded and XOR masked.
  const bit = (i: number) => ((format >>> i) & 1) !== 0;
  for (let i = 0; i <= 5; i++) set(8, i, bit(i));
  set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
  for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
  for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
  for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
  set(8, size - 8, true);
  let index = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vertical = 0; vertical < size; vertical++) {
      const y = ((right + 1) & 2) === 0 ? size - 1 - vertical : vertical;
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        if (reserved[y][x]) continue;
        const dark = index < codewords.length * 8 && ((codewords[index >>> 3] >>> (7 - (index & 7))) & 1) !== 0;
        matrix[y][x] = dark !== ((x + y) % 2 === 0); index++;
      }
    }
  }
  return matrix;
}
