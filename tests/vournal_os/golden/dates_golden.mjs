// Golden answers for app/os/common/dates.py, computed by vournalOS's compiled DateService.
//   node tests/vournal_os/golden/dates_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node dates_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const { DateService } = require(join(reference, 'dist/common/services/date.service.js'));
const here = dirname(fileURLToPath(import.meta.url));
const cases = JSON.parse(readFileSync(join(here, 'date_cases.json'), 'utf8'));
const dates = new DateService();
const isIso = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T.*Z$/.test(v);
const out = (v) => (v instanceof Date ? v.toISOString() : v && typeof v === 'object' ? { from: v.from.toISOString(), to: v.to.toISOString() } : v);
const results = cases.map((c) => {
  const args = c.args.map((a, i) => (isIso(a) && !(c.method === 'parseDateOnly') ? new Date(a) : a));
  let result;
  try {
    result = out(dates[c.method](...args));
  } catch (err) {
    result = { error: String(err.message) };
  }
  return { ...c, result };
});
writeFileSync(join(here, 'dates.json'), JSON.stringify(results, null, 1) + '\n');
console.log(`wrote ${results.length} golden answers`);
