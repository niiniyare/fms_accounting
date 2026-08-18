"""
fms_driver.py — Driver model for FMS fuel credit sales.

Tracks who was driving when fuel was purchased.
Can be linked to an hr.employee (internal staff) or be external.
"""

from odoo import models, fields


class FMSDriver(models.Model):
    _name = 'fms.driver'
    _description = 'Fuel Driver'
    _order = 'name'

    name = fields.Char('Driver Name', required=True, index=True)
    license_no = fields.Char('Driving Licence No.')
    partner_id = fields.Many2one(
        'res.partner', 'Linked Customer',
        help="Optional: link to a res.partner for AR purposes.",
    )
    employee_id = fields.Many2one(
        'hr.employee', 'Linked Employee',
        help="Optional: link to an hr.employee if this driver is internal staff.",
    )
    vehicle_ids = fields.Many2many(
        'fms.vehicle', 'fms_vehicle_driver_rel', 'driver_id', 'vehicle_id',
        string='Authorised Vehicles',
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', 'Company',
        default=lambda self: self.env.company,
        required=True,
    )
    notes = fields.Text('Notes')
