// Notification texts: the message and plural tables (copied out of vournalOS's compiled
// notifications.messages.js into app/os/data/notification_texts.json) and golden answers
// from its own notificationTexts(): plurals, when(), date() and time().
//   node tests/vournal_os/golden/notifications_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node notifications_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const file = join(reference, 'dist/modules/notifications/notifications.messages.js');
const { notificationTexts } = require(file);

function literal(name) {
  const source = readFileSync(file, 'utf8');
  const start = source.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  let i = source.indexOf('{', start);
  const from = i;
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) break;
  }
  return new Function(`return ${source.slice(from, i + 1)};`)();
}
const EN = literal('EN');
const AR = literal('AR');
const P = literal('P');
const here = dirname(fileURLToPath(import.meta.url));
// weekday and month names as Intl writes them (en-GB writes "Sept"), Sunday first
const names = (locale, options, dates) => dates.map((d) => new Intl.DateTimeFormat(locale, { timeZone: 'UTC', ...options }).format(d));
const sundays = [...Array(7)].map((_, i) => new Date(Date.UTC(2026, 10, 1 + i)));
const firsts = [...Array(12)].map((_, i) => new Date(Date.UTC(2026, i, 15)));
const calendar = {
  en: { weekdays: names('en-GB', { weekday: 'short' }, sundays), months: names('en-GB', { month: 'short' }, firsts) },
  ar: { weekdays: names('ar-LB-u-nu-latn', { weekday: 'short' }, sundays), months: names('ar-LB-u-nu-latn', { month: 'short' }, firsts) },
};
writeFileSync(join(here, '..', '..', '..', 'app/os/data/notification_texts.json'), JSON.stringify({ messages: { en: EN, ar: AR }, plurals: P, calendar }) + '\n');

const golden = { plurals: [], when: [], date: [], time: [] };
for (const lang of ['en', 'ar']) {
  const T = notificationTexts(lang);
  for (const key of Object.keys(P.en)) for (const n of [0, 1, 2, 3, 10, 11, 99, 100, 101]) golden.plurals.push({ lang, key, n, text: T.n(key, n, { name: 'Sara' }) });
  for (const n of [0, 1, 2, 3, 11, 100]) golden.when.push({ lang, n, text: T.when(n) });
  for (const d of ['2026-01-01', '2026-02-28', '2026-03-29', '2026-07-04', '2026-10-08', '2026-11-04', '2026-12-31', '2028-02-29']) golden.date.push({ lang, date: d, text: T.date(d) });
  for (const fmt of ['12h', '24h']) for (const tz of ['Asia/Beirut', 'America/Los_Angeles', 'UTC']) for (const iso of ['2026-11-04T00:00:00Z', '2026-11-04T09:05:00Z', '2026-11-04T11:59:00Z', '2026-11-04T12:00:00Z', '2026-11-04T13:30:00Z', '2026-11-04T21:45:00Z', '2026-03-29T00:30:00Z'])
    golden.time.push({ lang, fmt, tz, iso, text: T.time(new Date(iso), tz, fmt) });
}
writeFileSync(join(here, 'notifications.json'), JSON.stringify(golden, null, 0) + '\n');
console.log(`wrote ${Object.keys(EN).length} messages, ${Object.keys(P.en).length} plurals, ${golden.time.length} times`);
