"""
Seed script: Generates realistic product & service reviews with pre-computed
sentiment scores and vector embeddings.

Demonstrates: TiDB as the single store for operational data + ML features.
Run after seed_demo_data.py and seed_orders.py.
"""
import sys
import os
import random
import math
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mysql.connector
from agent_tools import DB_CONFIG, get_model
from tenancy import resolve_tenant_id

# ---------------------------------------------------------------------------
# Review templates keyed by sentiment
# ---------------------------------------------------------------------------
PRODUCT_REVIEWS = {
    "positive": [
        "Absolutely love this product. Setup took minutes and performance has been flawless.",
        "Best purchase I've made this year. Exceeds expectations in every way.",
        "Incredibly fast and reliable. My workflow has never been smoother.",
        "Outstanding build quality. Feels premium and works even better than it looks.",
        "Delivered ahead of schedule and works exactly as advertised. Five stars.",
        "Blew me away with the performance. Worth every cent.",
        "Solid product. My team has been using it daily with zero issues.",
        "Exactly what I needed. Clean design, intuitive setup, great results.",
        "Noticeably faster than the previous model. Very happy with the upgrade.",
        "Handles everything I throw at it without breaking a sweat.",
    ],
    "neutral": [
        "Does what it says on the box. Nothing exceptional but no complaints either.",
        "Decent for the price point. A few minor quirks but nothing deal-breaking.",
        "Average performance. Meets basic requirements but won't blow you away.",
        "Works fine. Setup was a bit fiddly but it functions as expected.",
        "Okay product overall. Expected slightly more polish at this price.",
        "Gets the job done. Not the best I've used but far from the worst.",
        "Middle of the road. Reliable enough for everyday tasks.",
        "It's fine. No major issues but nothing that stands out.",
    ],
    "negative": [
        "Disappointed with the build quality. Feels cheap compared to the price.",
        "Stopped working reliably after two weeks. Very frustrating experience.",
        "Not worth the price. Expected significantly more for what I paid.",
        "Arrived with a defect and the return process was a nightmare.",
        "Performance is nowhere near what was advertised. Misleading description.",
        "Had constant connectivity issues from day one. Not recommended.",
        "The product overheats under normal load. Serious design flaw.",
        "Poor documentation and zero useful support. Gave up after several hours.",
        "Broke within a month of normal use. Would not purchase again.",
        "Way below expectations. Regret this purchase.",
    ],
}

SERVICE_REVIEWS = {
    "positive": [
        "Support team resolved my issue within the hour. Exceptional service.",
        "Fast shipping and perfectly packaged. Will definitely order again.",
        "Customer service went above and beyond. Really impressed.",
        "Smooth ordering experience from start to finish. No friction at all.",
        "Problem was resolved on the first contact. That's rare and appreciated.",
    ],
    "neutral": [
        "Response was slow but the team was eventually helpful.",
        "Shipping took longer than expected but the product arrived safely.",
        "Support was adequate. Not exceptional but got the job done.",
        "Had to follow up twice but the issue was eventually resolved.",
    ],
    "negative": [
        "Waited three days for a response. Completely unacceptable for a paid plan.",
        "Support closed my ticket without resolving the issue. Very unhappy.",
        "Package arrived damaged and the refund process is taking forever.",
        "Nobody seems to know what they are doing. Escalated twice with no resolution.",
    ],
}

# ---------------------------------------------------------------------------
# Sentiment score mapping: rating → base score with slight randomness
# ---------------------------------------------------------------------------
def rating_to_sentiment(rating: int) -> tuple[float, str]:
    """Convert a 1-5 star rating to a (score, label) pair."""
    base = {1: -0.85, 2: -0.45, 3: 0.0, 4: 0.55, 5: 0.90}[rating]
    jitter = random.uniform(-0.08, 0.08)
    score = round(max(-1.0, min(1.0, base + jitter)), 3)
    if score > 0.15:
        label = "positive"
    elif score < -0.15:
        label = "negative"
    else:
        label = "neutral"
    return score, label


def weighted_sentiment() -> str:
    """Return a sentiment label with realistic distribution: 55% pos / 25% neu / 20% neg."""
    return random.choices(
        ["positive", "neutral", "negative"],
        weights=[55, 25, 20]
    )[0]


def random_past_date(days: int = 60) -> datetime:
    return datetime.now() - timedelta(days=random.randint(0, days))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def seed_reviews():
    tenant_id = resolve_tenant_id()
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # Fetch existing IDs
    cursor.execute("SELECT customer_id FROM customers WHERE tenant_id = %s", (tenant_id,))
    customer_ids = [r[0] for r in cursor.fetchall()]

    cursor.execute("SELECT product_id FROM products WHERE tenant_id = %s", (tenant_id,))
    product_ids = [r[0] for r in cursor.fetchall()]

    if not customer_ids or not product_ids:
        print("❌ No customers or products found. Run seed_demo_data.py and generate_world.py first.")
        conn.close()
        return

    model = get_model()
    inserted = 0

    print("🌱 Seeding product reviews...")
    # ~3-4 reviews per product across the catalog
    for product_id in product_ids:
        n_reviews = random.randint(3, 6)
        for _ in range(n_reviews):
            sentiment = weighted_sentiment()
            rating = random.choice(
                [4, 5] if sentiment == "positive"
                else [3] if sentiment == "neutral"
                else [1, 2]
            )
            text = random.choice(PRODUCT_REVIEWS[sentiment])
            score, label = rating_to_sentiment(rating)
            embedding = model.encode(text).tolist()
            created_at = random_past_date(60)

            cursor.execute(
                """INSERT INTO reviews
                   (customer_id, product_id, review_type, rating, review_text,
                    sentiment_score, sentiment_label, embedding, created_at, tenant_id)
                   VALUES (%s, %s, 'product', %s, %s, %s, %s, %s, %s, %s)""",
                (
                    random.choice(customer_ids),
                    product_id,
                    rating,
                    text,
                    score,
                    label,
                    str(embedding),
                    created_at,
                    tenant_id,
                )
            )
            inserted += 1

    print("🌱 Seeding service/experience reviews...")
    for _ in range(40):
        sentiment = weighted_sentiment()
        rating = random.choice(
            [4, 5] if sentiment == "positive"
            else [3] if sentiment == "neutral"
            else [1, 2]
        )
        text = random.choice(SERVICE_REVIEWS[sentiment])
        score, label = rating_to_sentiment(rating)
        embedding = model.encode(text).tolist()
        created_at = random_past_date(60)

        cursor.execute(
            """INSERT INTO reviews
               (customer_id, product_id, review_type, rating, review_text,
                sentiment_score, sentiment_label, embedding, created_at, tenant_id)
               VALUES (%s, NULL, 'service', %s, %s, %s, %s, %s, %s, %s)""",
            (
                random.choice(customer_ids),
                rating,
                text,
                score,
                label,
                str(embedding),
                created_at,
                tenant_id,
            )
        )
        inserted += 1

    conn.commit()
    conn.close()
    print(f"✅ Done — {inserted} reviews seeded.")


if __name__ == "__main__":
    seed_reviews()
