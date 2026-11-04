// Smart capture and the food estimate: the system prompts and the structured-output
// formats exactly as vournalOS's SDK put them on the wire (into app/os/data/ai_prompts.json),
// and golden answers from its own captureUserMessage(), normalizeCapture(), matchPerson()
// and normalizeEstimate() (into tests/vournal_os/golden/ai.json).
//   node tests/vournal_os/golden/ai_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node ai_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const Anthropic = require('@anthropic-ai/sdk').default;
const { betaZodOutputFormat } = require('@anthropic-ai/sdk/helpers/beta/zod');
const { CAPTURE_SYSTEM, CaptureSchema, captureUserMessage } = require(join(reference, 'dist/modules/ai/capture.schema.js'));
const { FOOD_SYSTEM, FoodEstimateSchema, normalizeEstimate } = require(join(reference, 'dist/modules/ai/food.schema.js'));
const { normalizeCapture, matchPerson } = require(join(reference, 'dist/modules/ai/capture.normalize.js'));
const here = dirname(fileURLToPath(import.meta.url));

// what the SDK sends: a client whose fetch records the body and answers with an empty draft
async function wireFormat(schema, reply) {
  let body;
  const client = new Anthropic({
    apiKey: 'golden',
    fetch: async (_url, init) => {
      body = JSON.parse(init.body);
      const message = { id: 'msg_golden', type: 'message', role: 'assistant', model: 'golden', stop_reason: 'end_turn', stop_sequence: null,
                        content: [{ type: 'text', text: JSON.stringify(reply) }], usage: { input_tokens: 1, output_tokens: 1 } };
      return new Response(JSON.stringify(message), { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });
  await client.beta.messages.parse({ model: 'golden', max_tokens: 1, messages: [{ role: 'user', content: 'x' }],
                                     output_config: { effort: 'low', format: betaZodOutputFormat(schema) } });
  return body.output_config.format;
}

const blank = { kind: 'unknown', title: '', amount: null, currency: null, date: null, time: null, durationMinutes: null, category: null,
                account: null, list: null, quantity: null, priority: null, note: null, debtDirection: null, person: null };
const prompts = {
  captureSystem: CAPTURE_SYSTEM,
  foodSystem: FOOD_SYSTEM,
  captureFormat: await wireFormat(CaptureSchema, blank),
  foodFormat: await wireFormat(FoodEstimateSchema, { items: [] }),
};
writeFileSync(join(here, '../../../app/os/data/ai_prompts.json'), JSON.stringify(prompts));

const lookups = {
  expenseCategories: [{ id: 'c-food', name: 'Food & Dining' }, { id: 'c-taxi', name: 'Transport' }],
  incomeCategories: [{ id: 'c-salary', name: 'Salary' }],
  accounts: [{ id: 'a-cash', name: 'Cash' }, { id: 'a-visa', name: 'Visa  Card' }],
  shoppingLists: [{ id: 'l-home', name: 'Home' }],
  people: [{ id: 'p-ahmad', name: 'Ahmad Khalil' }, { id: 'p-rami', name: 'Rami' }, { id: 'p-sara1', name: 'Sara A' }, { id: 'p-sara2', name: 'Sara B' }],
  defaultCurrency: 'USD',
};
const raws = [
  { ...blank, kind: 'expense', title: 'lunch', amount: 12.345, currency: 'usd', date: '2026-11-03', category: ' food  & DINING', account: 'visa card' },
  { ...blank, kind: 'expense', title: 'taxi', amount: 0 },
  { ...blank, kind: 'expense', title: 'taxi', amount: 300000, currency: 'XYZ', category: 'Taxis' },
  { ...blank, kind: 'income', title: 'Salary', amount: 2000, category: 'salary', date: '2026-02-30' },
  { ...blank, kind: 'event', title: 'Dentist', date: '2026-11-05', time: '9:05', durationMinutes: 2000.6 },
  { ...blank, kind: 'event', title: 'Call', time: '24:00', durationMinutes: -5 },
  { ...blank, kind: 'reminder', title: 'اتصل بأمي', date: '2026-11-05', time: '18:30', note: '   ' },
  { ...blank, kind: 'task', title: '  Pay rent  ', priority: 'HIGH', note: ' before Friday ' },
  { ...blank, kind: 'note', title: '', note: 'idea' },
  { ...blank, kind: 'shopping', title: 'Milk', quantity: 2.555, list: 'home', priority: 'LOW' },
  { ...blank, kind: 'habit', title: 'Run every morning', amount: 5 },
  { ...blank, kind: 'debt', title: '', amount: 50, debtDirection: 'OWED_TO_ME', person: 'ahmad' },
  { ...blank, kind: 'debt', title: 'dinner', amount: 20, debtDirection: 'I_OWE', person: 'Sara', currency: 'lbp' },
  { ...blank, kind: 'debt', title: 'loan', amount: 200, debtDirection: null, person: 'Rami' },
  { ...blank, kind: 'debt', title: 'loan', amount: 200, debtDirection: 'I_OWE', person: '  ' },
  { ...blank, kind: 'unknown', title: 'hello' },
  { ...blank, kind: 'task', title: 'x'.repeat(250), note: 'y'.repeat(1200) },
];
const meals = [
  { items: [{ name: ' Manoushe zaatar ', servings: 2, serving: '1 piece', calories: 560.6, protein: 14.26, carbs: 70, fat: null },
            { name: 'Laban', servings: 0, serving: null, calories: 120, protein: -1, carbs: 900, fat: 4.44 }] },
  { items: [{ name: 'Rice', servings: 1.555, serving: 'cup', calories: 205.5, protein: 4.25, carbs: 44.45, fat: 0.05 },
            { name: 'Bread', servings: 2.345, serving: 'slice', calories: 160.49, protein: 0.15, carbs: 1.005, fat: 499.96 }] },
  { items: [{ name: '', servings: 1, serving: 'x', calories: 10, protein: null, carbs: null, fat: null },
            { name: 'Cake', servings: 75.123, serving: 'slice', calories: 9000, protein: null, carbs: null, fat: null },
            { name: 'Water', servings: 1, serving: 'cup', calories: -1, protein: null, carbs: null, fat: null }] },
  { items: Array.from({ length: 17 }, (_, i) => ({ name: `Item ${i}`, servings: 1, serving: 'p', calories: i, protein: null, carbs: null, fat: null })) },
];
const contexts = [
  { today: '2026-11-04', weekday: 'Wednesday', timezone: 'Asia/Beirut', language: 'ar', defaultCurrency: 'LBP',
    expenseCategories: ['Food', 'Transport'], incomeCategories: [], accounts: ['Cash'], shoppingLists: ['Home', 'Work'] },
];
const golden = {
  capture: raws.map((raw) => ({ raw, out: normalizeCapture(raw, lookups) })),
  lookups,
  people: ['Ahmad', 'ahmad khalil', 'Sara', 'Rami', 'Nobody'].map((name) => ({ name, out: matchPerson(lookups.people, name)?.id ?? null })),
  estimate: meals.map((raw) => ({ raw, out: normalizeEstimate(raw) })),
  userMessage: contexts.map((ctx) => ({ ctx, text: 'ghada 12$ mbere7', out: captureUserMessage('ghada 12$ mbere7', ctx) })),
};
writeFileSync(join(here, 'ai.json'), JSON.stringify(golden, null, 1));
