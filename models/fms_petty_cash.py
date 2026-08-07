"""
fms_petty_cash.py — Station petty cash float management

One permanent float per company. Disbursements reduce the float and post
a GL entry (DR Expense | CR Cash journal account). Top-ups replenish it.

Integration: fms.shift.attendant.cash.expense_amount is extended (in
fms_shift_accounting.py) to also sum petty cash disbursements for the
shift/attendant combination, so they appear in the shift reconciliation.
"""

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class FMSPettyCashFloat(models.Model):
    _name = 'fms.petty.cash.float'
    _description = 'Petty Cash Float'
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', 'Company', required=True, ondelete='cascade',
        default=lambda self: self.env.company,
    )
    journal_id = fields.Many2one(
        'account.journal', 'Cash Journal', required=True,
        domain=[('type', '=', 'cash')],
        help="The cash account this float is held in.",
    )
    low_balance_alert = fields.Float(
        'Low Balance Alert (KES)', digits=(16, 2), default=5000.0,
        help="Show a warning when current balance falls below this threshold.",
    )
    disbursement_ids = fields.One2many(
        'fms.petty.cash.disbursement', 'float_id', 'Disbursements',
    )
    current_balance = fields.Float(
        'Current Balance (KES)', compute='_compute_balance',
        digits=(16, 2), store=True,
    )
    is_low = fields.Boolean(compute='_compute_balance', store=True)

    _sql_constraints = [
        ('company_unique', 'UNIQUE(company_id)',
         'Only one petty cash float per company.'),
    ]

    @api.depends('disbursement_ids.amount', 'disbursement_ids.state',
                 'disbursement_ids.is_topup')
    def _compute_balance(self):
        for rec in self:
            posted = rec.disbursement_ids.filtered(lambda d: d.state == 'posted')
            topups = sum(d.amount for d in posted if d.is_topup)
            spend  = sum(d.amount for d in posted if not d.is_topup)
            rec.current_balance = topups - spend
            rec.is_low = (rec.current_balance < rec.low_balance_alert)

    @api.model
    def get_for_company(self, company=None):
        company = company or self.env.company
        return self.search([('company_id', '=', company.id)], limit=1)


class FMSPettyCashDisbursement(models.Model):
    _name = 'fms.petty.cash.disbursement'
    _description = 'Petty Cash Disbursement'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    float_id = fields.Many2one(
        'fms.petty.cash.float', 'Float', required=True, ondelete='restrict',
    )
    date = fields.Date('Date', required=True, default=fields.Date.today)
    is_topup = fields.Boolean(
        'Top-up', default=False,
        help="Tick for replenishments (CR Bank | DR Cash). "
             "Untick for disbursements (DR Expense | CR Cash).",
    )
    shift_id = fields.Many2one('fms.shift', 'Shift', ondelete='set null')
    attendant_id = fields.Many2one('hr.employee', 'Attendant / Payee')
    amount = fields.Float('Amount (KES)', digits=(16, 2), required=True)
    description = fields.Char('Description', required=True)
    account_id = fields.Many2one(
        'account.account', 'Expense Account',
        domain=[('account_type', 'in', ('expense', 'expense_direct_cost'))],
        help="Required for disbursements. Leave blank for top-ups.",
    )
    move_id = fields.Many2one('account.move', 'Journal Entry', readonly=True, copy=False)
    state = fields.Selection([
        ('draft',  'Draft'),
        ('posted', 'Posted'),
    ], default='draft', readonly=True, copy=False)

    # ------------------------------------------------------------------

    def action_post(self):
        for rec in self:
            if rec.state != 'draft':
                continue
            if rec.amount <= 0:
                raise ValidationError("Amount must be positive.")
            if not rec.is_topup and not rec.account_id:
                raise ValidationError("Expense Account is required for disbursements.")
            move = rec._create_move()
            rec.write({'state': 'posted', 'move_id': move.id})

    def action_reset_draft(self):
        for rec in self:
            if rec.move_id and rec.move_id.state == 'posted':
                raise ValidationError(
                    "Cannot reset a disbursement whose journal entry is already posted. "
                    "Reverse the journal entry in Accounting first."
                )
            rec.write({'state': 'draft', 'move_id': False})

    def _create_move(self):
        self.ensure_one()
        journal = self.float_id.journal_id
        cash_account = journal.default_account_id
        if not cash_account:
            raise ValidationError(
                f"Journal '{journal.name}' has no default account configured."
            )

        if self.is_topup:
            # DR Cash | CR Bank (receivable from management)
            bank_account = self.env['account.account'].search([
                ('account_type', '=', 'asset_receivable'),
                ('company_ids', 'in', self.float_id.company_id.id),
            ], limit=1)
            if not bank_account:
                raise ValidationError("No receivable account found for top-up entry.")
            lines = [
                (0, 0, {'account_id': cash_account.id, 'debit': self.amount, 'credit': 0.0,
                         'name': f'Petty cash top-up — {self.description}'}),
                (0, 0, {'account_id': bank_account.id, 'debit': 0.0, 'credit': self.amount,
                         'name': f'Petty cash top-up — {self.description}'}),
            ]
        else:
            # DR Expense | CR Cash
            lines = [
                (0, 0, {'account_id': self.account_id.id, 'debit': self.amount, 'credit': 0.0,
                         'name': self.description}),
                (0, 0, {'account_id': cash_account.id, 'debit': 0.0, 'credit': self.amount,
                         'name': self.description}),
            ]

        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': self.date,
            'ref': f'Petty Cash — {self.description}',
            'line_ids': lines,
        })
        move.action_post()
        return move
