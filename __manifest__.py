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
        "security/ir_rule.xml",

        # Data
        "data/fms_accounting_data.xml",
        "data/fms_accounting_company_defaults.xml",

        # Views
        "views/fms_financial_report_views.xml",
        "views/fms_sales_receipt_views.xml",
        "views/fms_vehicle_views.xml",
        "views/fms_driver_views.xml",
        "views/fms_credit_customer_views.xml",
        "views/fms_fuel_delivery_views.xml",
        "views/fms_petty_cash_views.xml",
        "views/fms_pdc_views.xml",
        "views/fms_payment_views.xml",
        "views/fms_report_debtor_views.xml",
        "views/fms_receipt_reconciliation_views.xml",
        "views/fms_accounting_menus.xml",  # must be last — references actions from all files above

        # Reports
        "reports/fms_pl_report.xml",
        "reports/fms_balance_sheet_report.xml",
        "reports/fms_trial_balance_report.xml",
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
