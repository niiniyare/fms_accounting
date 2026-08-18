"""
fms_report_debtor.py — R12 Debtors Aging & Credit Exposure (build order 6)

_auto=False SQL view: one row per customer with outstanding receivables.
Columns:
  - Aging buckets: current / 1-30 / 31-60 / 61-90 / 90+ days past due
  - Credit limit and exposure from fms_accounting partner extension
  - Last payment date
  - Over-limit and on-hold flags for exception filtering

F6 (Aged Receivable from OCA account_financial_report) is a separate module
to install and configure — not built here.
"""

from odoo import models, fields


class FMSReportDebtorAging(models.Model):
    _name        = 'fms.report.debtor.aging'
    _description = 'Debtors Aging & Credit Exposure'
    _auto        = False
    _order       = 'over_limit desc, days_overdue desc, balance desc'

    # ── Dimensions ────────────────────────────────────────────────────────
    partner_id    = fields.Many2one('res.partner',  'Customer',  readonly=True)
    company_id    = fields.Many2one('res.company',  'Company',   readonly=True)

    # ── Balance & aging buckets ───────────────────────────────────────────
    balance       = fields.Float('Total Outstanding', readonly=True, digits=(16, 2))
    bucket_current = fields.Float('Not Yet Due',      readonly=True, digits=(16, 2))
    bucket_1_30   = fields.Float('1–30 Days',         readonly=True, digits=(16, 2))
    bucket_31_60  = fields.Float('31–60 Days',        readonly=True, digits=(16, 2))
    bucket_61_90  = fields.Float('61–90 Days',        readonly=True, digits=(16, 2))
    bucket_90_plus = fields.Float('90+ Days',         readonly=True, digits=(16, 2))
    days_overdue  = fields.Integer('Days Overdue (oldest)', readonly=True)
    last_payment_date = fields.Date('Last Payment',         readonly=True)

    # ── Credit control ────────────────────────────────────────────────────
    credit_limit  = fields.Float('Credit Limit',      readonly=True, digits=(16, 2))
    exposure_pct  = fields.Float('Exposure %',              readonly=True, digits=(16, 1))
    over_limit    = fields.Boolean('Over Limit',            readonly=True)
    on_hold       = fields.Boolean('On Hold',               readonly=True)

    # ── Invoice count (for drill-down context) ────────────────────────────
    invoice_count = fields.Integer('Open Invoices',         readonly=True)

    def init(self):
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW fms_report_debtor_aging AS (
                SELECT
                    rp.id                                               AS id,
                    rp.id                                               AS partner_id,
                    COALESCE(aml.company_id, rc.id)                     AS company_id,

                    -- Total outstanding balance (receivable, open, posted)
                    COALESCE(SUM(aml.amount_residual), 0)               AS balance,

                    -- Aging buckets based on date_maturity vs today
                    -- Not yet due: maturity in the future (or null = no term set, treat as current)
                    COALESCE(SUM(
                        CASE WHEN COALESCE(aml.date_maturity, CURRENT_DATE) >= CURRENT_DATE
                             THEN aml.amount_residual ELSE 0 END
                    ), 0)                                               AS bucket_current,

                    COALESCE(SUM(
                        CASE WHEN aml.date_maturity IS NOT NULL
                              AND CURRENT_DATE - aml.date_maturity BETWEEN 1 AND 30
                             THEN aml.amount_residual ELSE 0 END
                    ), 0)                                               AS bucket_1_30,

                    COALESCE(SUM(
                        CASE WHEN aml.date_maturity IS NOT NULL
                              AND CURRENT_DATE - aml.date_maturity BETWEEN 31 AND 60
                             THEN aml.amount_residual ELSE 0 END
                    ), 0)                                               AS bucket_31_60,

                    COALESCE(SUM(
                        CASE WHEN aml.date_maturity IS NOT NULL
                              AND CURRENT_DATE - aml.date_maturity BETWEEN 61 AND 90
                             THEN aml.amount_residual ELSE 0 END
                    ), 0)                                               AS bucket_61_90,

                    COALESCE(SUM(
                        CASE WHEN aml.date_maturity IS NOT NULL
                              AND CURRENT_DATE - aml.date_maturity > 90
                             THEN aml.amount_residual ELSE 0 END
                    ), 0)                                               AS bucket_90_plus,

                    -- Oldest overdue (days since oldest unpaid maturity)
                    GREATEST(
                        COALESCE(
                            MAX(CASE WHEN aml.date_maturity < CURRENT_DATE
                                     THEN CURRENT_DATE - aml.date_maturity END),
                            0
                        ), 0
                    )                                                   AS days_overdue,

                    -- Last payment date for this partner (posted payments)
                    (
                        SELECT MAX(am2.invoice_date)
                        FROM account_move am2
                        WHERE am2.partner_id   = rp.id
                          AND am2.move_type    = 'in_receipt'
                          AND am2.state        = 'posted'
                    )                                                   AS last_payment_date,

                    -- Credit control from FMS extension
                    COALESCE(rp.fms_credit_limit, 0)                    AS credit_limit,
                    CASE
                        WHEN COALESCE(rp.fms_credit_limit, 0) > 0
                        THEN ROUND(
                            (COALESCE(SUM(aml.amount_residual), 0)
                             / rp.fms_credit_limit * 100)::numeric, 1
                        )
                        ELSE 0
                    END                                                 AS exposure_pct,
                    CASE
                        WHEN COALESCE(rp.fms_credit_limit, 0) > 0
                         AND COALESCE(SUM(aml.amount_residual), 0) > rp.fms_credit_limit
                        THEN TRUE ELSE FALSE
                    END                                                 AS over_limit,
                    COALESCE(rp.fms_on_hold, FALSE)                     AS on_hold,

                    COUNT(DISTINCT aml.move_id)                         AS invoice_count

                FROM res_partner rp
                JOIN account_move_line aml  ON aml.partner_id = rp.id
                JOIN account_account   aa   ON aa.id = aml.account_id
                CROSS JOIN (SELECT id FROM res_company ORDER BY id LIMIT 1) rc
                WHERE aa.account_type  = 'asset_receivable'
                  AND aml.reconciled   = FALSE
                  AND aml.parent_state = 'posted'
                  AND aml.amount_residual > 0
                GROUP BY rp.id, rp.name, rp.fms_credit_limit, rp.fms_on_hold,
                         aml.company_id, rc.id
                HAVING COALESCE(SUM(aml.amount_residual), 0) > 0
            )
        """)

    # ── Smart button: open invoices for this customer ─────────────────────
    def action_open_invoices(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Open Invoices — {self.partner_id.name}',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [
                ('partner_id', '=', self.partner_id.id),
                ('move_type', '=', 'out_invoice'),
                ('payment_state', 'in', ('not_paid', 'partial')),
                ('state', '=', 'posted'),
            ],
            'context': {'default_move_type': 'out_invoice'},
        }
