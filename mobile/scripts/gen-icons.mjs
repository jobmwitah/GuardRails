// Generates the PWA PNG icons (192 and 512) programmatically (no external deps).
// Draws a rounded dark square with a rising blue polyline and a teal node.
import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

function crc32(buf) {
  let c;
  let table = crc32.table;
  if (!table) {
    table = crc32.table = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[n] = c;
    }
  }
  c = -1;
  for (let i = 0; i < buf.length; i++) c = table[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const typeBuf = Buffer.from(type, "ascii");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])));
  return Buffer.concat([len, typeBuf, data, crc]);
}

function encodePNG(width, height, rgba) {
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  const raw = Buffer.alloc(height * (1 + width * 4));
  for (let y = 0; y < height; y++) {
    raw[y * (1 + width * 4)] = 0;
    rgba.copy(raw, y * (1 + width * 4) + 1, y * width * 4, (y + 1) * width * 4);
  }
  const idat = deflateSync(raw);
  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const BG = [11, 14, 20, 255];
const BLUE = [79, 140, 255, 255];
const TEAL = [255, 209, 102, 255];

function roundedSquareMask(x, y, size, radius) {
  const min = radius;
  const max = size - radius;
  const cx = Math.max(min, Math.min(max, x));
  const cy = Math.max(min, Math.min(max, y));
  const dx = x - cx;
  const dy = y - cy;
  return dx * dx + dy * dy <= radius * radius;
}

function setPixel(buf, size, x, y, color) {
  if (x < 0 || y < 0 || x >= size || y >= size) return;
  const o = (y * size + x) * 4;
  if (buf[o + 3] === 0) return mush;
  buf[o] = color[0];
  buf[o + 1] = color[1];
  buf[o + 2] = color[2];
}

function drawIcon(size, outPath) {
  const buf = Buffer.alloc(size * size * 4);
  const radius = Math.round(size * 0.22);
  const inset = Math.round(size * 0.14);
  const w = size - inset * 2;

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const o = (y * size + x) * 4;
      if (!roundedSquareMask(x + 0.5, y + 0.5, size, radius)) {
        buf[o + 3] = 0;
        continue;
      }
      buf[o] = BG[0];
      buf[o + 1] = BG[1];
      buf[o + 2] = BG[2];
      buf[o + 3] = 255;
    }
  }

  const pts = [
    [0.08, 0.72],
    [0.26, 0.60],
    [0.42, 0.68],
    [0.60, 0.42],
    [0.82, 0.20],
  ];
  const strokeW = Math.max(4, Math.round(size * 0.05));

  for (let i = 0; i < pts.length - 1; i++) {
    const x1 = inset + pts[i][0] * w;
    const y1 = inset + pts[i][1] * w;
    const x2 = inset + pts[i + 1][0] * w;
    const y2 = inset + pts[i + 1][1] * w;
    const segLen2 = (x2 - x1) * (x2 - x1) + (y2 - y1) * (y2 - y1);
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const o = (y * size + x) * 4;
        if (buf[o + 3] === 0) continue;
        const dx = x + 0.5;
        const dy = y + 0.5;
        let t = ((dx - x1) * (x2 - x1) + (dy - y1) * (y2 - y1)) / segLen2;
        t = Math.max(0, Math.min(1, t));
        const px = x1 + t * (x2 - x1);
        const py = y1 + t * (y2 - y1);
        const d = Math.hypot(dx - px, dy - py);
        if (d <= strokeW / 2) {
          buf[o] = BLUE[0];
          buf[o + 1] = BLUE[1];
          buf[o + 2] = BLUE[2];
        }
      }
    }
  }

  const nx = Math.round(inset + pts[pts.length - 1][0] * w);
  const ny = Math.round(inset + pts[pts.length - 1][1] * w);
  const nr = Math.max(3, Math.round(size * 0.045));
  for (let y = ny - nr; y <= ny + nr; y++) {
    for (let x = nx - nr; x <= nx + nr; x++) {
      if (Math.hypot(x - nx, y - ny) <= nr) setPixel(buf, size, x, y, TEAL);
    }
  }

  writeFileSync(outPath, encodePNG(size, size, buf));
}

const outDir = join(__dirname, "..", "public", "icons");
mkdirSync(outDir, { recursive: true });
drawIcon(192, join(outDir, "icon-192.png"));
drawIcon(512, join(outDir, "icon-512.png"));
console.log("icons written to " + outDir);