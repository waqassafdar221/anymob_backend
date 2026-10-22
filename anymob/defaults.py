PERMISSIONS = [
    "manage_users",
    "moderate_catalog",
    "verify_stores",
    "manage_orders",
    "resolve_disputes",
    "process_payouts",
    "manage_finance",
    "edit_settings",
]
ROLES = {
    "super_admin": PERMISSIONS,
    "moderator": ["moderate_catalog", "verify_stores"],
    "support": ["manage_users", "manage_orders", "resolve_disputes"],
    "finance": ["process_payouts", "manage_finance"],
}
EVENTS = [
    "order_placed",
    "order_confirmed",
    "order_shipped",
    "ready_for_pickup",
    "order_delivered",
    "order_cancelled",
    "return_approved",
    "payout_sent",
    "store_verified",
    "store_rejected",
    "product_rejected",
    "password_reset",
]
DEFAULTS = {
    "general": {
        "siteName": "AnyMob",
        "tagline": "Your mobile marketplace",
        "logo": "",
        "favicon": "",
        "contactEmail": "support@anymob.pk",
        "supportPhone": "",
        "defaultCity": "Lahore",
        "currencyFormat": "symbol",
    },
    "homepage": {
        "heroTitle": "Find your next phone",
        "heroSubtitle": "Trusted local stores",
        "heroDescription": "Shop phones, tablets and accessories",
        "trustBadges": ["Verified stores", "Local pickup", "Buyer support"],
        "trustStrip": [
            {"title": "100% Genuine Phones", "description": "All phones are verified & original."},
            {"title": "Fast & Secure Delivery", "description": "Across Pakistan."},
            {"title": "24/7 Customer Support", "description": "We're here to help."},
            {"title": "Safe Transactions", "description": "Your trust, our priority."},
        ],
        "featuredProductCount": 8,
        "featuredStoreCount": 4,
        "pinnedStoreIds": [],
        "pinnedProductIds": [],
        "sections": [
            {"id": name, "enabled": True}
            for name in ["popular_brands", "featured_phones", "featured_shops", "top_deals", "trust_strip"]
        ],
    },
    "commerce": {
        "paymentMethods": ["cod", "bank_transfer"],
        "codMaximum": 200000,
        "minimumOrder": 0,
        "autoCancelHours": 48,
        "autoCompleteDays": 14,
        "returnWindowDays": 7,
        "disputeEscalationHours": 48,
        "payoutSchedule": "on_request",
        "minimumPayout": 5000,
        "holdingPeriodDays": 7,
        "couriers": [{"name": name, "enabled": True} for name in ["TCS", "Leopards", "M&P", "Trax"]],
        "maximumDeliveryFee": 2000,
    },
    "catalog": {
        "autoApproveVerifiedProducts": False,
        "autoApproveAds": False,
        "reviewUnderDays": 7,
        "adExpiryDays": 30,
        "maxProductImages": 10,
        "maxAdImages": 10,
        "bannedWords": [],
        "priceWarningPercent": 70,
    },
    "revenue": {"defaultCommission": 0.05},
    "maintenance": {"enabled": False, "message": "We will be back shortly."},
    "team": {
        "permissions": {role: {p: p in allowed for p in PERMISSIONS} for role, allowed in ROLES.items()}
    },
    "notifications": {
        "templates": {
            event: {
                "emailSubject": event.replace("_", " ").title(),
                "emailBody": "Hello {{customer_name}}, {{reason}}",
                "sms": "{{site_name}}: {{reason}}",
            }
            for event in EVENTS
        },
        "otpSms": "Your AnyMob code is {{code}}. Expires in {{expires_minutes}} minutes.",
    },
}
