"""Default household-expense taxonomy + seeding helpers.

The taxonomy is system-default (user_id=NULL) on first run; per-user copies
are made lazily by ``seed_user_categories`` on first ingest. Per-user rows
let the user customize labels without touching defaults shared by other tenants.

Aggregation rules (canonical, used by /api/expenses/monthly-summary):
    real_spending(month) = SUM(amount_nis) WHERE direction='debit'
                           AND is_excluded_from_spend = FALSE
                           AND is_inflow = FALSE
                           AND is_card_payment = FALSE
    real_income(month)   = SUM(amount_nis) WHERE direction='credit'
                           AND is_inflow = TRUE
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from argosy.state.models import ExpenseCategory


@dataclass(frozen=True)
class TaxonomyEntry:
    slug: str
    label_en: str
    label_he: str
    parent_slug: str | None = None
    is_excluded_from_spend: bool = False
    is_inflow: bool = False
    display_order: int = 0


DEFAULT_TAXONOMY: list[TaxonomyEntry] = [
    # === INFLOWS ===
    TaxonomyEntry("income", "Income", "הכנסות",
                  is_inflow=True, display_order=0),
    TaxonomyEntry("income.salary", "Salary", "משכורת", "income",
                  is_inflow=True, display_order=1),
    TaxonomyEntry("income.rsu_vest_proceeds", "RSU vest proceeds",
                  "תמורת מימוש RSU", "income", is_inflow=True, display_order=2),
    TaxonomyEntry("income.bonus", "Bonus", "בונוס", "income",
                  is_inflow=True, display_order=3),
    TaxonomyEntry("income.child_benefit", "Child benefit",
                  "קצבת ילדים", "income", is_inflow=True, display_order=4),
    TaxonomyEntry("income.interest_credit", "Interest credited",
                  "ריבית זכות", "income", is_inflow=True, display_order=5),
    TaxonomyEntry("income.other_recurring_income", "Other recurring",
                  "הכנסה שוטפת אחרת", "income", is_inflow=True, display_order=6),

    # === HOUSING ===
    TaxonomyEntry("housing", "Housing", "דיור", display_order=10),
    TaxonomyEntry("housing.mortgage", "Mortgage", "משכנתא", "housing",
                  display_order=11),
    TaxonomyEntry("housing.property_tax", "Property tax (arnona)",
                  "ארנונה", "housing", display_order=12),
    TaxonomyEntry("housing.utilities_electric", "Electricity", "חשמל",
                  "housing", display_order=13),
    TaxonomyEntry("housing.utilities_water_gas", "Water & gas",
                  "מים וגז", "housing", display_order=14),
    TaxonomyEntry("housing.internet_phone", "Internet & phone",
                  "אינטרנט וטלפון", "housing", display_order=15),
    TaxonomyEntry("housing.home_maintenance", "Home maintenance",
                  "תחזוקת בית", "housing", display_order=16),
    TaxonomyEntry("housing.furniture", "Furniture", "ריהוט", "housing",
                  display_order=17),

    # === FOOD (groceries only — restaurants live under dining_out) ===
    TaxonomyEntry("food", "Food (groceries)", "מזון (מצרכים)",
                  display_order=20),
    TaxonomyEntry("food.groceries", "Groceries", "מצרכי מזון", "food",
                  display_order=21),

    # === DINING OUT (top-level, NOT under food) ===
    TaxonomyEntry("dining_out", "Dining out", "אכילה בחוץ", display_order=22),
    TaxonomyEntry("dining_out.restaurants", "Restaurants",
                  "מסעדות", "dining_out", display_order=23),
    TaxonomyEntry("dining_out.takeout", "Takeout", "טייק אווי",
                  "dining_out", display_order=24),
    TaxonomyEntry("dining_out.coffee_bars", "Coffee/bars",
                  "בתי קפה ובארים", "dining_out", display_order=25),

    # === TRANSPORTATION ===
    TaxonomyEntry("transportation", "Transportation", "תחבורה",
                  display_order=30),
    TaxonomyEntry("transportation.fuel", "Fuel", "דלק", "transportation",
                  display_order=31),
    TaxonomyEntry("transportation.public_transit", "Public transit",
                  "תחבורה ציבורית", "transportation", display_order=32),
    TaxonomyEntry("transportation.parking", "Parking", "חניה",
                  "transportation", display_order=33),
    TaxonomyEntry("transportation.car_insurance", "Car insurance",
                  "ביטוח רכב", "transportation", display_order=34),
    TaxonomyEntry("transportation.car_maintenance", "Car maintenance",
                  "תחזוקת רכב", "transportation", display_order=35),
    TaxonomyEntry("transportation.taxi_rideshare", "Taxi / rideshare",
                  "מונית/שיתופי-נסיעה", "transportation", display_order=36),

    # === HEALTHCARE ===
    TaxonomyEntry("healthcare", "Healthcare", "בריאות", display_order=40),
    TaxonomyEntry("healthcare.health_insurance", "Health insurance",
                  "ביטוח בריאות", "healthcare", display_order=41),
    TaxonomyEntry("healthcare.pharmacy", "Pharmacy", "בית מרקחת",
                  "healthcare", display_order=42),
    TaxonomyEntry("healthcare.dental", "Dental", "טיפולי שיניים",
                  "healthcare", display_order=43),
    TaxonomyEntry("healthcare.doctors", "Doctors", "רופאים", "healthcare",
                  display_order=44),
    TaxonomyEntry("healthcare.medical_other", "Medical other",
                  "רפואה אחר", "healthcare", display_order=45),

    # === INSURANCE OTHER ===
    TaxonomyEntry("insurance_other", "Other insurance", "ביטוחים אחרים",
                  display_order=50),
    TaxonomyEntry("insurance_other.life", "Life", "ביטוח חיים",
                  "insurance_other", display_order=51),
    TaxonomyEntry("insurance_other.home", "Home", "ביטוח דירה",
                  "insurance_other", display_order=52),
    TaxonomyEntry("insurance_other.umbrella", "Umbrella", "ביטוח-על",
                  "insurance_other", display_order=53),
    TaxonomyEntry("insurance_other.other", "Other", "אחר",
                  "insurance_other", display_order=54),

    # === CHILDCARE / EDUCATION ===
    TaxonomyEntry("childcare_education", "Childcare & education",
                  "טיפול בילדים וחינוך", display_order=60),
    TaxonomyEntry("childcare_education.daycare", "Daycare", "מעון/גן",
                  "childcare_education", display_order=61),
    TaxonomyEntry("childcare_education.tuition", "Tuition", "שכר לימוד",
                  "childcare_education", display_order=62),
    TaxonomyEntry("childcare_education.after_school", "After school",
                  "צהרון/חוגים", "childcare_education", display_order=63),
    TaxonomyEntry("childcare_education.education_materials",
                  "Education materials", "ציוד לימודי",
                  "childcare_education", display_order=64),
    TaxonomyEntry("childcare_education.kids_activities",
                  "Kids activities", "פעילויות ילדים",
                  "childcare_education", display_order=65),

    # === SUBSCRIPTIONS ===
    TaxonomyEntry("subscriptions", "Subscriptions", "מנויים",
                  display_order=70),
    TaxonomyEntry("subscriptions.streaming", "Streaming", "סטרימינג",
                  "subscriptions", display_order=71),
    TaxonomyEntry("subscriptions.software", "Software", "תוכנה",
                  "subscriptions", display_order=72),
    TaxonomyEntry("subscriptions.gym", "Gym", "חדר כושר",
                  "subscriptions", display_order=73),
    TaxonomyEntry("subscriptions.news", "News", "חדשות",
                  "subscriptions", display_order=74),
    TaxonomyEntry("subscriptions.other_subscription", "Other subscription",
                  "מנוי אחר", "subscriptions", display_order=75),

    # === DISCRETIONARY ===
    TaxonomyEntry("discretionary", "Discretionary", "הוצאות בחירה",
                  display_order=80),
    TaxonomyEntry("discretionary.shopping_clothing", "Clothing",
                  "לבוש והנעלה", "discretionary", display_order=81),
    TaxonomyEntry("discretionary.shopping_other", "Shopping (other)",
                  "קניות אחרות", "discretionary", display_order=82),
    TaxonomyEntry("discretionary.entertainment", "Entertainment",
                  "בידור", "discretionary", display_order=83),
    TaxonomyEntry("discretionary.hobbies", "Hobbies", "תחביבים",
                  "discretionary", display_order=84),
    TaxonomyEntry("discretionary.gifts_to_others", "Gifts",
                  "מתנות לאחרים", "discretionary", display_order=85),
    TaxonomyEntry("discretionary.charity", "Charity", "צדקה",
                  "discretionary", display_order=86),

    # === TRAVEL ===
    TaxonomyEntry("travel", "Travel", "נסיעות", display_order=90),
    TaxonomyEntry("travel.flights", "Flights", "טיסות", "travel",
                  display_order=91),
    TaxonomyEntry("travel.hotels", "Hotels", "מלונות", "travel",
                  display_order=92),
    TaxonomyEntry("travel.vacation_other", "Vacation (other)",
                  "חופשה (אחר)", "travel", display_order=93),

    # === PERSONAL ===
    TaxonomyEntry("personal", "Personal", "אישי", display_order=100),
    TaxonomyEntry("personal.personal_care", "Personal care",
                  "טיפוח אישי", "personal", display_order=101),

    # === FINANCIAL (fees only — interest income is in income.*) ===
    TaxonomyEntry("financial", "Financial fees", "עמלות פיננסיות",
                  display_order=110),
    TaxonomyEntry("financial.bank_fees", "Bank fees", "עמלות בנק",
                  "financial", display_order=111),
    TaxonomyEntry("financial.fx_fees", "FX fees", "עמלות מט\"ח",
                  "financial", display_order=112),
    TaxonomyEntry("financial.interest_paid_other", "Interest paid",
                  "ריבית חובה", "financial", display_order=113),

    # === EXCLUDED FROM SPEND ===
    TaxonomyEntry("transfers", "Transfers", "העברות",
                  is_excluded_from_spend=True, display_order=200),
    TaxonomyEntry("transfers.internal_transfer", "Internal transfer",
                  "העברה פנימית", "transfers",
                  is_excluded_from_spend=True, display_order=201),
    TaxonomyEntry("transfers.paybox_to_household", "PayBox to household",
                  "פייבוקס למשק בית", "transfers",
                  is_excluded_from_spend=True, display_order=202),
    TaxonomyEntry("transfers.atm_cash_withdrawal", "ATM cash withdrawal",
                  "משיכת מזומן", "transfers",
                  is_excluded_from_spend=True, display_order=203),

    TaxonomyEntry("investments", "Investments", "השקעות",
                  is_excluded_from_spend=True, display_order=210),
    TaxonomyEntry("investments.broker_buy_us", "Broker buy (US)",
                  "קנייה ברוקר חו\"ל", "investments",
                  is_excluded_from_spend=True, display_order=211),
    TaxonomyEntry("investments.broker_buy_il", "Broker buy (IL)",
                  "קנייה ברוקר ישראלי", "investments",
                  is_excluded_from_spend=True, display_order=212),
    TaxonomyEntry("investments.retirement_contrib", "Retirement contribution",
                  "הפקדה לפנסיה", "investments",
                  is_excluded_from_spend=True, display_order=213),
    TaxonomyEntry("investments.keren_hishtalmut_contrib",
                  "Keren hishtalmut contribution",
                  "הפקדה לקרן השתלמות", "investments",
                  is_excluded_from_spend=True, display_order=214),
    TaxonomyEntry("investments.savings_deposit", "Savings deposit",
                  "פקדון/חיסכון", "investments",
                  is_excluded_from_spend=True, display_order=215),

    TaxonomyEntry("taxes", "Taxes", "מסים",
                  is_excluded_from_spend=True, display_order=220),
    TaxonomyEntry("taxes.income_tax_paid", "Income tax paid",
                  "תשלום מס הכנסה", "taxes",
                  is_excluded_from_spend=True, display_order=221),
    TaxonomyEntry("taxes.social_security_paid", "Social security paid",
                  "תשלום ביטוח לאומי", "taxes",
                  is_excluded_from_spend=True, display_order=222),

    # === SPECIAL ===
    TaxonomyEntry("uncategorized", "Uncategorized", "לא מסווג",
                  display_order=900),
]


def seed_system_defaults(session: Session) -> None:
    """Idempotent: insert one row per TaxonomyEntry as user_id=NULL."""
    existing = {
        c.slug for c in session.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)
        ).all()
    }
    by_slug: dict[str, ExpenseCategory] = {}
    # First pass — top-level rows; SQLAlchemy needs IDs flushed before children
    for entry in DEFAULT_TAXONOMY:
        if entry.parent_slug is None and entry.slug not in existing:
            cat = ExpenseCategory(
                user_id=None, slug=entry.slug,
                label_en=entry.label_en, label_he=entry.label_he,
                is_excluded_from_spend=entry.is_excluded_from_spend,
                is_inflow=entry.is_inflow,
                display_order=entry.display_order,
            )
            session.add(cat)
            by_slug[entry.slug] = cat
    session.flush()
    for c in session.query(ExpenseCategory).filter(
        ExpenseCategory.user_id.is_(None)
    ).all():
        by_slug[c.slug] = c
    # Second pass — children
    for entry in DEFAULT_TAXONOMY:
        if entry.parent_slug is not None and entry.slug not in existing:
            parent = by_slug[entry.parent_slug]
            cat = ExpenseCategory(
                user_id=None, slug=entry.slug,
                label_en=entry.label_en, label_he=entry.label_he,
                parent_id=parent.id,
                is_excluded_from_spend=entry.is_excluded_from_spend,
                is_inflow=entry.is_inflow,
                display_order=entry.display_order,
            )
            session.add(cat)


def seed_user_categories(session: Session, user_id: str) -> None:
    """Copy system-default categories into user-scoped rows. Idempotent."""
    existing = {
        c.slug for c in session.query(ExpenseCategory).filter_by(
            user_id=user_id
        ).all()
    }
    if existing:
        # If the user already has any rows, assume seeded; the cache test
        # (n_user == n_sys after re-run) will catch a mid-stream gap.
        return
    sys_rows = session.query(ExpenseCategory).filter(
        ExpenseCategory.user_id.is_(None)
    ).order_by(ExpenseCategory.display_order).all()
    sys_by_id: dict[int, ExpenseCategory] = {c.id: c for c in sys_rows}
    new_by_slug: dict[str, ExpenseCategory] = {}
    # Top-level
    for c in sys_rows:
        if c.parent_id is None:
            user_c = ExpenseCategory(
                user_id=user_id, slug=c.slug,
                label_en=c.label_en, label_he=c.label_he,
                is_excluded_from_spend=c.is_excluded_from_spend,
                is_inflow=c.is_inflow, display_order=c.display_order,
            )
            session.add(user_c)
            new_by_slug[c.slug] = user_c
    session.flush()
    # Children
    for c in sys_rows:
        if c.parent_id is not None:
            parent_slug = sys_by_id[c.parent_id].slug
            user_c = ExpenseCategory(
                user_id=user_id, slug=c.slug,
                label_en=c.label_en, label_he=c.label_he,
                parent_id=new_by_slug[parent_slug].id,
                is_excluded_from_spend=c.is_excluded_from_spend,
                is_inflow=c.is_inflow, display_order=c.display_order,
            )
            session.add(user_c)
            new_by_slug[c.slug] = user_c
