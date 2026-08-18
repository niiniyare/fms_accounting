"""
fms_shift_accounting.py — Extends fms.shift for accounting features.

1. fms.shift gets delivery_ids One2many field.
2. _post_sales_journal is extended to split revenue lines into net + tax
   using Odoo's native account.tax.compute_all().
"""

from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class FMSShiftAccountingExt(models.Model):
    _inherit = 'fms.shift'

    delivery_ids = fields.One2many(
        'fms.fuel.delivery', 'shift_id', 'Fuel Deliveries',
        help="Tanker deliveries that arrived during this shift.",
    )

    def _post_sales_journal(self):
        """
        Override: split revenue GL lines into net + tax per product when
        taxes_id is configured. Falls back to base (no tax) otherwise.
        """
        self.ensure_one()

        self.env['product.product'].flush_model(['taxes_id'])
        has_taxes = any(
            e.product_id.taxes_id for e in self.meter_entry_ids if e.product_id
        )
        if not has_taxes:
            return super()._post_sales_journal()

        total_cash = sum(self.meter_entry_ids.mapped('elec_cash_sold'))
        if abs(total_cash) < 0.01:
            return False

        cash_by_product = {}
        for entry in self.meter_entry_ids:
            if abs(entry.elec_cash_sold) < 0.01:
                continue
            pid = entry.product_id.id
            cash_by_product[pid] = cash_by_product.get(pid, 0.0) + entry.elec_cash_sold

        if not cash_by_product:
            return False

        products = self.env['product.product'].browse(list(cash_by_product))
        if not any(p.fms_revenue_account_id for p in products):
            _logger.warning("FMS: Shift %s has no revenue accounts — GL skipped.", self.display_name)
            return False

        journal = self._get_fms_journal()
        clearing_account = self._get_clearing_account()
        company = self.company_id
        move_lines = []
        cr_total = 0.0

        for product in products:
            gross = cash_by_product.get(product.id, 0.0)
            if abs(gross) < 0.01 or not product.fms_revenue_account_id:
                continue

            taxes = product.taxes_id.filtered(lambda t: t.company_id == company)
            if taxes:
                res = taxes.compute_all(gross, currency=company.currency_id, quantity=1.0, product=product)
                net = res['total_excluded']
                move_lines.append((0, 0, {
                    'account_id': product.fms_revenue_account_id.id,
                    'name': f'{product.name} — {self.display_name}',
                    'debit': 0.0, 'credit': net,
                }))
                cr_total += net
                for tl in res['taxes']:
                    tax_account = (
                        self.env['account.tax'].browse(tl['id'])
                        .invoice_repartition_line_ids
                        .filtered(lambda r: r.repartition_type == 'tax' and r.account_id)[:1]
                        .account_id.id
                    )
                    if not tax_account:
                        cr_total += tl['amount']
                        continue
                    move_lines.append((0, 0, {
                        'account_id': tax_account,
                        'name': f"{tl['name']} — {product.name}",
                        'debit': 0.0, 'credit': tl['amount'],
                        'tax_line_id': tl['id'],
                    }))
                    cr_total += tl['amount']
            else:
                move_lines.append((0, 0, {
                    'account_id': product.fms_revenue_account_id.id,
                    'name': f'{product.name} — {self.display_name}',
                    'debit': 0.0, 'credit': gross,
                }))
                cr_total += gross

        if not move_lines:
            return False

        move_lines.insert(0, (0, 0, {
            'account_id': clearing_account.id,
            'name': f'Forecourt cash sales — {self.display_name}',
            'debit': cr_total, 'credit': 0.0,
        }))

        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': self.date,
            'ref': f'FMS Shift: {self.display_name}',
            'line_ids': move_lines,
        })
        move.action_post()
        return move


class FMSFuelDeliveryShiftLink(models.Model):
    _inherit = 'fms.fuel.delivery'

    shift_id = fields.Many2one(
        'fms.shift', 'Shift', ondelete='set null',
        help="Shift during which this delivery arrived.",
    )


class FMSAccountMoveExtension(models.Model):
    """Add FMS context fields to invoices and receipts."""
    _inherit = 'account.move'

    fms_shift_id = fields.Many2one(
        'fms.shift', 'FMS Shift',
        ondelete='restrict',
        help="The shift during which this sale was made.",
        copy=False,
    )
    fms_attendant_id = fields.Many2one(
        'hr.employee', 'Attendant',
        domain=[('fms_is_attendant', '=', True)],
        help="The pump attendant who made this sale.",
    )
    fms_vehicle_id = fields.Many2one(
        'fms.vehicle', 'Vehicle',
        help="The vehicle fuelled (for fleet/credit sales).",
    )
    fms_driver_id = fields.Many2one(
        'fms.driver', 'Driver',
        help="The driver at the pump (for fleet/credit sales).",
    )
    fms_station_id = fields.Many2one(
        'res.company', 'Station',
        related='fms_shift_id.company_id', store=True, readonly=True,
    )
