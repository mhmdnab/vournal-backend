// Golden prayer times for the Python port of `adhan` (app/os/common/adhan.py) and of
// vournalOS's prayer.util (app/os/common/prayer.py), computed by NestJS's own compiled
// prayer.util with the adhan it pins. Also copies the Lebanese calendar table out of
// aicp-beirut-2026.js into app/os/data/aicp_beirut.json.
//
//   TZ=UTC node tests/vournal_os/golden/prayer_golden.mjs /path/to/vournalOS-backend
//
// The answers must not depend on TZ; run it under another TZ and the output is identical.
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node prayer_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const util = require(join(reference, 'dist/common/utils/prayer.util.js'));
const aicp = require(join(reference, 'dist/common/data/aicp-beirut-2026.js'));

const here = dirname(fileURLToPath(import.meta.url));
const backend = join(here, '..', '..', '..');
writeFileSync(join(backend, 'app/os/data/aicp_beirut.json'), JSON.stringify({ year: aicp.AICP_YEAR, days: aicp.AICP_BEIRUT }) + '\n');

const places = {
  beirut: [33.8938, 35.5018],
  tripoli: [34.4367, 35.8497],
  london: [51.5074, -0.1278],
  oslo: [59.9139, 10.7522],
  reykjavik: [64.1466, -21.9426],
  tromso: [69.6492, 18.9553],
  sydney: [-33.8688, 151.2093],
  losAngeles: [34.0522, -118.2437],
  auckland: [-36.8485, 174.7633],
  mecca: [21.4225, 39.8262],
  honolulu: [21.3069, -157.8583],
  quito: [-0.1807, -78.4678],
};
const dates = ['2026-01-15', '2026-02-28', '2026-03-08', '2026-03-29', '2026-06-21', '2026-09-23', '2026-10-25', '2026-11-01', '2026-12-21', '2026-12-31', '2024-02-29', '2028-02-29', '2027-07-04'];
const iso = (d) => (Number.isNaN(d.getTime()) ? null : d.toISOString());
const day = (t) => Object.fromEntries(util.PRAYERS.map((p) => [p, iso(t[p])]));

const times = [];
for (const [place, [lat, lng]] of Object.entries(places)) {
  for (const date of dates) {
    for (const method of util.PRAYER_METHODS) {
      const madhabs = method === 'MuslimWorldLeague' || method === 'LebanonAICP' || method === 'Karachi' ? ['shafi', 'hanafi'] : ['shafi'];
      for (const madhab of madhabs) {
        times.push({ place, lat, lng, date, method, madhab, times: day(util.prayerTimes({ lat, lng, method, madhab }, date)) });
      }
    }
  }
}
const adjustments = { fajr: 7, sunrise: -30, dhuhr: 30, asr: 0, maghrib: -3, isha: 1 };
times.push({ place: 'beirut', lat: 33.8938, lng: 35.5018, date: '2026-11-03', method: 'MuslimWorldLeague', madhab: 'shafi', adjustments, times: day(util.prayerTimes({ lat: 33.8938, lng: 35.5018, method: 'MuslimWorldLeague', madhab: 'shafi', adjustments }, '2026-11-03')) });

const settings = { cityId: 'beirut', lat: 33.8938, lng: 35.5018, method: 'LebanonAICP', madhab: 'shafi', adjustments: util.NO_ADJUSTMENTS };
const anchorCases = [
  { start: { prayer: 'maghrib', offset: 15 } },
  { start: { prayer: 'fajr', offset: -20 } },
  { start: { prayer: 'maghrib', offset: 0 }, end: { prayer: 'isha', offset: 0 } },
  { start: { prayer: 'isha', offset: 0 }, end: { prayer: 'fajr', offset: 0 } },
  { start: { prayer: 'asr', offset: 30 }, end: { prayer: 'asr', offset: 10 } },
];
const anchors = [];
for (const date of ['2026-03-28', '2026-03-29', '2026-10-24', '2026-12-31']) {
  for (const a of anchorCases) {
    const r = util.resolveAnchor(settings, date, a);
    anchors.push({ date, anchor: a, start: iso(r.start), end: r.end ? iso(r.end) : null });
  }
}

const reads = [
  { start: { prayer: 'asr', offset: -10 } },
  { start: { prayer: 'nope', offset: 0 } },
  { start: { prayer: 'asr', offset: 999 } },
  { start: { prayer: 'asr', offset: 240 } },
  { start: { prayer: 'asr', offset: -241 } },
  { start: { prayer: 'asr', offset: 1.5 } },
  { start: { prayer: 'asr', offset: '5' } },
  { start: { prayer: 'asr', offset: 5 }, end: { prayer: 'isha', offset: 2.5 } },
  { start: { prayer: 'asr', offset: 5 }, end: null, extra: 1 },
  null,
  'x',
  [],
].map((raw) => ({ raw, read: util.readAnchor(raw) }));
const settingsReads = [
  { lat: 33.9, lng: 35.5, method: 'X' },
  { lat: 200, lng: 0 },
  { lat: '33', lng: 35 },
  { lat: 90, lng: -180, method: 'Turkey', madhab: 'hanafi', cityId: 7, adjustments: { fajr: 31, isha: -30, dhuhr: 2.5, asr: '3' } },
  { cityId: 'x', lat: -90, lng: 180, madhab: 'HANAFI' },
  null,
  [],
].map((raw) => ({ raw, read: util.readPrayerSettings(raw) }));

writeFileSync(join(here, 'prayer.json'), JSON.stringify({ times, anchors, reads, settingsReads }) + '\n');
console.log(`wrote ${times.length} days, ${anchors.length} anchors, ${reads.length + settingsReads.length} reads`);
