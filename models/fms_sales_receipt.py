"""
fms_sales_receipt.py — Per-transaction Sales Receipt extension on account.move.

Native Odoo document: account.move, move_type='out_receipt'.
FMS adds shift (auto-resolved from date) / attendant / vehicle_reg to the header.
No custom model is created (see feature.md §2 for the rationale).
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class FMSSalesReceiptMove(models.Model):
    _inherit = 'account.move'

    fms_shift_id = fields.Many2one(
        'fms.shift',
        string='Shift',
        index=True,
        ondelete='restrict',
        readonly=True,
        help="Auto-resolved from the receipt date — the open shift for that day.",
    )
    fms_attendant_id = fields.Many2one(
        'hr.employee',
        string='Attendant',
        domain=[('fms_is_attendant', '=', True)],
        help="Forecourt attendant who made the sale.",
    )
    fms_vehicle_reg = fields.Char(
        'Vehicle Reg',
        size=20,
        help="Customer vehicle registration — optional for walk-in cash.",
    )

    def _fms_find_shift(self, date, company_id):
        """Return the open/closing shift for date+company, or empty recordset."""
        return self.env['fms.shift'].search([
            ('date',       '=',  date),
            ('state',      'in', ('open', 'closing')),
            ('company_id', '=',  company_id),
        ], limit=1)

    @api.onchange('invoice_date')
    def _onchange_invoice_date_fms_shift(self):
        """Auto-populate shift whenever the user changes the receipt date."""
        for move in self:
            if move.move_type != 'out_receipt':
                continue
            date = move.invoice_date or fields.Date.today()
            move.fms_shift_id = self._fms_find_shift(date, move.company_id.id)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('move_type') == 'out_receipt' and not vals.get('fms_shift_id'):
                date = vals.get('invoice_date') or fields.Date.today()
                cid  = vals.get('company_id') or self.env.company.id
                shift = self._fms_find_shift(date, cid)
                if shift:
                    vals['fms_shift_id'] = shift.id
        return super().create(vals_list)

    @api.constrains('fms_shift_id', 'move_type', 'company_id')
    def _check_fms_receipt_shift(self):
        for move in self:
            if move.move_type != 'out_receipt' or not move.fms_shift_id:
                continue
            shift = move.fms_shift_id
            if shift.state == 'closed':
                raise ValidationError(
                    "Cannot raise a receipt against closed shift %s. "
                    "Closed shifts are locked." % shift.display_name
                )
            if shift.company_id != move.company_id:
                raise ValidationError(
                    "Shift company (%s) does not match document company (%s)."
                    % (shift.company_id.name, move.company_id.name)
                )
