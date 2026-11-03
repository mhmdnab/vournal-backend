// Golden answers for app/os/common/jsparse.py and collation.py: how vournalOS's
// JavaScript read dates, numbers and decimals and sorted titles. Run in UTC, like Render:
//   TZ=UTC node tests/vournal_os/golden/jsparse_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: TZ=UTC node jsparse_golden.mjs /path/to/vournalOS-backend');
if (new Date(0).getTimezoneOffset() !== 0) throw new Error('run with TZ=UTC');
const require = createRequire(join(reference, 'package.json'));
const validator = require('validator');
const { isNumber } = require('class-validator');
const here = dirname(fileURLToPath(import.meta.url));

const dateInputs = [
  '2026-11-05', '2026-02-28', '2026-02-29', '2026-02-30', '2026-02-31', '2026-02-32', '2024-02-29', '2024-02-30',
  '2026-04-31', '2026-13-01', '2026-00-10', '2026-11-00', '2026-11', '2026', '+2026-11-05', '20261105', '2026-W45-3', '2026-309',
  '2026-11-05T10:00:00Z', '2026-11-05T10:00:00.123Z', '2026-11-05T10:00:00.123456Z', '2026-11-05T10:00:00,5Z',
  '2026-11-05T10:00', '2026-11-05 10:00', '2026-11-05T10', '2026-11-05T1000', '2026-11-05T10:00:00+03:00', '2026-11-05T10:00:00+0300',
  '2026-11-05T10:00:00-05', '2026-11-05t10:00:00z', '2026-02-30T22:30:00Z', '2026-11-05T24:00:00Z', '2026-11-05T24:00', '2026-11-05T25:00:00Z',
  '2026-11-05T10:60:00Z', '2026-11-05T10:00:60Z', '2026-11-05T23:59:59.999+14:00', '٢٠٢٦-01-01', '2026-11-05T10:00:00Z ', ' 2026-11-05', '',
];
const dates = dateInputs.map((value) => {
  const parsed = new Date(value);
  return { value, iso: validator.isISO8601(value), date: Number.isNaN(parsed.getTime()) ? null : parsed.toISOString() };
});

const numberInputs = [
  '12', ' 12 ', ' 12\t', '', '   ', '+5', '-5', '.5', '5.', '1e3', '1E3', '1e400', '-1e400', '1_000', '1,5', '0x10', '0X1f', '-0x10',
  '0b11', '0o7', '0x', 'Infinity', '-Infinity', '+Infinity', 'infinity', 'inf', 'nan', 'NaN', '12abc', '007', '1.2.3', '--1', '٣',
];
const numbers = numberInputs.map((value) => {
  const n = Number(value);
  return { value, number: Number.isNaN(n) ? 'NaN' : n === Infinity ? 'Infinity' : n === -Infinity ? '-Infinity' : n };
});

const floats = [0, 12, 12.5, 0.1, 0.00005, 0.000001, 1e-7, 1.5e-7, 0.30000000000000004, 123.456, 1e20, 1.5e20, 1e21, 1.5e21, 99999999.99, -0.001, 5e-324];
const strings = floats.map((value) => {
  let decimals2;
  try {
    decimals2 = isNumber(value, { maxDecimalPlaces: 2 });
  } catch (err) {
    decimals2 = 'throws';
  }
  return { value, string: String(value), decimals2 };
});

const titles = [
  'netflix', 'Netflix', 'NETFLIX', 'Nétflix', 'apple', 'Apple', 'Äpfel', 'zebra', 'Zebra', 'éclair', 'eclair', 'Eclair',
  '1x', '10x', '2x', '[x]', '(x)', '_x', '-x', ' x', 'x', 'X', 'x1', 'x10', 'x2', 'a b', 'ab', 'a-b', 'a_b', 'a.b',
  'Gym', 'gym session', 'Gym session', 'Rent', 'rent', 'Ramadan', 'رمضان', 'إيجار', 'ضريبة', '😀 party', 'Ωmega', 'omega',
];
const sorted = [...titles].sort((a, b) => a.localeCompare(b));

writeFileSync(join(here, 'jsparse.json'), JSON.stringify({ dates, numbers, strings, titles: sorted }, null, 1) + '\n');
console.log(`wrote ${dates.length} dates, ${numbers.length} numbers, ${strings.length} floats, ${sorted.length} titles`);
