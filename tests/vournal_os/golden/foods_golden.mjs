// The foods catalogue and activity METs, copied out of vournalOS's compiled foods.catalog.js
// (its rows aren't exported), plus golden search answers from its own searchCatalog.
//   node tests/vournal_os/golden/foods_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node foods_golden.mjs /path/to/vournalOS-backend');
const compiled = join(reference, 'dist/modules/health/foods.catalog.js');
const require = createRequire(join(reference, 'package.json'));
const catalog = require(compiled);
const source = readFileSync(compiled, 'utf8');
const literal = source.slice(source.indexOf('const ROWS = [') + 'const ROWS = '.length, source.indexOf('];', source.indexOf('const ROWS = [')) + 1);
const rows = new Function(`return ${literal};`)();
if (rows.length !== catalog.CATALOG_SIZE) throw new Error('row count mismatch');
const here = dirname(fileURLToPath(import.meta.url));
const backend = join(here, '..', '..', '..');
writeFileSync(join(backend, 'app/os/data/foods_catalog.json'), JSON.stringify({ rows, activities: catalog.ACTIVITIES }, null, 0) + '\n');
const queries = ['manoushe', 'man2oushe jebne', 'منقوشة', 'اكعكة', 'شاورما دجاج', 'banana', 'أفوكادو', '   ', 'chicken', 'رز', 'Knéfé', 'tea', 'ة', 'fattoush salad', 'labne', 'egg', 'شاي', "ka'ak", 'zz'];
const golden = [];
for (const q of queries) for (const lang of ['en', 'ar']) golden.push({ q, lang, results: catalog.searchCatalog(q, lang).map((f) => f.id) });
golden.push({ q: 'a', lang: 'en', limit: 500, results: catalog.searchCatalog('a', 'en', 500).map((f) => f.id) });
writeFileSync(join(here, 'foods.json'), JSON.stringify(golden, null, 1) + '\n');
console.log(`wrote ${rows.length} foods, ${catalog.ACTIVITIES.length} activities, ${golden.length} searches`);
