"""
fms_cash_drop.py — Cash Drop model.

Data clerk posts one or more cash drops per attendant per shift (individual
drops during the shift, or a single lump sum at the end). The total drives
cash_collected on the attendant cash line automatically — no manual entry.
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class FMSCashDrop(models.Model):
    _name        = 'fms.cash.drop'
    _description = 'Cash Drop'
    _order       = 'dropped_at desc, id desc'
    _rec_name    = 'name'

    name = fields.Char('Reference', readonly=True, copy=False, default='/')

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


class FMSAttendantCashDropCompute(models.Model):
    """Make cash_collected on attendant cash line auto-computed from cash drops."""
    _inherit = 'fms.shift.attendant.cash'

    cash_collected = fields.Float(
        'Cash Dropped to Safe',
        digits=(16, 2),
        compute='_compute_cash_collected_from_drops',
        store=True,
        help="Sum of cash drops posted by data clerk for this attendant on this shift.",
    )

    @api.depends(
        'shift_id.cash_drop_ids',
        'shift_id.cash_drop_ids.amount',
        'shift_id.cash_drop_ids.attendant_id',
    )
    def _compute_cash_collected_from_drops(self):
        for rec in self:
            drops = rec.shift_id.cash_drop_ids.filtered(
                lambda d: d.attendant_id == rec.attendant_id
            )
            rec.cash_collected = sum(drops.mapped('amount'))
