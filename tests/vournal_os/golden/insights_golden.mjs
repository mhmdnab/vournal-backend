// The insights slice's words and formats, and golden answers from its own pure code:
//  - app/os/data/insights_i18n.json: rule messages, the shared dictionary, enum labels,
//    relative-day words, en-US currency formats per supported currency, and Arabic month
//    names (ar-LB, Western digits), all read from vournalOS's compiled build;
//  - tests/vournal_os/golden/insights.json: runInsightRules over the fixtures below in
//    both languages, the habit helpers, money() and relDay().
//   node tests/vournal_os/golden/insights_golden.mjs /path/to/vournalOS-backend
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const reference = process.argv[2];
if (!reference) throw new Error('usage: node insights_golden.mjs /path/to/vournalOS-backend');
const require = createRequire(join(reference, 'package.json'));
const lib = (name) => join(reference, 'dist/modules/insights/lib', name);
const i18n = require(lib('i18n.js'));
const rules = require(lib('rules.js'));
const habits = require(lib('habits.js'));
const { RULE_MESSAGES } = require(lib('rules.messages.js'));
const { SUPPORTED_CURRENCIES } = require(join(reference, 'dist/common/constants/currencies.js'));

function literal(file, name) {
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

const ENUMS = literal(lib('i18n.js'), 'ENUMS');
const REL = literal(lib('i18n.js'), 'REL');
const currency = {};
for (const code of SUPPORTED_CURRENCIES) {
  const parts = (n, d) => new Intl.NumberFormat('en-US', { style: 'currency', currency: code, maximumFractionDigits: d, minimumFractionDigits: d }).format(n);
  const two = parts(1234.5, 2);
  const zero = parts(1234, 0);
  const at2 = two.indexOf('1,234.50');
  const at0 = zero.indexOf('1,234');
  currency[code] = { prefix: two.slice(0, at2), suffix: two.slice(at2 + 8) };
  if (zero.slice(0, at0) !== currency[code].prefix || zero.slice(at0 + 5) !== currency[code].suffix) throw new Error(`${code}: affixes differ`);
}
const ar = (options, month) => new Intl.DateTimeFormat('ar-LB-u-nu-latn', { timeZone: 'UTC', ...options }).format(new Date(Date.UTC(2026, month, 5)));
const months = { long: [], short: [] };
for (let m = 0; m < 12; m++) {
  months.long.push(ar({ month: 'long' }, m));
  months.short.push(ar({ month: 'short' }, m));
  if (ar({ day: 'numeric', month: 'long' }, m) !== `5 ${months.long[m]}`) throw new Error('day-month pattern changed');
  if (ar({ day: 'numeric', month: 'long', year: 'numeric' }, m) !== `5 ${months.long[m]} 2026`) throw new Error('day-month-year pattern changed');
}
const here = dirname(fileURLToPath(import.meta.url));
writeFileSync(join(here, '..', '..', '..', 'app/os/data/insights_i18n.json'),
  JSON.stringify({ messages: RULE_MESSAGES, shared: i18n.SHARED, enums: ENUMS, rel: REL, currency, months }) + '\n');

// ----- golden answers -----------------------------------------------------------------
const ALL = ['finance', 'tasks', 'documents', 'maintenance', 'people', 'habits', 'goals', 'projects'];
const base = { today: '2026-11-15', currency: 'USD', dayOfMonth: 15, daysInMonth: 30, sections: ALL };
const finance = (o = {}) => ({
  hasTransactions: true,
  thisMonthByCategory: [{ categoryId: 'c1', name: 'Shopping', amount: 320 }, { categoryId: 'c2', name: 'Food', amount: 150 }, { categoryId: null, name: 'Uncategorized', amount: 40 }],
  lastMonthSamePeriodByCategory: [{ categoryId: 'c1', name: 'Shopping', amount: 200 }, { categoryId: 'c2', name: 'Food', amount: 260 }, { categoryId: null, name: 'Uncategorized', amount: 39 }],
  incomeThisMonth: 2000, expensesThisMonth: 510, incomeLastMonthSamePeriod: 2000, expensesLastMonthSamePeriod: 900,
  budgets: [
    { id: 'b1', name: 'Food', spent: 520, amount: 500, currency: 'USD', percentUsed: 104, alertThreshold: 80, period: 'MONTHLY' },
    { id: 'b2', name: 'Fun', spent: 85, amount: 100, currency: 'EUR', percentUsed: 85, alertThreshold: 80, period: 'YEARLY' },
    { id: 'b3', name: 'Zero', spent: 5, amount: 0, currency: 'USD', percentUsed: 0, alertThreshold: 80, period: 'MONTHLY' },
  ],
  upcomingBills: [
    { id: 'r1', name: 'Rent', amountDefault: 700, daysUntil: -2, isOverdue: true, kind: 'BILL' },
    { id: 'r2', name: 'Netflix', amountDefault: 12.99, daysUntil: 3, isOverdue: false, kind: 'SUBSCRIPTION' },
    { id: 'r3', name: 'Gym', amountDefault: 25, daysUntil: 6, isOverdue: false, kind: 'BILL' },
  ],
  subscriptionsMonthly: 42.5, subscriptionCount: 3, daysSinceLastExpense: 1,
  recentExpenses: [{ id: 't1', title: 'Laptop', amount: 1200, daysAgo: 2 }, { id: 't2', title: 'Coffee', amount: 4, daysAgo: 0 }],
  averageExpense: 60, expenseSampleSize: 30,
  accounts: [{ id: 'a1', name: 'Visa', type: 'CREDIT_CARD', balance: -350.25, currency: 'USD' }, { id: 'a2', name: 'Checking', type: 'BANK', balance: -10, currency: 'EUR' },
             { id: 'a3', name: 'Cash', type: 'CASH', balance: 100, currency: 'USD' }],
  ...o,
});
const full = {
  ...base,
  finance: finance(),
  tasks: { overdueCount: 6, oldestOverdueDays: 12, dueTodayCount: 2, completedThisWeek: 9, completedLastWeekSamePeriod: 4 },
  documents: [{ id: 'd1', name: 'Passport', daysUntil: -3 }, { id: 'd2', name: 'ID', daysUntil: 10 }, { id: 'd3', name: 'Insurance', daysUntil: 45 }, { id: 'd4', name: 'Visa', daysUntil: 59 }],
  maintenance: [{ id: 'm1', title: 'Oil change', daysUntil: -5 }, { id: 'm2', title: 'Filters', daysUntil: -1 }, { id: 'm3', title: 'AC', daysUntil: -9 }, { id: 'm4', title: 'Car', daysUntil: -2 }],
  dates: [{ id: 'birthday:p1', title: "Sara's birthday", personName: 'Sara', kind: 'birthday', daysUntil: 0, turningAge: 31, link: '/people?id=p1' },
          { id: 'anniversary:p2', title: 'Anniversary — Omar', personName: 'Omar', kind: 'anniversary', daysUntil: 3, turningAge: 5, link: '/people?id=p2' },
          { id: 'date:x', title: 'Exam', personName: null, kind: 'date', daysUntil: 7, turningAge: null, link: '/people' }],
  habits: [{ id: 'h1', name: 'Read', dueToday: true, doneToday: false, currentStreak: 12, bestStreak: 20, frequency: 'DAILY' },
           { id: 'h2', name: 'Run', dueToday: true, doneToday: true, currentStreak: 30, bestStreak: 30, frequency: 'CUSTOM' },
           { id: 'h3', name: 'Swim', dueToday: false, doneToday: false, currentStreak: 4, bestStreak: 4, frequency: 'WEEKLY' }],
  goals: [{ id: 'g1', name: 'Car fund', type: 'FINANCIAL', progress: 30, timeProgress: 60, daysLeft: 100, currentValue: 3000, targetValue: 10000, monthlyRequired: 2100, currency: 'USD', unit: null },
          { id: 'g2', name: 'Marathon', type: 'FITNESS', progress: 90, timeProgress: 50, daysLeft: 20, currentValue: 36, targetValue: 40, currency: null, unit: 'km' },
          { id: 'g3', name: 'Laptop', type: 'FINANCIAL', progress: 50, timeProgress: 10, daysLeft: 20, currentValue: 500, targetValue: 1000, monthlyRequired: null, currency: 'EUR', unit: null }],
  projects: [{ id: 'p1', name: 'Website', progress: 40, daysLeft: 5 }, { id: 'p2', name: 'Thesis', progress: 65, daysLeft: 12 }, { id: 'p3', name: 'Done-ish', progress: 90, daysLeft: 3 }],
};
const fixtures = [
  full,
  { ...full, sections: ['tasks', 'habits'] },
  { ...base },
  { ...base, dayOfMonth: 2, finance: finance() },
  { ...base, finance: finance({ incomeThisMonth: 300, expensesThisMonth: 800 }) },
  { ...base, finance: finance({ incomeThisMonth: 2000, expensesThisMonth: 1950, incomeLastMonthSamePeriod: 2000, expensesLastMonthSamePeriod: 500 }) },
  { ...base, finance: finance({ hasTransactions: false, daysSinceLastExpense: null, upcomingBills: [{ id: 'r9', name: 'Water', amountDefault: 30, daysUntil: -1, isOverdue: true, kind: 'BILL' }, { id: 'r8', name: 'Power', amountDefault: 70.5, daysUntil: -4, isOverdue: true, kind: 'BILL' }] }) },
  { ...base, finance: finance({ daysSinceLastExpense: 9, averageExpense: 60, expenseSampleSize: 4, budgets: [], accounts: [] }) },
  { ...base, finance: finance({ upcomingBills: [1, 2, 3, 4].map((n) => ({ id: `u${n}`, name: `Bill ${n}`, amountDefault: n * 10.255, daysUntil: n, isOverdue: false, kind: 'BILL' })), subscriptionCount: 1, subscriptionsMonthly: 9.99 }) },
  { ...base, tasks: { overdueCount: 1, oldestOverdueDays: 1, dueTodayCount: 0, completedThisWeek: 0, completedLastWeekSamePeriod: 0 } },
  { ...base, tasks: { overdueCount: 0, oldestOverdueDays: null, dueTodayCount: 0, completedThisWeek: 1, completedLastWeekSamePeriod: 6 } },
  { ...base, tasks: { overdueCount: 3, oldestOverdueDays: 2, dueTodayCount: 0, completedThisWeek: 2, completedLastWeekSamePeriod: 0 } },
  { ...base, maintenance: [{ id: 'm1', title: 'Oil change', daysUntil: -5 }], habits: [{ id: 'h1', name: 'Read', dueToday: true, doneToday: true, currentStreak: 7, bestStreak: 9, frequency: 'DAILY' }, { id: 'h2', name: 'Run', dueToday: true, doneToday: true, currentStreak: 3, bestStreak: 3, frequency: 'DAILY' }] },
  { ...base, habits: [{ id: 'h1', name: 'Write', dueToday: true, doneToday: true, currentStreak: 11, bestStreak: 11, frequency: 'DAILY' }], goals: [{ id: 'g1', name: 'Savings', type: 'FINANCIAL', progress: 10, timeProgress: 95, daysLeft: 25, currentValue: 100, targetValue: 1000, currency: null, unit: null }] },
  { ...base, lang: 'xx', documents: [{ id: 'd9', name: 'License', daysUntil: 0 }] },
];
const ruleGolden = [];
for (const input of fixtures) for (const lang of ['en', 'ar']) {
  const run = { ...input, lang: input.lang === 'xx' ? undefined : lang };
  ruleGolden.push({ input: run, output: rules.runInsightRules(run) });
}

const day = (k) => new Date(`${k}T00:00:00Z`);
const habitDefs = [
  { id: 'd', name: 'Daily', frequency: 'DAILY', daysOfWeek: [], timesPerWeek: null, targetPerDay: 1, startDate: day('2026-09-01') },
  { id: 'c', name: 'MWF', frequency: 'CUSTOM', daysOfWeek: [1, 3, 5], timesPerWeek: null, targetPerDay: 1, startDate: day('2026-09-01') },
  { id: 'w', name: 'Weekly', frequency: 'WEEKLY', daysOfWeek: [], timesPerWeek: 3, targetPerDay: 1, startDate: day('2026-09-01') },
  { id: 't', name: 'Twice', frequency: 'DAILY', daysOfWeek: [], timesPerWeek: null, targetPerDay: 2, startDate: day('2026-10-20') },
  { id: 'e', name: 'Empty custom', frequency: 'CUSTOM', daysOfWeek: [], timesPerWeek: 9, targetPerDay: 0, startDate: day('2026-11-10') },
];
const logs = [];
for (let n = 0; n < 75; n++) {
  const k = new Date(Date.UTC(2026, 8, 1) + n * 86_400_000).toISOString().slice(0, 10);
  if (n % 7 !== 3) logs.push({ habitId: 'd', date: day(k), count: 1 });
  if (n % 2 === 0) logs.push({ habitId: 'c', date: day(k), count: 1 });
  if (n % 3 === 0) logs.push({ habitId: 'w', date: day(k), count: 1 });
  if (n > 50) logs.push({ habitId: 't', date: day(k), count: n % 4 === 0 ? 1 : 2 });
}
const idx = habits.indexHabitLogs(logs);
const habitGolden = [];
for (const today of ['2026-11-14', '2026-11-15', '2026-11-20']) for (const ws of [0, 1, 6]) for (const h of habitDefs) {
  habitGolden.push({
    habit: h.id, today, weekStartsOn: ws,
    dueToday: habits.isDueToday(h, idx, today, ws),
    doneToday: habits.isDoneOn(h, idx, today),
    currentStreak: habits.currentStreak(h, idx, today, ws, 400),
    best: habits.bestStreakInRange(h, idx, '2026-09-10', today, ws),
    completion: habits.completionInRange(h, idx, '2026-10-01', '2026-10-31', today),
    completionToday: habits.completionInRange(h, idx, '2026-11-01', '2026-11-30', today),
  });
}
const moneyGolden = [];
for (const lang of ['en', 'ar']) for (const code of ['USD', 'EUR', 'LBP', 'GBP', 'SAR', 'AED', 'JPY'].filter((c) => SUPPORTED_CURRENCIES.includes(c)))
  for (const amount of [0, 5, 12.5, -12.5, 0.125, 2.675, 1234.565, 9999.995, 10000, 12345.6, -0.004, 1e6])
    moneyGolden.push({ lang, code, amount, text: i18n.money(lang, amount, code) });
const relGolden = [];
for (const lang of ['en', 'ar']) for (const n of [-100, -11, -10, -3, -2, -1, 0, 1, 2, 3, 10, 11, 15, 100, 101]) relGolden.push({ lang, n, text: i18n.relDay(lang, n) });

writeFileSync(join(here, 'insights.json'), JSON.stringify({ rules: ruleGolden, habits: { defs: habitDefs, logs, answers: habitGolden }, money: moneyGolden, rel: relGolden }) + '\n');
console.log(`wrote ${ruleGolden.length} rule runs, ${habitGolden.length} habit answers, ${moneyGolden.length} amounts`);
