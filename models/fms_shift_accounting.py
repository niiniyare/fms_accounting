"""
fms_shift_accounting.py — Extends fms.shift and fms.dip_log for accounting features.

1. fms.shift gets delivery_ids and petty_cash_disbursement_ids One2many fields.
2. _post_sales_journal is extended to split revenue lines into net + tax
   using Odoo's native account.tax.compute_all().
3. expense_amount on fms.shift.attendant.cash is extended to also include
   petty cash disbursements for the shift/attendant combination.
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
    petty_cash_disbursement_ids = fields.One2many(
        'fms.petty.cash.disbursement', 'shift_id', 'Petty Cash',
        help="Petty cash disbursements charged to this shift.",
    )

    # ------------------------------------------------------------------
    # Override _post_sales_journal to split net + tax per product
    # ------------------------------------------------------------------

    def _post_sales_journal(self):
        """
        Override: if a fuel product has taxes_id configured, split the
        revenue GL line into net amount + tax line(s) using compute_all().

        Falls back to the base implementation (no tax) when no taxes
        are configured on any product — fully backward-compatible.
        """
        self.ensure_one()
        import logging as _log
        _log = _log.getLogger(__name__)

        # Flush ORM cache so taxes_id reflects any mid-transaction changes
        self.env['product.product'].flush_model(['taxes_id'])
        # Check if any product has taxes — if not, use base method unchanged
        has_taxes = any(
            e.product_id.taxes_id
            for e in self.meter_entry_ids
            if e.product_id
        )
        if not has_taxes:
            return super()._post_sales_journal()

        # Replicate base logic but inject tax lines
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
        has_revenue_accounts = any(p.fms_revenue_account_id for p in products)
        if not has_revenue_accounts:
            _log.warning(
                "FMS Accounting: Shift %s has no fms_revenue_account_id — GL skipped.",
                self.display_name,
            )
            return False

        journal = self._get_fms_journal()
        clearing_account = self._get_clearing_account()
        company = self.company_id

        move_lines = []
        cr_total = 0.0

        for product in products:
            gross = cash_by_product.get(product.id, 0.0)
            if abs(gross) < 0.01:
                continue
            if not product.fms_revenue_account_id:
                _log.warning(
                    "FMS Accounting: Product %s has no revenue account — skipped.",
                    product.name,
                )
                continue

            taxes = product.taxes_id.filtered(
                lambda t: t.company_id == company
            )

            if taxes:
                tax_result = taxes.compute_all(
                    gross, currency=company.currency_id,
                    quantity=1.0, product=product,
                )
                net = tax_result['total_excluded']

                # CR revenue at net amount
                move_lines.append((0, 0, {
                    'account_id': product.fms_revenue_account_id.id,
                    'name': f'{product.name} — {self.display_name}',
                    'debit': 0.0,
                    'credit': net,
                }))
                cr_total += net

                # CR tax lines
                for tax_line in tax_result['taxes']:
                    tax_account_id = (
                        tax_line.get('account_id')
                        or self.env['account.tax'].browse(tax_line['id']).invoice_repartition_line_ids
                           .filtered(lambda r: r.repartition_type == 'tax' and r.account_id)
                           [:1].account_id.id
                    )
                    if not tax_account_id:
                        _log.warning(
                            "FMS Accounting: Tax %s has no account — tax line skipped.",
                            tax_line.get('name'),
                        )
                        cr_total += tax_line['amount']
                        continue
                    move_lines.append((0, 0, {
                        'account_id': tax_account_id,
                        'name': f"{tax_line['name']} — {product.name}",
                        'debit': 0.0,
                        'credit': tax_line['amount'],
                        'tax_line_id': tax_line['id'],
                    }))
                    cr_total += tax_line['amount']
            else:
                # No taxes on this product — post gross
                move_lines.append((0, 0, {
                    'account_id': product.fms_revenue_account_id.id,
                    'name': f'{product.name} — {self.display_name}',
                    'debit': 0.0,
                    'credit': gross,
                }))
                cr_total += gross

        if not move_lines:
            return False

        # DR clearing for exact sum of CRs
        move_lines.insert(0, (0, 0, {
            'account_id': clearing_account.id,
            'name': f'Forecourt cash sales — {self.display_name}',
            'debit': cr_total,
            'credit': 0.0,
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


# ---------------------------------------------------------------------------
# Link fms.fuel.delivery back to a shift
# ---------------------------------------------------------------------------

class FMSFuelDeliveryShiftLink(models.Model):
    _inherit = 'fms.fuel.delivery'

    shift_id = fields.Many2one(
        'fms.shift', 'Shift',
        help="Shift during which this delivery arrived (optional, for reconciliation context).",
        ondelete='set null',
    )


# ---------------------------------------------------------------------------
# Extend expense_amount on attendant cash to include petty cash disbursements
# ---------------------------------------------------------------------------

class FMSShiftAttendantCashExpense(models.Model):
    _inherit = 'fms.shift.attendant.cash'

    @api.depends(
        'shift_id', 'attendant_id',
        'shift_id.petty_cash_disbursement_ids.amount',
        'shift_id.petty_cash_disbursement_ids.state',
        'shift_id.petty_cash_disbursement_ids.attendant_id',
    )
    def _compute_expense_from_petty_cash(self):
        """
        Add posted petty cash disbursements for this attendant+shift to expense_amount.
        Called alongside the base _compute_expense method (which reads vendor bills).
        """
        for rec in self:
            disbursements = rec.shift_id.petty_cash_disbursement_ids.filtered(
                lambda d: d.attendant_id == rec.attendant_id and d.state == 'posted'
            )
            rec.petty_cash_expense = sum(disbursements.mapped('amount'))

    petty_cash_expense = fields.Float(
        'Petty Cash Expenses (KES)', compute='_compute_expense_from_petty_cash',
        digits=(16, 2),
        help="Sum of posted petty cash disbursements for this attendant on this shift.",
    )
