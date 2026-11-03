// Golden answers for the Python repeat engine (app/os/common/rrule.py), computed by
// vournalOS's own compiled RecurrenceService and rrule.util (NestJS, JS `rrule`).
//
//   node tests/vournal_os/golden/rrule_golden.mjs /path/to/vournalOS-backend
//
// Reads cases.json next to this file and writes rrule.json. A developer tool only:
// the Python tests read the committed rrule.json and never run Node.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node rrule_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const { RecurrenceService } = require(join(reference, 'dist/common/services/recurrence.service.js'));
const { validateRRule, normalizeRRule } = require(join(reference, 'dist/common/utils/rrule.util.js'));

const here = dirname(fileURLToPath(import.meta.url));
const cases = JSON.parse(readFileSync(join(here, 'cases.json'), 'utf8'));
const service = new RecurrenceService();
const iso = (d) => (d ? d.toISOString() : null);
const date = (s) => (s ? new Date(s) : undefined);
const options = (o = {}) => ({ ...o, until: date(o.until) });

const results = cases.map((c) => {
  let result;
  switch (c.op) {
    case 'validate':
      result = validateRRule(c.rule);
      break;
    case 'normalize':
      result = normalizeRRule(c.rule);
      break;
    case 'next':
      result = iso(service.next(c.rule, date(c.anchor), date(c.after), options(c.opts)));
      break;
    case 'between':
      result = service
        .between(c.rule, date(c.anchor), date(c.start), date(c.end), (c.exDates ?? []).map(date), options(c.opts))
        .map(iso);
      break;
    case 'monthlyFactor':
      result = service.monthlyFactor(c.rule);
      break;
    case 'describe':
      result = service.describe(c.rule, c.lang);
      break;
    default:
      throw new Error(`unknown op ${c.op}`);
  }
  return { ...c, result };
});
writeFileSync(join(here, 'rrule.json'), JSON.stringify(results, null, 1) + '\n');
console.log(`wrote ${results.length} golden answers`);
