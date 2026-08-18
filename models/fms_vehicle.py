"""
fms_vehicle.py — Fleet vehicle model for FMS fuel credit sales.

Tracks vehicles that purchase fuel on company accounts.
Linked to a fleet customer (res.partner) and optionally to drivers.
"""

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class FMSVehicle(models.Model):
    _name = 'fms.vehicle'
    _description = 'Fleet Vehicle'
    _order = 'license_plate'

    license_plate = fields.Char('License Plate', required=True, index=True)
    make = fields.Char('Make')
    model = fields.Char('Model')
    year = fields.Integer('Year')
    fuel_type = fields.Selection([
        ('dx', 'Diesel (DX)'),
        ('ux', 'Unleaded (UX)'),
        ('vp', 'V-Power (VP)'),
    ], string='Fuel Type')
    partner_id = fields.Many2one(
        'res.partner', 'Fleet Account Holder',
        domain=[('fms_is_fleet_customer', '=', True)],
        help="The credit customer who owns/operates this vehicle.",
    )
    driver_ids = fields.Many2many(
        'fms.driver', 'fms_vehicle_driver_rel', 'vehicle_id', 'driver_id',
        string='Authorised Drivers',
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', 'Company',
        default=lambda self: self.env.company,
        required=True,
    )
    notes = fields.Text('Notes')

    _sql_constraints = [
        ('license_plate_company_uniq', 'unique(license_plate, company_id)',
         'License plate must be unique per company.'),
    ]
