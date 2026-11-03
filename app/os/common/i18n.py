"""English/Arabic helpers for server-made text (port of vournalOS common/i18n).

Only what the ported sections use so far; later phases add the message helpers.
"""

from typing import Literal

Lang = Literal["en", "ar"]


def to_lang(code: str | None) -> Lang:
    return "ar" if code == "ar" else "en"


# Names of the data seeded for a new profile (default categories, the "Cash"
# account, the "Grocery" list). Switching language renames rows that still carry
# an untouched default name.
_AR: dict[str, str] = {
    "Food": "الطعام",
    "Transportation": "المواصلات",
    "Shopping": "التسوّق",
    "Bills": "الفواتير",
    "Entertainment": "الترفيه",
    "Education": "التعليم",
    "Health": "الصحة",
    "Subscriptions": "الاشتراكات",
    "Housing": "السكن",
    "Other": "أخرى",
    "Salary": "الراتب",
    "Freelance": "العمل الحر",
    "Business": "الأعمال",
    "Investments": "الاستثمارات",
    "Personal": "شخصي",
    "Work": "العمل",
    "Home": "المنزل",
    "Errands": "المشاوير",
    "Social": "اجتماعي",
    "Ideas": "أفكار",
    "Business Ideas": "أفكار مشاريع",
    "Study": "الدراسة",
    "Travel": "السفر",
    "Things to Remember": "أشياء للتذكّر",
    "Health & Fitness": "الصحة واللياقة",
    "Career": "المسار المهني",
    "Learning": "التعلّم",
    "Finance": "المال",
    "Fitness": "اللياقة",
    "Mindfulness": "الصفاء الذهني",
    "Productivity": "الإنتاجية",
    "Car": "السيارة",
    "Identity": "الهوية",
    "Vehicle": "المركبة",
    "Insurance": "التأمين",
    "Financial": "مالي",
    "Medical": "طبي",
    "Grocery": "البقالة",
    "Electronics": "الإلكترونيات",
    "Clothes": "الملابس",
    "General": "عام",
    "Cash": "نقدي",
}
_EN: dict[str, str] = {ar: en for en, ar in _AR.items()}


def default_name(english_name: str, lang: Lang) -> str:
    """Default name in `lang` for an English seed name (unknown names unchanged)."""
    return _AR.get(english_name, english_name) if lang == "ar" else english_name


def translate_default_name(current: str, lang: Lang) -> str | None:
    """The counterpart in `lang` when `current` is an untouched default name in the
    other language; otherwise None (the user renamed it, or it's their own)."""
    target = _AR.get(current) if lang == "ar" else _EN.get(current)
    return target if target and target != current else None
