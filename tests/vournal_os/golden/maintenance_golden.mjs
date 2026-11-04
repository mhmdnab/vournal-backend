// Maintenance templates, their group labels and the expense categories "mark done" looks
// for, copied out of vournalOS's compiled maintenance.templates.js into
// app/os/data/maintenance_templates.json.
//   node tests/vournal_os/golden/maintenance_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node maintenance_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const t = require(join(reference, 'dist/modules/maintenance/maintenance.templates.js'));
const areas = ['HOME', 'CAR', 'PERSONAL', 'HEALTH', 'OTHER'];
const out = {
  templates: t.MAINTENANCE_TEMPLATES,
  groupLabels: t.TEMPLATE_GROUP_LABELS,
  expenseCandidates: Object.fromEntries(areas.map((a) => [a, t.expenseCategoryCandidates(a)])),
  categoryKeys: Object.fromEntries([...new Set(t.MAINTENANCE_TEMPLATES.map((x) => x.categoryName).filter(Boolean))].map((n) => [n, t.categoryNameKeys(n)])),
};
const here = dirname(fileURLToPath(import.meta.url));
writeFileSync(join(here, '..', '..', '..', 'app/os/data/maintenance_templates.json'), JSON.stringify(out) + '\n');
console.log(`wrote ${out.templates.length} templates`);
