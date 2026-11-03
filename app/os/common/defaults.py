"""Option lists and the data a new vournal OS profile starts with.

Ports of vournalOS common/constants/sections.ts, currencies.ts and
modules/auth/default-data.ts: keep the values identical, parity tests compare them.
"""

from app.os.common.i18n import Lang, default_name

SECTION_KEYS = [
    "dashboard",
    "calendar",
    "tasks",
    "projects",
    "finance",
    "goals",
    "habits",
    "maintenance",
    "documents",
    "shopping",
    "meals",
    "health",
    "notes",
    "study",
    "work",
    "travel",
    "people",
    "reminders",
    "reports",
    "settings",
]
ALWAYS_ON_SECTIONS = ["dashboard", "settings"]
# New profiles: everything except the optional sections (picked during onboarding).
DEFAULT_ENABLED_SECTIONS = [
    k for k in SECTION_KEYS if k not in ("study", "work", "meals", "travel", "health")
]

DASHBOARD_WIDGET_IDS = [
    "dailyCompletion",
    "topPriorities",
    "todayTasks",
    "overdueTasks",
    "upcomingEvents",
    "reminders",
    "upcomingBills",
    "todayExpenses",
    "monthlyFinance",
    "savings",
    "accounts",
    "budgets",
    "birthdays",
    "goals",
    "habits",
    "deadlines",
    "insights",
    "quickActions",
]
DEFAULT_DASHBOARD_WIDGETS = [
    {"id": wid, "visible": True, "order": order} for order, wid in enumerate(DASHBOARD_WIDGET_IDS)
]

DATE_FORMATS = ["dd/MM/yyyy", "MM/dd/yyyy", "yyyy-MM-dd", "dd.MM.yyyy", "d MMM yyyy", "MMM d, yyyy"]
TIME_FORMATS = ["12h", "24h"]
LANGUAGES = [{"code": "en", "name": "English"}, {"code": "ar", "name": "العربية"}]
LANGUAGE_CODES = [lang["code"] for lang in LANGUAGES]
# 0 = Sunday, 1 = Monday, 6 = Saturday
WEEK_STARTS = [
    {"value": 0, "label": "Sunday"},
    {"value": 1, "label": "Monday"},
    {"value": 6, "label": "Saturday"},
]
WEEK_START_VALUES = [0, 1, 6]
THEMES = ["LIGHT", "DARK", "SYSTEM"]

DEFAULT_NOTIFICATION_PREFS: dict = {
    "inApp": True,
    "push": True,
    "email": False,
    "taskReminders": True,
    "eventReminders": True,
    "bills": True,
    "birthdays": True,
    "documents": True,
    "maintenance": True,
    "habits": True,
    "budgets": True,
    "health": True,
    "dailyDigestTime": "08:00",  # "HH:mm" in the user's timezone, or None = off
}

CURRENCIES = [
    {"code": "USD", "name": "US Dollar", "symbol": "$", "decimals": 2},
    {"code": "LBP", "name": "Lebanese Pound", "symbol": "L£", "decimals": 0},
    {"code": "EUR", "name": "Euro", "symbol": "€", "decimals": 2},
    {"code": "GBP", "name": "British Pound", "symbol": "£", "decimals": 2},
    {"code": "AED", "name": "UAE Dirham", "symbol": "AED", "decimals": 2},
    {"code": "SAR", "name": "Saudi Riyal", "symbol": "SAR", "decimals": 2},
    {"code": "QAR", "name": "Qatari Riyal", "symbol": "QAR", "decimals": 2},
    {"code": "KWD", "name": "Kuwaiti Dinar", "symbol": "KWD", "decimals": 2},
    {"code": "TRY", "name": "Turkish Lira", "symbol": "₺", "decimals": 2},
    {"code": "EGP", "name": "Egyptian Pound", "symbol": "E£", "decimals": 2},
    {"code": "JOD", "name": "Jordanian Dinar", "symbol": "JOD", "decimals": 2},
    {"code": "CAD", "name": "Canadian Dollar", "symbol": "CA$", "decimals": 2},
    {"code": "AUD", "name": "Australian Dollar", "symbol": "A$", "decimals": 2},
    {"code": "CHF", "name": "Swiss Franc", "symbol": "CHF", "decimals": 2},
    {"code": "JPY", "name": "Japanese Yen", "symbol": "¥", "decimals": 0},
    {"code": "CNY", "name": "Chinese Yuan", "symbol": "CN¥", "decimals": 2},
    {"code": "INR", "name": "Indian Rupee", "symbol": "₹", "decimals": 2},
]
SUPPORTED_CURRENCIES = [c["code"] for c in CURRENCIES]
DEFAULT_CURRENCY = "USD"

# Units of currency per 1 USD (approximate market values; user-editable).
DEFAULT_RATES_PER_USD: dict[str, str] = {
    "USD": "1",
    "LBP": "89500",
    "EUR": "0.92",
    "GBP": "0.79",
    "AED": "3.6725",
    "SAR": "3.75",
    "QAR": "3.64",
    "KWD": "0.307",
    "TRY": "41",
    "EGP": "48.5",
    "JOD": "0.709",
    "CAD": "1.37",
    "AUD": "1.52",
    "CHF": "0.88",
    "JPY": "150",
    "CNY": "7.2",
    "INR": "85",
}

_C = {
    "indigo": "#6366f1",
    "violet": "#8b5cf6",
    "purple": "#a855f7",
    "pink": "#ec4899",
    "rose": "#f43f5e",
    "red": "#ef4444",
    "orange": "#f97316",
    "amber": "#f59e0b",
    "yellow": "#eab308",
    "lime": "#84cc16",
    "green": "#22c55e",
    "emerald": "#10b981",
    "teal": "#14b8a6",
    "cyan": "#06b6d4",
    "sky": "#0ea5e9",
    "blue": "#3b82f6",
    "slate": "#64748b",
}

# Default categories per scope: (name, lucide icon, colour). Order is the sort order.
DEFAULT_CATEGORIES: dict[str, list[tuple[str, str, str]]] = {
    "EXPENSE": [
        ("Food", "utensils", _C["orange"]),
        ("Transportation", "car", _C["blue"]),
        ("Shopping", "shopping-bag", _C["pink"]),
        ("Bills", "receipt", _C["red"]),
        ("Entertainment", "clapperboard", _C["purple"]),
        ("Education", "graduation-cap", _C["sky"]),
        ("Health", "heart-pulse", _C["emerald"]),
        ("Subscriptions", "repeat", _C["indigo"]),
        ("Housing", "house", _C["amber"]),
        ("Other", "circle-ellipsis", _C["slate"]),
    ],
    "INCOME": [
        ("Salary", "briefcase", _C["emerald"]),
        ("Freelance", "laptop", _C["cyan"]),
        ("Business", "building-2", _C["indigo"]),
        ("Investments", "trending-up", _C["green"]),
        ("Other", "circle-plus", _C["slate"]),
    ],
    "TASK": [
        ("Personal", "user", _C["indigo"]),
        ("Work", "briefcase", _C["sky"]),
        ("Home", "house", _C["amber"]),
        ("Health", "heart-pulse", _C["emerald"]),
        ("Errands", "shopping-cart", _C["pink"]),
    ],
    "EVENT": [
        ("Personal", "user", _C["indigo"]),
        ("Work", "briefcase", _C["sky"]),
        ("Social", "users", _C["pink"]),
        ("Health", "heart-pulse", _C["emerald"]),
    ],
    "NOTE": [
        ("Ideas", "lightbulb", _C["yellow"]),
        ("Business Ideas", "rocket", _C["violet"]),
        ("Personal", "user", _C["indigo"]),
        ("Study", "book-open", _C["sky"]),
        ("Travel", "plane", _C["teal"]),
        ("Things to Remember", "bookmark", _C["rose"]),
    ],
    "GOAL": [
        ("Personal", "user", _C["indigo"]),
        ("Health & Fitness", "dumbbell", _C["emerald"]),
        ("Career", "briefcase", _C["sky"]),
        ("Learning", "book-open", _C["violet"]),
        ("Finance", "piggy-bank", _C["green"]),
    ],
    "HABIT": [
        ("Health", "heart-pulse", _C["emerald"]),
        ("Fitness", "dumbbell", _C["orange"]),
        ("Mindfulness", "brain", _C["violet"]),
        ("Learning", "book-open", _C["sky"]),
        ("Productivity", "zap", _C["amber"]),
    ],
    "PROJECT": [
        ("Personal", "user", _C["indigo"]),
        ("Work", "briefcase", _C["sky"]),
        ("Business", "rocket", _C["violet"]),
        ("Home", "house", _C["amber"]),
    ],
    "MAINTENANCE": [
        ("Home", "house", _C["amber"]),
        ("Car", "car", _C["blue"]),
        ("Personal", "user", _C["indigo"]),
    ],
    "DOCUMENT": [
        ("Identity", "id-card", _C["indigo"]),
        ("Travel", "plane", _C["teal"]),
        ("Vehicle", "car", _C["blue"]),
        ("Insurance", "shield-check", _C["emerald"]),
        ("Financial", "landmark", _C["green"]),
        ("Medical", "stethoscope", _C["rose"]),
        ("Other", "file-text", _C["slate"]),
    ],
    "SHOPPING": [
        ("Grocery", "shopping-basket", _C["green"]),
        ("Electronics", "smartphone", _C["sky"]),
        ("Clothes", "shirt", _C["pink"]),
        ("Home", "sofa", _C["amber"]),
        ("General", "shopping-cart", _C["slate"]),
    ],
    "GENERAL": [],
}


def default_category_rows(lang: Lang = "en") -> list[dict]:
    return [
        {
            "scope": scope,
            "name": default_name(name, lang),
            "icon": icon,
            "color": color,
            "sortOrder": i,
            "isDefault": True,
        }
        for scope, seeds in DEFAULT_CATEGORIES.items()
        for i, (name, icon, color) in enumerate(seeds)
    ]
