"""
fms_cash_drop.py — Cash Drop and Cash Declaration models.

Workflow:
  During shift: attendant makes one or more cash drops (fms.cash.drop).
  At shift close: attendant makes a single blind declaration of total cash
  handed over (fms.cash.declaration). System generates an out_receipt for
  the declared amount and the manager resolves any variance.

Variance accounting:
  Short (declared < expected): DR Attendant Receivable / CR Cash Clearing
  Over  (declared > expected): DR Cash Clearing / CR Station Variance Account
"""

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class FMSCashDrop(models.Model):
    _name        = 'fms.cash.drop'
    _description = 'Cash Drop'
    _order       = 'dropped_at desc, id desc'
    _rec_name    = 'name'

    name = fields.Char('Reference', readonly=True, copy=False)

    shift_id = fields.Many2one(
        'fms.shift', 'Shift', required=True, ondelete='cascade', index=True,
    )
    attendant_id = fields.Many2one(
        'hr.employee', 'Attendant', required=True,
        domain=[('fms_is_attendant', '=', True)],
    )
    amount = fields.Float('Amount (KES)', required=True, digits=(16, 2))
    dropped_at = fields.Datetime(
        'Dropped At', required=True, default=fields.Datetime.now,
    )
    note = fields.Char('Note', size=120)

    journal_id = fields.Many2one(
        'account.journal', 'Cash Journal', required=True,
        domain=[('type', '=', 'cash')],
    )
    move_id = fields.Many2one(
        'account.move', 'Journal Entry', readonly=True, copy=False,
    )
    state = fields.Selection([
        ('draft',  'Draft'),
        ('posted', 'Posted'),
    ], default='draft', required=True, readonly=True)

    company_id = fields.Many2one(
        'res.company', related='shift_id.company_id', store=True, index=True,
    )

    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence']
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == '/':
                vals['name'] = seq.next_by_code('fms.cash.drop') or '/'
        return super().create(vals_list)

    @api.constrains('shift_id', 'amount')
    def _check_drop(self):
        for drop in self:
            if drop.shift_id.state == 'closed':
                raise ValidationError(
                    "Cannot add a cash drop to closed shift '%s'."
                    % drop.shift_id.display_name
                )
            if drop.amount <= 0:
                raise ValidationError("Cash drop amount must be positive.")

    def action_post(self):
        for drop in self:
            if drop.state == 'posted':
                continue
            if drop.shift_id.state == 'closed':
                raise UserError(
                    "Shift '%s' is closed — cannot post cash drop."
                    % drop.shift_id.display_name
                )
            move = self.env['account.move'].create({
                'move_type':  'entry',
                'date':       drop.dropped_at.date(),
                'journal_id': drop.journal_id.id,
                'ref':        f"Cash Drop {drop.name} — {drop.shift_id.display_name}",
                'company_id': drop.company_id.id,
                'line_ids': [
                    # DR Cash journal account (money into safe)
                    (0, 0, {
                        'account_id': drop.journal_id.default_account_id.id,
                        'name': f"Cash drop — {drop.attendant_id.name}",
                        'debit': drop.amount,
                        'credit': 0.0,
                    }),
                    # CR Attendant clearing (reduces their float)
                    (0, 0, {
                        'account_id': drop._get_attendant_clearing_account().id,
                        'name': f"Cash drop — {drop.attendant_id.name}",
                        'debit': 0.0,
                        'credit': drop.amount,
                    }),
                ],
            })
            move.action_post()
            drop.write({'move_id': move.id, 'state': 'posted'})

    def _get_attendant_clearing_account(self):
        """Return the cash clearing account from site preferences."""
        self.ensure_one()
        prefs = self.env['fms.site.preferences'].get_for_company(self.company_id)
        account = prefs and prefs.clearing_account_id
        if not account:
            raise UserError(
                "Cash Clearing Account not configured. "
                "Set it in Forecourt → Configuration → Site Preferences."
            )
        return account


class FMSCashDeclaration(models.Model):
    _name        = 'fms.cash.declaration'
    _description = 'Cash Declaration'
    _order       = 'shift_id desc, attendant_id'
    _rec_name    = 'display_name'

    shift_id = fields.Many2one(
        'fms.shift', 'Shift', required=True, ondelete='cascade', index=True,
    )
    attendant_id = fields.Many2one(
        'hr.employee', 'Attendant', required=True,
        domain=[('fms_is_attendant', '=', True)],
    )
    company_id = fields.Many2one(
        'res.company', related='shift_id.company_id', store=True, index=True,
    )

    # Blind count: attendant declares without knowing expected
    declared_amount = fields.Float(
        'Declared Cash (KES)', digits=(16, 2),
        help="Total cash the attendant says they handed over, including all drops.",
    )

    # Computed totals
    drops_total = fields.Float(
        'Drops Total (KES)', compute='_compute_drops_total', store=True,
        digits=(16, 2),
        help="Sum of posted cash drops for this attendant on this shift.",
    )
    expected_cash = fields.Float(
        'Expected Cash (KES)', compute='_compute_expected_cash', store=True,
        digits=(16, 2),
        help="Cash expected from meter sales minus digital/credit collections.",
    )
    variance = fields.Float(
        'Variance (KES)', compute='_compute_variance', store=True,
        digits=(16, 2),
        help="Declared − Expected. Negative = short. Positive = over.",
    )
    variance_type = fields.Selection([
        ('balanced', 'Balanced'),
        ('short',    'Short'),
        ('over',     'Over'),
    ], compute='_compute_variance', store=True)

    # Documents
    receipt_move_id = fields.Many2one(
        'account.move', 'Cash Receipt', readonly=True, copy=False,
        help="out_receipt generated for the declared cash amount.",
    )
    resolution_move_id = fields.Many2one(
        'account.move', 'Variance Entry', readonly=True, copy=False,
        help="Journal entry posting the variance to attendant or station variance account.",
    )

    state = fields.Selection([
        ('draft',    'Draft'),
        ('declared', 'Declared'),
        ('resolved', 'Resolved'),
    ], default='draft', required=True, readonly=True)

    display_name = fields.Char(compute='_compute_display_name', store=True)

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    @api.depends('attendant_id', 'shift_id')
    def _compute_display_name(self):
        for rec in self:
            shift  = rec.shift_id.display_name   if rec.shift_id   else '?'
            att    = rec.attendant_id.name        if rec.attendant_id else '?'
            rec.display_name = f"{att} — {shift}"

    @api.depends('shift_id', 'attendant_id', 'shift_id.cash_drop_ids.amount',
                 'shift_id.cash_drop_ids.state')
    def _compute_drops_total(self):
        for rec in self:
            drops = rec.shift_id.cash_drop_ids.filtered(
                lambda d: d.attendant_id == rec.attendant_id and d.state == 'posted'
            )
            rec.drops_total = sum(drops.mapped('amount'))

    @api.depends('shift_id', 'attendant_id')
    def _compute_expected_cash(self):
        """
        Expected cash = attendant's cash sales from meter − digital receipts − AR.
        Reads from the attendant cash line on the shift (fms.shift.attendant.cash).
        """
        for rec in self:
            cash_line = rec.shift_id.attendant_cash_ids.filtered(
                lambda c: c.attendant_id == rec.attendant_id
            )
            if cash_line:
                rec.expected_cash = cash_line[0].cash_amount
            else:
                rec.expected_cash = 0.0

    @api.depends('declared_amount', 'expected_cash')
    def _compute_variance(self):
        for rec in self:
            var = rec.declared_amount - rec.expected_cash
            rec.variance = var
            if abs(var) < 0.01:
                rec.variance_type = 'balanced'
            elif var < 0:
                rec.variance_type = 'short'
            else:
                rec.variance_type = 'over'

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_declare(self):
        """Attendant submits blind declaration. Locks declared_amount."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError("Declaration already submitted.")
            if rec.shift_id.state == 'closed':
                raise UserError("Shift is closed.")
            if rec.declared_amount < 0:
                raise ValidationError("Declared amount cannot be negative.")
            rec.state = 'declared'

    def action_generate_receipt(self):
        """Generate out_receipt for declared cash amount. Called by shift close."""
        for rec in self:
            if rec.receipt_move_id:
                continue
            if not rec.declared_amount:
                continue
            journal = self._get_cash_journal()
            shift   = rec.shift_id
            move = self.env['account.move'].with_context(
                default_move_type='out_receipt',
            ).create({
                'move_type':       'out_receipt',
                'invoice_date':    shift.end_time.date() if shift.end_time else fields.Date.today(),
                'journal_id':      journal.id,
                'company_id':      rec.company_id.id,
                'fms_shift_id':    shift.id,
                'fms_attendant_id': rec.attendant_id.id,
                'ref':             f"Cash Declaration — {rec.display_name}",
                'invoice_line_ids': [(0, 0, {
                    'name':      f"Shift cash sales — {rec.attendant_id.name}",
                    'quantity':  1.0,
                    'price_unit': rec.declared_amount,
                    'account_id': self._get_cash_revenue_account().id,
                })],
            })
            move.action_post()
            rec.receipt_move_id = move

    def action_post_resolution(self):
        """
        Manager posts the variance journal entry.
        Short: DR Attendant Receivable / CR Cash Clearing
        Over:  DR Cash Clearing / CR Station Variance Account
        """
        for rec in self:
            if rec.state == 'resolved':
                continue
            if abs(rec.variance) < 0.01:
                rec.state = 'resolved'
                continue
            if rec.state != 'declared':
                raise UserError("Declare cash first before resolving variance.")

            debit_account, credit_account = self._get_variance_accounts(rec.variance_type)
            journal = self._get_cash_journal()
            move = self.env['account.move'].create({
                'move_type':  'entry',
                'date':       fields.Date.today(),
                'journal_id': journal.id,
                'company_id': rec.company_id.id,
                'ref':        f"Cash variance — {rec.display_name}",
                'line_ids': [
                    (0, 0, {
                        'account_id': debit_account.id,
                        'name':       f"Cash variance ({rec.variance_type}) — {rec.attendant_id.name}",
                        'debit':      abs(rec.variance),
                        'credit':     0.0,
                    }),
                    (0, 0, {
                        'account_id': credit_account.id,
                        'name':       f"Cash variance ({rec.variance_type}) — {rec.attendant_id.name}",
                        'debit':      0.0,
                        'credit':     abs(rec.variance),
                    }),
                ],
            })
            move.action_post()
            rec.write({'resolution_move_id': move.id, 'state': 'resolved'})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_cash_journal(self):
        self.ensure_one()
        prefs = self.env['fms.site.preferences'].get_for_company(self.company_id)
        journal = prefs and prefs.sales_journal_id
        if not journal:
            raise UserError(
                "Forecourt Sales Journal not configured. "
                "Set it in Forecourt → Configuration → Site Preferences."
            )
        return journal

    def _get_cash_revenue_account(self):
        self.ensure_one()
        prefs = self.env['fms.site.preferences'].get_for_company(self.company_id)
        account = prefs and prefs.clearing_account_id
        if not account:
            raise UserError(
                "Cash Clearing Account not configured. "
                "Set it in Forecourt → Configuration → Site Preferences."
            )
        return account

    def _get_variance_accounts(self, variance_type):
        """Return (debit_account, credit_account) for the variance entry."""
        self.ensure_one()
        prefs = self.env['fms.site.preferences'].get_for_company(self.company_id)
        clearing = prefs and prefs.clearing_account_id
        if not clearing:
            raise UserError("Cash Clearing Account not configured in Site Preferences.")

        if variance_type == 'short':
            # Short: attendant owes station
            # DR Attendant Receivable / CR Cash Clearing
            att_account = (
                self.attendant_id.address_home_id.property_account_receivable_id
                if self.attendant_id.address_home_id else None
            )
            if not att_account:
                att_account = clearing  # fallback: charge to clearing
            return att_account, clearing
        else:
            # Over: station received more than expected
            # DR Cash Clearing / CR Station Variance Account
            var_account = getattr(prefs, 'shift_variance_account_id', None) or None
            if not var_account:
                var_account = clearing  # fallback
            return clearing, var_account

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------

    _sql_constraints = [
        (
            'unique_attendant_shift',
            'UNIQUE(shift_id, attendant_id)',
            'Only one cash declaration per attendant per shift.',
        ),
    ]
