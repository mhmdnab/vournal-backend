// The trip packing presets, in both languages, copied out of vournalOS's compiled
// packing-templates.js (its tables aren't exported, so they're read through its own
// packingTemplate()) into app/os/data/packing_templates.json.
//   node tests/vournal_os/golden/packing_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node packing_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const { packingTemplate, matchExistingGroup } = require(join(reference, 'dist/modules/trips/packing-templates.js'));
const { PACKING_PRESETS } = require(join(reference, 'dist/modules/trips/dto/packing.dto.js'));

const presets = {};
const groups = new Map();
for (const preset of PACKING_PRESETS) {
  const en = packingTemplate(preset, 'en');
  const ar = packingTemplate(preset, 'ar');
  presets[preset] = en.map((item, i) => ({ group: { en: item.group, ar: ar[i].group }, name: { en: item.name, ar: ar[i].name }, quantity: item.quantity ?? null }));
  en.forEach((item, i) => groups.set(item.group, ar[i].group));
}
const groupPairs = [...groups].map(([en, ar]) => ({ en, ar }));
for (const { en, ar } of groupPairs) {
  if (matchExistingGroup(en, [` ${ar.toUpperCase()} `]) !== ` ${ar.toUpperCase()} `) throw new Error(`group pair ${en}/${ar} not matched`);
}
const here = dirname(fileURLToPath(import.meta.url));
writeFileSync(join(here, '..', '..', '..', 'app/os/data/packing_templates.json'), JSON.stringify({ presets, groups: groupPairs }) + '\n');
console.log(`wrote ${PACKING_PRESETS.length} presets, ${groupPairs.length} groups`);
