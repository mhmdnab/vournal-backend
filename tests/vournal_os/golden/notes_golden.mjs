// Golden answers for app/os/notes.py make_excerpt / word_count, from vournalOS's own compiled code.
//   node tests/vournal_os/golden/notes_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node notes_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const { makeExcerpt } = require(join(reference, 'dist/modules/notes/notes.service.js'));
const here = dirname(fileURLToPath(import.meta.url));
const long = 'word '.repeat(80);
const inputs = [
  '', '   ', 'Plain text.', '# Title\n\nSome **bold** and _italic_ and ~~gone~~ text.',
  '- [ ] todo one\n- [x] done two\n* bullet\n1. numbered\n> quote',
  'Code: `inline` and\n```js\nconst x = 1;\n```\nafter', '![alt text](http://x/y.png) and [a link](http://x)',
  long, `${'x'.repeat(199)} 😀😀 tail`, `${'é'.repeat(150)} ${'ü'.repeat(100)}`, 'tabs\tand nbsp em space',
  '   ### deep heading   \n  - indented item', '**unclosed bold and __mixed_ marks*', 'مرحبا **بالعالم** - قائمة\n- عنصر',
  `${'a'.repeat(198)}  b`, 'line1\r\nline2\r\n\r\n', '[ ] not a checkbox at start', ' sep par',
];
const results = inputs.map((md) => ({
  md,
  excerpt: makeExcerpt(md),
  wordCount: md.trim() ? md.trim().split(/\s+/).length : 0,
}));
writeFileSync(join(here, 'notes.json'), JSON.stringify(results, null, 1) + '\n');
console.log(`wrote ${results.length} golden answers`);
