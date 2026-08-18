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


class FMSAttendantCashAccountingExt(models.Model):
    """Add credit customer link to attendant cash line."""
    _inherit = 'fms.shift.attendant.cash'

    credit_customer_id = fields.Many2one(
        'res.partner', 'Credit Customer',
        domain=[('fms_is_fleet_customer', '=', True)],
        help="The fleet/credit account customer for credit sales on this line.",
    )


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

    @api.onchange('fms_vehicle_id')
    def _onchange_fms_vehicle_id(self):
        """Auto-populate vehicle_reg from selected vehicle."""
        for move in self:
            if move.fms_vehicle_id:
                move.fms_vehicle_reg = move.fms_vehicle_id.license_plate or ''


class FMSSitePreferencesAccountingExt(models.Model):
    _inherit = 'fms.site.preferences'

    @api.model
    def _ensure_fms_gl_setup(self):
        """Find-or-create FMS GL accounts and journal, then wire into site preferences.

        Safe for both fresh install (creates records) and existing DBs (finds by code).
        Called from fms_accounting_company_defaults.xml instead of raw <record> blocks.
        """
        company = self.env.company
        Account = self.env['account.account']
        Journal = self.env['account.journal']

        def _find_or_create_account(code, name, account_type, reconcile=False):
            acc = Account.search([
                ('code', '=', code),
                ('company_ids', 'in', company.id),
            ], limit=1)
            if not acc:
                acc = Account.create({
                    'code': code,
                    'name': name,
                    'account_type': account_type,
                    'company_ids': [(4, company.id)],
                    'reconcile': reconcile,
                })
            return acc

        def _find_or_create_journal(code, name, journal_type):
            jnl = Journal.search([
                ('code', '=', code),
                ('company_id', '=', company.id),
            ], limit=1)
            if not jnl:
                jnl = Journal.create({
                    'code': code,
                    'name': name,
                    'type': journal_type,
                    'company_id': company.id,
                })
            return jnl

        clearing = _find_or_create_account(
            '191600', 'FMS Cash Clearing', 'asset_current', reconcile=True)
        cogs = _find_or_create_account(
            '591000', 'Fuel Cost of Sales', 'expense_direct_cost')
        journal = _find_or_create_journal('FCST', 'Forecourt Sales', 'sale')

        self._fms_configure_defaults(clearing.id, cogs.id, journal.id)

        # Find-or-create Kenya VAT tax group and taxes (idempotent — safe for existing DBs)
        Tax = self.env['account.tax']
        kenya = self.env.ref('base.ke', raise_if_not_found=False)
        TaxGroup = self.env['account.tax.group']
        tax_group = TaxGroup.search([
            ('name', '=', 'VAT'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not tax_group:
            vals = {'name': 'VAT', 'company_id': company.id}
            if kenya:
                vals['country_id'] = kenya.id
            tax_group = TaxGroup.create(vals)

        def _find_or_create_tax(name, amount, use, group):
            tax = Tax.search([
                ('name', '=', name),
                ('type_tax_use', '=', use),
                ('company_id', '=', company.id),
            ], limit=1)
            if not tax:
                vals = {
                    'name': name,
                    'amount': amount,
                    'amount_type': 'percent',
                    'type_tax_use': use,
                    'price_include': False,
                    'company_id': company.id,
                }
                if kenya:
                    vals['country_id'] = kenya.id
                if group:
                    vals['tax_group_id'] = group.id
                Tax.create(vals)

        _find_or_create_tax('VAT 16%', 16.0, 'sale',     tax_group)
        _find_or_create_tax('VAT 8%',   8.0, 'sale',     tax_group)
        _find_or_create_tax('VAT 16%', 16.0, 'purchase', tax_group)
        _find_or_create_tax('VAT 8%',   8.0, 'purchase', tax_group)
