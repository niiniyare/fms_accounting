{
    "name": "FMS Accounting",
    "version": "18.0.1.0.0",
    "category": "Accounting",
    "summary": "Forecourt accounting: credit customers, fuel deliveries, petty cash, VAT",
    "author": "Anika Global Limited",
    "depends": ["fms", "account", "stock", "purchase", "mail"],
    "data": [
        # Security
        "security/ir_model_access.xml",

        # Data
        "data/fms_accounting_data.xml",

        # Views
        "views/fms_credit_customer_views.xml",
        "views/fms_fuel_delivery_views.xml",
        "views/fms_petty_cash_views.xml",
        "views/fms_pdc_views.xml",

        # Reports
        "reports/fms_ar_statement_report.xml",
        "reports/fms_delivery_register_report.xml",
        "reports/fms_vat_summary_report.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "test": [
        "tests/test_fms_accounting.py",
    ],
}
