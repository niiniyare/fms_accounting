"""
fms_receipt_reconciliation.py — Receipt reconciliation report (SQL view).

Groups posted sale receipts by shift → attendant → payment mode → product.
Used at shift close to verify every transaction is accounted for.
"""

from odoo import fields, models


class FMSReceiptReconciliation(models.Model):
    _name        = 'fms.report.receipt.reconciliation'
    _description = 'Receipt Reconciliation'
    _auto        = False
    _order       = 'shift_date desc, attendant_id, payment_mode, product_name'

    shift_id      = fields.Many2one('fms.shift',       'Shift',        readonly=True)
    shift_date    = fields.Date(                        'Shift Date',   readonly=True)
    shift_label   = fields.Char(                        'Period',       readonly=True)
    company_id    = fields.Many2one('res.company',      'Company',      readonly=True)
    attendant_id  = fields.Many2one('hr.employee',      'Attendant',    readonly=True)
    journal_id    = fields.Many2one('account.journal',  'Journal',      readonly=True)
    payment_mode  = fields.Char(                        'Payment Mode', readonly=True)
    product_id    = fields.Many2one('product.product',  'Product',      readonly=True)
    product_name  = fields.Char(                        'Product Name', readonly=True)
    receipt_count = fields.Integer(                     'Receipts',     readonly=True)
    total_litres  = fields.Float(  'Litres',  digits=(16, 3),           readonly=True)
    total_amount  = fields.Float(  'Amount', digits=(16, 2),      readonly=True)
    state_draft   = fields.Integer('Draft',                             readonly=True)
    state_posted  = fields.Integer('Confirmed',                         readonly=True)

    def init(self):
        self.env.cr.execute("DROP VIEW IF EXISTS fms_report_receipt_reconciliation")
        self.env.cr.execute("""
            CREATE VIEW fms_report_receipt_reconciliation AS
            SELECT
                ROW_NUMBER() OVER ()                    AS id,
                am.fms_shift_id                         AS shift_id,
                s.date                                  AS shift_date,
                s.label                                 AS shift_label,
                COALESCE(s.company_id, am.company_id)   AS company_id,
                am.fms_attendant_id                     AS attendant_id,
                am.journal_id,
                j.name->>'en_US'                        AS payment_mode,
                aml.product_id,
                pt.name->>'en_US'                       AS product_name,
                COUNT(DISTINCT am.id)                   AS receipt_count,
                COALESCE(SUM(aml.quantity),        0)   AS total_litres,
                COALESCE(SUM(aml.price_subtotal),  0)   AS total_amount,
                COUNT(*) FILTER (WHERE am.state = 'draft')   AS state_draft,
                COUNT(*) FILTER (WHERE am.state = 'posted')  AS state_posted
            FROM account_move am
            JOIN account_move_line aml
                ON  aml.move_id      = am.id
                AND aml.display_type = 'product'
            LEFT JOIN fms_shift s         ON s.id   = am.fms_shift_id
            LEFT JOIN account_journal j   ON j.id   = am.journal_id
            LEFT JOIN product_product pp  ON pp.id  = aml.product_id
            LEFT JOIN product_template pt ON pt.id  = pp.product_tmpl_id
            WHERE am.move_type = 'out_receipt'
            GROUP BY
                am.fms_shift_id,
                s.date,
                s.label,
                s.company_id,
                am.company_id,
                am.fms_attendant_id,
                am.journal_id,
                j.name->>'en_US',
                aml.product_id,
                pt.name->>'en_US'
        """)
