// Extract GoPro GPMF telemetry (IMU) from an MP4 into JSON.
// Usage: node extract.js <input.mp4> <output.json>
// Exits non-zero if the output was not successfully written.
const fs = require('fs');
const goproTelemetry = require('gopro-telemetry');
const gpmfExtract = require('gpmf-extract');

async function main() {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) {
    console.error('Usage: node extract.js <input.mp4> <output.json>');
    process.exit(2);
  }
  const buf = fs.readFileSync(input);
  const res = await gpmfExtract(buf);
  const telemetry = await new Promise((resolve, reject) => {
    try {
      goproTelemetry(res, {}, (tele) => resolve(tele));
    } catch (err) {
      reject(err);
    }
  });
  if (!telemetry || Object.keys(telemetry).length === 0) {
    throw new Error('empty telemetry');
  }
  fs.writeFileSync(output, JSON.stringify(telemetry, null, 2));
  console.log('Wrote', output, 'with', Object.keys(telemetry).length, 'top-level keys');
}

main().catch((err) => {
  console.error('GPMF extraction failed:', err && err.message ? err.message : err);
  process.exit(1);
});
