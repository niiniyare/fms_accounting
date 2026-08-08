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
    fms_receipt_no = fields.Char(
        'Physical Receipt No',
        size=30,
        index=True,
        help="Printed slip / pump receipt number handed to the customer.",
    )
    fms_card_number = fields.Char(
        'Card / Cheque Ref',
        size=30,
        help="Last 4 digits of card, cheque number, or MPesa confirmation code.",
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


class FMSSalesReceiptLine(models.Model):
    _inherit = 'account.move.line'

    fms_line_amount = fields.Float(
        'Amount (KES)',
        digits=(16, 2),
        help="Enter the sale amount; litres are back-calculated from the unit price.",
    )

    @api.onchange('fms_line_amount', 'price_unit')
    def _onchange_fms_line_amount(self):
        """Back-calculate quantity from amount ÷ unit price."""
        for line in self:
            if line.fms_line_amount and line.price_unit:
                line.quantity = line.fms_line_amount / line.price_unit
            elif line.fms_line_amount and not line.price_unit:
                # No price yet — store amount as price_unit with qty=1
                line.price_unit = line.fms_line_amount
                line.quantity   = 1.0

    @api.onchange('quantity', 'price_unit')
    def _onchange_qty_price_sync_amount(self):
        """Keep fms_line_amount in sync when qty or price is edited directly."""
        for line in self:
            if line.quantity and line.price_unit:
                line.fms_line_amount = line.quantity * line.price_unit
