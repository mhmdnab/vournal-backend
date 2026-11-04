// The chatbot's system prompt and tool definitions, copied out of vournalOS's compiled
// assistant.prompt.js and assistant.tools.js into app/os/data/assistant.json: the model
// must see exactly what it saw before (the order of the tools is part of the prompt cache).
//   node tests/vournal_os/golden/assistant_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node assistant_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const { ASSISTANT_SYSTEM, contextBlock } = require(join(reference, 'dist/modules/assistant/assistant.prompt.js'));
const { ASSISTANT_TOOLS, DELETABLE } = require(join(reference, 'dist/modules/assistant/assistant.tools.js'));
const here = dirname(fileURLToPath(import.meta.url));

writeFileSync(join(here, '../../../app/os/data/assistant.json'), JSON.stringify({ system: ASSISTANT_SYSTEM, tools: ASSISTANT_TOOLS, deletable: DELETABLE }));
const contexts = [
  { now: '2026-11-04 09:05', weekday: 'Wednesday', timezone: 'Asia/Beirut', language: 'en', currency: 'USD', name: 'Owner' },
  { now: '2026-11-04 21:30', weekday: 'Wednesday', timezone: 'UTC', language: 'ar', currency: 'LBP', name: null },
];
writeFileSync(join(here, 'assistant.json'), JSON.stringify({ context: contexts.map((c) => ({ c, out: contextBlock(c) })) }, null, 1));
