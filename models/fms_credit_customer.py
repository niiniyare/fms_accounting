"""
fms_credit_customer.py — Credit invoice extensions

Adds vehicle and driver fields to standard Odoo customer invoices.
PDC tracking fields remain on account.payment.
"""

from odoo import models, fields


class AccountMoveVehicle(models.Model):
    _inherit = 'account.move'

    fms_vehicle = fields.Char(
        'Vehicle / Plate',
        help="Vehicle registration or plate number for fleet credit invoices.",
    )
    fms_driver = fields.Many2one(
        'hr.employee', 'Driver',
        help="Driver or authorised person for this credit transaction.",
    )


class AccountPaymentPDC(models.Model):
    _inherit = 'account.payment'

    fms_is_pdc = fields.Boolean(
        'Post-Dated Cheque', default=False,
    )
    fms_pdc_state = fields.Selection([
        ('held',    'Held'),
        ('cleared', 'Cleared'),
        ('bounced', 'Bounced'),
    ], string='PDC Status')
    fms_cheque_number = fields.Char('Cheque Number')
    fms_cheque_date = fields.Date('Cheque Date')
