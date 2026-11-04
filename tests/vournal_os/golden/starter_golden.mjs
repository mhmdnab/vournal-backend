// The onboarding starter packs (tasks, habits, goals, a project, shopping and a note per
// template, in both languages), copied out of vournalOS's compiled starter-packs.js into
// app/os/data/starter_packs.json.
//   node tests/vournal_os/golden/starter_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node starter_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const { TEMPLATES, STARTER_PACKS } = require(join(reference, 'dist/modules/onboarding/starter-packs.js'));
const here = dirname(fileURLToPath(import.meta.url));
writeFileSync(join(here, '..', '..', '..', 'app/os/data/starter_packs.json'), JSON.stringify({ templates: TEMPLATES, packs: STARTER_PACKS }) + '\n');
console.log(`wrote ${TEMPLATES.length} starter packs`);
