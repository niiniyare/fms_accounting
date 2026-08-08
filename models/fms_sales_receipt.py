"""
fms_sales_receipt.py — Per-transaction Sales Receipt extension on account.move.

Native Odoo document: account.move, move_type='out_receipt'.
FMS adds shift / attendant / vehicle_reg to the header and nozzle to each line.
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
        domain=[('state', 'in', ('open', 'closing'))],
        help="The shift during which this sale was made.",
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

    @api.onchange('fms_shift_id')
    def _onchange_fms_shift_date(self):
        if self.fms_shift_id and not self.invoice_date:
            self.invoice_date = self.fms_shift_id.date

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


class FMSSalesReceiptLine(models.Model):
    _inherit = 'account.move.line'

    fms_nozzle_id = fields.Many2one(
        'fms.pump.nozzle',
        string='Nozzle',
        ondelete='set null',
        help="Nozzle that dispensed the fuel — links the receipt line to the meter.",
    )
