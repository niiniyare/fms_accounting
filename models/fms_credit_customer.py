"""
fms_credit_customer.py — Fleet / credit customer management

fms.credit.customer: one record per fleet account, linked to res.partner.
  - credit_limit, fleet_card_ref, pdc_allowed
  - outstanding_balance computed from posted account.move.line

account.payment _inherit: adds PDC (post-dated cheque) tracking fields.
"""

from odoo import models, fields, api


class FMSCreditCustomer(models.Model):
    _name = 'fms.credit.customer'
    _description = 'FMS Credit Customer'
    _inherit = ['mail.thread']
    _rec_name = 'partner_id'
    _order = 'partner_id'

    partner_id = fields.Many2one(
        'res.partner', 'Customer', required=True, ondelete='restrict',
        help="Odoo contact linked to this fleet/credit account.",
    )
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    fleet_card_ref = fields.Char(
        'Fleet Card / Account Code',
        help="Code used by attendants to identify this account at the pump.",
    )
    credit_limit = fields.Float(
        'Credit Limit (KES)', digits=(16, 2), default=0.0,
        help="Maximum outstanding balance allowed. 0 = no limit enforced.",
    )
    pdc_allowed = fields.Boolean(
        'PDC Allowed', default=False,
        help="Whether this customer may settle with post-dated cheques.",
    )
    active = fields.Boolean(default=True)
    notes = fields.Text('Internal Notes')

    outstanding_balance = fields.Float(
        'Outstanding Balance (KES)',
        compute='_compute_outstanding', digits=(16, 2),
        help="Sum of unpaid posted customer invoices for this partner.",
    )
    credit_available = fields.Float(
        'Credit Available (KES)',
        compute='_compute_outstanding', digits=(16, 2),
    )

    _sql_constraints = [
        ('partner_company_unique', 'UNIQUE(partner_id, company_id)',
         'A credit account already exists for this partner in this company.'),
    ]

    @api.depends('partner_id', 'credit_limit')
    def _compute_outstanding(self):
        for rec in self:
            if not rec.partner_id:
                rec.outstanding_balance = 0.0
                rec.credit_available = rec.credit_limit
                continue
            lines = self.env['account.move.line'].search([
                ('partner_id', '=', rec.partner_id.id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('parent_state', '=', 'posted'),
                ('reconciled', '=', False),
                ('company_id', '=', rec.company_id.id),
            ])
            balance = sum(lines.mapped('amount_residual'))
            rec.outstanding_balance = balance
            rec.credit_available = (rec.credit_limit - balance) if rec.credit_limit else 0.0

    def action_view_invoices(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [
                ('partner_id', '=', self.partner_id.id),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
            ],
        }


# ---------------------------------------------------------------------------
# Extend fms.shift.attendant.cash with credit customer linkage
# ---------------------------------------------------------------------------

class FMSShiftAttendantCashAccounting(models.Model):
    _inherit = 'fms.shift.attendant.cash'

    credit_customer_id = fields.Many2one(
        'fms.credit.customer', 'Credit Account',
        help="Fleet/credit account for the AR portion of this attendant's sales. "
             "Used to set partner_id on the AR GL line when the shift closes.",
    )


# ---------------------------------------------------------------------------
# Extend account.payment with PDC fields
# ---------------------------------------------------------------------------

class AccountPaymentPDC(models.Model):
    _inherit = 'account.payment'

    fms_is_pdc = fields.Boolean(
        'Post-Dated Cheque', default=False,
        help="Mark if this payment is a post-dated cheque held pending clearance.",
    )
    fms_pdc_state = fields.Selection([
        ('held',     'Held'),
        ('cleared',  'Cleared'),
        ('bounced',  'Bounced'),
    ], string='PDC Status',
       help="Tracking state for post-dated cheques. Only relevant when fms_is_pdc=True.")

    fms_cheque_number = fields.Char('Cheque Number')
    fms_cheque_date = fields.Date(
        'Cheque Date',
        help="The date printed on the cheque. If future-dated, mark as PDC.",
    )
    fms_credit_customer_id = fields.Many2one(
        'fms.credit.customer', 'Credit Account',
        help="The FMS fleet/credit account this payment settles.",
    )
