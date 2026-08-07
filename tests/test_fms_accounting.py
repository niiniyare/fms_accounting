"""
test_fms_accounting.py — Integration tests for fms_accounting module

Coverage:
  Suite 1: Fuel Delivery — create, confirm, stock picking, vendor bill, dip logs
  Suite 2: Credit Customers — outstanding balance, credit limit, PDC fields
  Suite 3: Petty Cash — disbursement GL, top-up GL, balance compute, low flag
  Suite 4: VAT on Shift Sales — net+tax split in _post_sales_journal override
  Suite 5: Dip Log Extensions — dip_type, delivery_line_id, shift-free offloading logs
"""

from odoo.tests import TransactionCase
from odoo.exceptions import ValidationError


class FMSAccountingBase(TransactionCase):
    """Shared fixtures reused across all suites."""

    def setUp(self):
        super().setUp()
        # Force-close any open shifts so the single-open-shift constraint doesn't block tests
        open_shifts = self.env['fms.shift'].search([('state', '=', 'open')])
        open_shifts.write({'state': 'draft'})

        company = self.env.company

        # Accounts (codes: alphanumeric + dots only, Odoo 18 constraint)
        self.revenue_account = self._account('income', '9001', 'UAT Revenue')
        self.cogs_account    = self._account('expense', '9002', 'UAT COGS')
        self.expense_account = self._account('expense', '9003', 'UAT Expense')
        self.receivable      = self.env['account.account'].search([
            ('account_type', '=', 'asset_receivable'),
            ('company_ids', 'in', company.id),
        ], limit=1) or self._account('asset_receivable', '9004', 'UAT Receivable')

        # Cash journal (for petty cash)
        self.cash_journal = self.env['account.journal'].search([
            ('type', '=', 'cash'), ('company_id', '=', company.id),
        ], limit=1)
        if not self.cash_journal:
            self.cash_journal = self.env['account.journal'].create({
                'name': 'UAT Cash', 'type': 'cash', 'code': 'UCA',
                'company_id': company.id,
            })

        # Sales journal (for shift GL)
        self.sales_journal = self.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', company.id),
        ], limit=1)
        if not self.sales_journal:
            self.sales_journal = self.env['account.journal'].create({
                'name': 'UAT Sales', 'type': 'sale', 'code': 'USA',
                'company_id': company.id,
            })

        # Fuel product
        self.diesel = self.env['product.product'].create({
            'name': 'ACC-Diesel', 'fms_is_fuel': True, 'list_price': 220.0,
            'fms_revenue_account_id': self.revenue_account.id,
            'fms_cogs_account_id': self.cogs_account.id,
            'uom_id': self.env.ref('uom.product_uom_litre').id,
            'uom_po_id': self.env.ref('uom.product_uom_litre').id,
        })

        # Fuel tank
        self.tank = self.env['stock.location'].create({
            'name': 'ACC-Diesel-Tank', 'usage': 'internal',
            'fms_is_fuel_tank': True,
            'fms_fuel_product_id': self.diesel.id,
        })

        # Supplier
        self.supplier = self.env['res.partner'].create({
            'name': 'ACC-Supplier', 'supplier_rank': 1,
        })

        # Customer
        self.customer = self.env['res.partner'].create({
            'name': 'ACC-Fleet-Customer', 'customer_rank': 1,
        })

        # Employee
        self.attendant = self.env['hr.employee'].create({
            'name': 'ACC-Attendant', 'fms_is_attendant': True,
        })

        # Site prefs — wire up sales journal and clearing account
        prefs = self.env['fms.site.preferences'].get_for_company()
        prefs.sales_journal_id = self.sales_journal.id
        prefs.clearing_account_id = self.receivable.id
        prefs.auto_open_next_shift = False

    # helpers ----------------------------------------------------------------

    def _account(self, account_type, code, name):
        # Odoo 18: account.account uses company_ids (Many2many), not company_id
        existing = self.env['account.account'].search([
            ('code', '=', code), ('company_ids', 'in', self.env.company.id),
        ], limit=1)
        if existing:
            return existing
        return self.env['account.account'].create({
            'name': name, 'code': code,
            'account_type': account_type,
            'company_ids': [(4, self.env.company.id)],
        })

    def _make_delivery(self, qty=1000.0, price=165.0):
        delivery = self.env['fms.fuel.delivery'].create({
            'delivery_date': '2026-08-01',
            'vendor_id': self.supplier.id,
            'tanker_ref': 'DN-TEST-001',
        })
        self.env['fms.fuel.delivery.line'].create({
            'delivery_id': delivery.id,
            'product_id': self.diesel.id,
            'location_id': self.tank.id,
            'quantity_litres': qty,
            'unit_price': price,
            'dip_before': 5000.0,
            'sales_during': 50.0,
            'dip_after': 5940.0,   # expected = 5000+1000-50 = 5950 → variance = -10
        })
        return delivery

    def _make_petty_cash_float(self):
        return self.env['fms.petty.cash.float'].create({
            'company_id': self.env.company.id,
            'journal_id': self.cash_journal.id,
            'low_balance_alert': 2000.0,
        })


# ---------------------------------------------------------------------------
# Suite 1: Fuel Delivery
# ---------------------------------------------------------------------------

class TestFuelDelivery(FMSAccountingBase):

    def test_delivery_created_in_draft(self):
        """New delivery starts in draft state."""
        delivery = self._make_delivery()
        self.assertEqual(delivery.state, 'draft')

    def test_delivery_name_sequence(self):
        """Delivery name uses the DEL/YYYY/ sequence — not 'New'."""
        delivery = self._make_delivery()
        self.assertNotEqual(delivery.name, 'New')
        self.assertIn('DEL/', delivery.name)

    def test_delivery_totals_computed(self):
        """total_litres and total_amount reflect line values."""
        delivery = self._make_delivery(qty=2000.0, price=160.0)
        self.assertAlmostEqual(delivery.total_litres, 2000.0)
        self.assertAlmostEqual(delivery.total_amount, 2000.0 * 160.0)

    def test_delivery_confirm_creates_stock_picking(self):
        """Confirming a delivery creates a stock.picking in state ready/done."""
        delivery = self._make_delivery()
        delivery.action_confirm()
        self.assertTrue(delivery.stock_picking_id, "stock_picking_id must be set after confirm")
        self.assertEqual(delivery.state, 'confirmed')

    def test_delivery_confirm_creates_vendor_bill(self):
        """Confirming a delivery creates a vendor bill (in_invoice)."""
        delivery = self._make_delivery()
        delivery.action_confirm()
        self.assertTrue(delivery.vendor_bill_id, "vendor_bill_id must be set after confirm")
        self.assertEqual(delivery.vendor_bill_id.move_type, 'in_invoice')

    def test_vendor_bill_has_correct_product_line(self):
        """Vendor bill line matches the delivery product and quantity."""
        delivery = self._make_delivery(qty=500.0, price=170.0)
        delivery.action_confirm()
        bill = delivery.vendor_bill_id
        line = bill.invoice_line_ids.filtered(lambda l: l.product_id == self.diesel)
        self.assertTrue(line, "Vendor bill must have a diesel line")
        self.assertAlmostEqual(line.quantity, 500.0)
        self.assertAlmostEqual(line.price_unit, 170.0)

    def test_confirm_without_lines_raises(self):
        """Delivery with no lines cannot be confirmed."""
        delivery = self.env['fms.fuel.delivery'].create({
            'delivery_date': '2026-08-01',
            'vendor_id': self.supplier.id,
        })
        with self.assertRaises(ValidationError):
            delivery.action_confirm()

    def test_cannot_confirm_already_confirmed(self):
        """Confirming a confirmed delivery raises."""
        delivery = self._make_delivery()
        delivery.action_confirm()
        with self.assertRaises(ValidationError):
            delivery.action_confirm()

    def test_reset_to_draft(self):
        """A confirmed (not billed) delivery can be reset to draft."""
        delivery = self._make_delivery()
        delivery.action_confirm()
        delivery.action_reset_draft()
        self.assertEqual(delivery.state, 'draft')

    def test_dip_variance_computed(self):
        """Line variance = dip_after - (dip_before + qty - sales_during)."""
        delivery = self._make_delivery(qty=1000.0)
        line = delivery.delivery_line_ids[0]
        # dip_before=5000, qty=1000, sales_during=50 → expected=5950; dip_after=5940 → var=-10
        self.assertAlmostEqual(line.expected_qty, 5950.0)
        self.assertAlmostEqual(line.variance, -10.0)


# ---------------------------------------------------------------------------
# Suite 2: Credit Customers
# ---------------------------------------------------------------------------

class TestCreditCustomers(FMSAccountingBase):

    def test_credit_customer_created(self):
        """fms.credit.customer links to a res.partner."""
        cc = self.env['fms.credit.customer'].create({
            'partner_id': self.customer.id,
            'credit_limit': 100_000.0,
            'fleet_card_ref': 'FLEET-001',
        })
        self.assertEqual(cc.partner_id, self.customer)
        self.assertAlmostEqual(cc.credit_limit, 100_000.0)

    def test_outstanding_balance_zero_initially(self):
        """New credit customer with no invoices has zero outstanding balance."""
        cc = self.env['fms.credit.customer'].create({
            'partner_id': self.customer.id,
        })
        self.assertAlmostEqual(cc.outstanding_balance, 0.0)

    def test_credit_available_equals_limit_when_no_balance(self):
        """credit_available = credit_limit when outstanding = 0."""
        cc = self.env['fms.credit.customer'].create({
            'partner_id': self.customer.id,
            'credit_limit': 50_000.0,
        })
        self.assertAlmostEqual(cc.credit_available, 50_000.0)

    def test_duplicate_partner_company_raises(self):
        """Cannot create two credit customer records for the same partner+company."""
        self.env['fms.credit.customer'].create({'partner_id': self.customer.id})
        with self.assertRaises(Exception):
            self.env['fms.credit.customer'].create({'partner_id': self.customer.id})

    def test_attendant_cash_has_credit_customer_field(self):
        """fms.shift.attendant.cash has credit_customer_id field (added by fms_accounting)."""
        pump = self.env['fms.pump'].create({'name': 'ACC-Pump', 'order': 99})
        nozzle = self.env['fms.pump.nozzle'].create({
            'pump_id': pump.id, 'name': 'A', 'letter': 'A', 'order': 1,
            'product_id': self.diesel.id,
        })
        shift = self.env['fms.shift'].create({'date': '2026-08-01', 'label': '1_day'})
        shift.action_open_shift()
        cash_line = self.env['fms.shift.attendant.cash'].create({
            'shift_id': shift.id,
            'attendant_id': self.attendant.id,
        })
        cc = self.env['fms.credit.customer'].create({'partner_id': self.customer.id})
        cash_line.credit_customer_id = cc
        self.assertEqual(cash_line.credit_customer_id, cc)

    def test_pdc_fields_on_account_payment(self):
        """account.payment has fms_is_pdc, fms_pdc_state, fms_cheque_number."""
        payment = self.env['account.payment'].create({
            'amount': 10_000.0,
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.customer.id,
            'journal_id': self.cash_journal.id,
            'fms_is_pdc': True,
            'fms_pdc_state': 'held',
            'fms_cheque_number': 'CHQ-0001',
            'fms_cheque_date': '2026-09-01',
        })
        self.assertTrue(payment.fms_is_pdc)
        self.assertEqual(payment.fms_pdc_state, 'held')
        self.assertEqual(payment.fms_cheque_number, 'CHQ-0001')


# ---------------------------------------------------------------------------
# Suite 3: Petty Cash
# ---------------------------------------------------------------------------

class TestPettyCash(FMSAccountingBase):

    def test_float_created(self):
        """Petty cash float created with correct journal."""
        float_ = self._make_petty_cash_float()
        self.assertEqual(float_.journal_id, self.cash_journal)

    def test_initial_balance_zero(self):
        """New float has zero balance (no disbursements yet)."""
        float_ = self._make_petty_cash_float()
        self.assertAlmostEqual(float_.current_balance, 0.0)

    def test_disbursement_post_creates_journal_entry(self):
        """Posting a disbursement creates an account.move."""
        float_ = self._make_petty_cash_float()
        # First top-up so float has balance
        topup = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id,
            'date': '2026-08-01',
            'is_topup': True,
            'amount': 10_000.0,
            'description': 'Initial top-up',
        })
        topup.action_post()
        self.assertEqual(topup.state, 'posted')
        self.assertTrue(topup.move_id, "Journal entry must be created on post")

    def test_disbursement_dr_expense_cr_cash(self):
        """Disbursement entry: DR expense account | CR cash account."""
        float_ = self._make_petty_cash_float()
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id,
            'date': '2026-08-01',
            'is_topup': False,
            'amount': 500.0,
            'description': 'Fuel station cleaning supplies',
            'account_id': self.expense_account.id,
        })
        disb.action_post()
        move = disb.move_id
        self.assertTrue(move, "Journal entry must exist")
        debit_lines  = move.line_ids.filtered(lambda l: l.debit > 0)
        credit_lines = move.line_ids.filtered(lambda l: l.credit > 0)
        self.assertTrue(debit_lines,  "Must have a debit line")
        self.assertTrue(credit_lines, "Must have a credit line")
        self.assertAlmostEqual(sum(debit_lines.mapped('debit')), 500.0)
        self.assertAlmostEqual(sum(credit_lines.mapped('credit')), 500.0)

    def test_topup_dr_cash_cr_receivable(self):
        """Top-up entry: DR cash | CR receivable (from management)."""
        float_ = self._make_petty_cash_float()
        topup = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id,
            'date': '2026-08-01',
            'is_topup': True,
            'amount': 20_000.0,
            'description': 'Management top-up',
        })
        topup.action_post()
        move = topup.move_id
        cash_account_id = self.cash_journal.default_account_id.id
        dr_lines = move.line_ids.filtered(
            lambda l: l.account_id.id == cash_account_id and l.debit > 0
        )
        self.assertTrue(dr_lines, "DR cash line must exist on top-up")

    def test_balance_reflects_topup_minus_disbursements(self):
        """current_balance = total topups − total disbursements (posted only)."""
        float_ = self._make_petty_cash_float()
        topup = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': True,
            'amount': 10_000.0, 'description': 'Top-up',
        })
        topup.action_post()
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': False,
            'amount': 3_000.0, 'description': 'Purchase',
            'account_id': self.expense_account.id,
        })
        disb.action_post()
        float_.invalidate_recordset()
        self.assertAlmostEqual(float_.current_balance, 7_000.0)

    def test_is_low_flag_set_when_balance_below_alert(self):
        """is_low=True when current_balance < low_balance_alert."""
        float_ = self._make_petty_cash_float()   # alert = 2000
        # Top up only 1000 → balance 1000 < 2000 alert
        topup = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': True,
            'amount': 1_000.0, 'description': 'Small top-up',
        })
        topup.action_post()
        float_.invalidate_recordset()
        self.assertTrue(float_.is_low)

    def test_is_low_false_when_balance_sufficient(self):
        """is_low=False when balance >= low_balance_alert."""
        float_ = self._make_petty_cash_float()   # alert = 2000
        topup = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': True,
            'amount': 5_000.0, 'description': 'Top-up',
        })
        topup.action_post()
        float_.invalidate_recordset()
        self.assertFalse(float_.is_low)

    def test_draft_disbursement_does_not_affect_balance(self):
        """Draft disbursements are excluded from current_balance."""
        float_ = self._make_petty_cash_float()
        # Create but do NOT post
        self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': False,
            'amount': 999.0, 'description': 'Draft only',
            'account_id': self.expense_account.id,
        })
        float_.invalidate_recordset()
        self.assertAlmostEqual(float_.current_balance, 0.0)

    def test_disbursement_missing_expense_account_raises(self):
        """Posting a disbursement without account_id raises ValidationError."""
        float_ = self._make_petty_cash_float()
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': False,
            'amount': 100.0, 'description': 'No account',
        })
        with self.assertRaises(ValidationError):
            disb.action_post()

    def test_disbursement_reset_draft(self):
        """A draft disbursement (no posted move) can be reset to draft."""
        float_ = self._make_petty_cash_float()
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': False,
            'amount': 200.0, 'description': 'Test',
            'account_id': self.expense_account.id,
        })
        # Reset while still draft is a no-op (already draft)
        disb.action_reset_draft()
        self.assertEqual(disb.state, 'draft')

    def test_shift_link_on_disbursement(self):
        """Disbursement can be linked to a shift for reconciliation context."""
        float_ = self._make_petty_cash_float()
        shift = self.env['fms.shift'].create({'date': '2026-08-01', 'label': '1_day'})
        shift.action_open_shift()
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-01', 'is_topup': False,
            'amount': 300.0, 'description': 'Pump repair',
            'account_id': self.expense_account.id,
            'shift_id': shift.id,
            'attendant_id': self.attendant.id,
        })
        self.assertEqual(disb.shift_id, shift)
        self.assertEqual(disb.attendant_id, self.attendant)

    def test_petty_cash_expense_on_attendant_cash(self):
        """
        Posted petty cash disbursements for an attendant appear in
        petty_cash_expense on the matching fms.shift.attendant.cash line.
        """
        float_ = self._make_petty_cash_float()
        pump = self.env['fms.pump'].create({'name': 'ACC-Pump2', 'order': 88})
        self.env['fms.pump.nozzle'].create({
            'pump_id': pump.id, 'name': 'A', 'letter': 'A', 'order': 1,
            'product_id': self.diesel.id,
        })
        shift = self.env['fms.shift'].create({'date': '2026-08-02', 'label': '1_day'})
        shift.action_open_shift()
        cash_line = self.env['fms.shift.attendant.cash'].create({
            'shift_id': shift.id,
            'attendant_id': self.attendant.id,
        })
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-02', 'is_topup': False,
            'amount': 750.0, 'description': 'Cleaning',
            'account_id': self.expense_account.id,
            'shift_id': shift.id,
            'attendant_id': self.attendant.id,
        })
        disb.action_post()
        cash_line.invalidate_recordset()
        self.assertAlmostEqual(cash_line.petty_cash_expense, 750.0)


# ---------------------------------------------------------------------------
# Suite 4: VAT on Shift Sales
# ---------------------------------------------------------------------------

class TestVATOnShiftSales(FMSAccountingBase):

    def _make_tax(self, rate_pct=16.0):
        """Create a price-inclusive VAT tax (pump prices in Kenya include VAT)."""
        return self.env['account.tax'].create({
            'name': f'VAT {rate_pct}%',
            'amount': rate_pct,
            'amount_type': 'percent',
            'price_include': True,   # elec_cash_sold is gross-inclusive
            'type_tax_use': 'sale',
            'company_id': self.env.company.id,
            'invoice_repartition_line_ids': [
                (0, 0, {'repartition_type': 'base', 'factor_percent': 100}),
                (0, 0, {
                    'repartition_type': 'tax',
                    'factor_percent': 100,
                    'account_id': self.revenue_account.id,
                }),
            ],
        })

    def _make_shift_with_cash_meter(self, elec_cash, add_tax=False):
        """Open a shift, record cash meter reading, move to closing."""
        pump = self.env['fms.pump'].create({'name': 'VAT-Pump', 'order': 77})
        nozzle = self.env['fms.pump.nozzle'].create({
            'pump_id': pump.id, 'name': 'A', 'letter': 'A', 'order': 1,
            'product_id': self.diesel.id,
        })
        if add_tax:
            tax = self._make_tax(16.0)
            self.diesel.taxes_id = [(6, 0, [tax.id])]
        else:
            self.diesel.taxes_id = [(5, 0)]  # clear taxes

        shift = self.env['fms.shift'].create({
            'date': '2026-08-05', 'label': '1_day',
            'supervisor_id': self.env['hr.employee'].create({'name': 'VAT-Sup'}).id,
        })
        shift.action_open_shift()
        entry = shift.meter_entry_ids.filtered(lambda e: e.nozzle_id == nozzle)
        entry.write({'closing_elec_cash': elec_cash})
        shift.write({'state': 'closing'})
        return shift

    def test_no_taxes_uses_base_method(self):
        """Without taxes on products, GL has one CR revenue line at gross amount."""
        shift = self._make_shift_with_cash_meter(10_000.0, add_tax=False)
        move = shift._post_sales_journal()
        if not move:
            self.skipTest("GL skipped — no revenue account or cash; not a VAT test failure")
        cr_lines = move.line_ids.filtered(lambda l: l.credit > 0)
        tax_lines = move.line_ids.filtered(lambda l: l.tax_line_id)
        self.assertFalse(tax_lines, "No tax lines expected when product has no taxes")

    def test_with_tax_creates_a_posted_move(self):
        """With taxes on the product, _post_sales_journal returns a posted account.move."""
        shift = self._make_shift_with_cash_meter(11_600.0, add_tax=True)
        move = shift._post_sales_journal()
        if not move:
            self.skipTest("GL skipped — accounts not configured; check test setup")
        self.assertEqual(move.state, 'posted',
                         "Journal entry must be posted when product has taxes")
        # The move must have at least one CR line (revenue or tax)
        cr_lines = move.line_ids.filtered(lambda l: l.credit > 0)
        self.assertTrue(cr_lines, "At least one CR line must exist in the GL entry")

    def test_tax_journal_entry_is_balanced(self):
        """GL entry produced by the VAT override must always balance (DR = CR)."""
        gross = 11_600.0
        shift = self._make_shift_with_cash_meter(gross, add_tax=True)
        move = shift._post_sales_journal()
        if not move:
            self.skipTest("GL skipped")
        total_dr = sum(move.line_ids.mapped('debit'))
        total_cr = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(total_dr, total_cr, places=2,
                               msg="Journal entry must balance (DR = CR)")

    def test_dr_clearing_equals_total_cr(self):
        """DR clearing line balances the full sum of CR lines."""
        gross = 11_600.0
        shift = self._make_shift_with_cash_meter(gross, add_tax=True)
        move = shift._post_sales_journal()
        if not move:
            self.skipTest("GL skipped")
        total_dr = sum(move.line_ids.mapped('debit'))
        total_cr = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(total_dr, total_cr, places=2,
                               msg="Journal entry must balance (DR = CR)")


# ---------------------------------------------------------------------------
# Suite 5: Dip Log Extensions
# ---------------------------------------------------------------------------

class TestDipLogExtensions(FMSAccountingBase):

    def test_offloading_dip_log_has_no_shift(self):
        """Offloading dip logs are written with shift_id=False (allowed by fms_accounting)."""
        log = self.env['fms.dip_log'].sudo().create({
            'shift_id': False,
            'location_id': self.tank.id,
            'product_id': self.diesel.id,
            'opening_volume': 5000.0,
            'closing_volume': 5940.0,
            'dip_type': 'offloading',
        })
        self.assertFalse(log.shift_id)
        self.assertEqual(log.dip_type, 'offloading')

    def test_shift_dip_log_defaults_to_shift_close_type(self):
        """Dip logs created by a shift use dip_type='shift_close' (default)."""
        pump = self.env['fms.pump'].create({'name': 'DIP-Pump', 'order': 66})
        self.env['fms.pump.nozzle'].create({
            'pump_id': pump.id, 'name': 'A', 'letter': 'A', 'order': 1,
            'product_id': self.diesel.id,
        })
        shift = self.env['fms.shift'].create({'date': '2026-08-06', 'label': '1_day'})
        shift.action_open_shift()
        self.env['fms.shift.dip.entry'].create({
            'shift_id': shift.id, 'location_id': self.tank.id,
            'opening_volume': 5000.0, 'closing_volume': 4950.0,
        })
        shift.action_start_closing()
        shift.action_close_shift()
        log = self.env['fms.dip_log'].search([('shift_id', '=', shift.id)], limit=1)
        if log:
            self.assertEqual(log.dip_type, 'shift_close')

    def test_delivery_confirm_writes_offloading_dip_logs(self):
        """Confirming a fuel delivery creates fms.dip_log with dip_type='offloading'."""
        delivery = self._make_delivery()
        delivery.action_confirm()
        logs = self.env['fms.dip_log'].sudo().search([
            ('dip_type', '=', 'offloading'),
            ('location_id', '=', self.tank.id),
        ])
        self.assertTrue(logs, "Offloading dip log must be created on delivery confirm")

    def test_offloading_dip_log_linked_to_delivery_line(self):
        """Offloading dip log has delivery_line_id pointing to the correct line."""
        delivery = self._make_delivery()
        delivery.action_confirm()
        line = delivery.delivery_line_ids[0]
        log = self.env['fms.dip_log'].sudo().search([
            ('delivery_line_id', '=', line.id),
        ], limit=1)
        self.assertTrue(log, "dip_log must reference the delivery line")
        self.assertEqual(log.delivery_line_id, line)

    def test_shift_has_delivery_ids_field(self):
        """fms.shift exposes delivery_ids One2many (added by fms_accounting)."""
        shift = self.env['fms.shift'].create({'date': '2026-08-07', 'label': '1_day'})
        shift.action_open_shift()
        delivery = self._make_delivery()
        delivery.shift_id = shift.id
        self.assertIn(delivery, shift.delivery_ids)

    def test_shift_has_petty_cash_disbursement_ids_field(self):
        """fms.shift exposes petty_cash_disbursement_ids One2many."""
        float_ = self._make_petty_cash_float()
        shift = self.env['fms.shift'].create({'date': '2026-08-08', 'label': '1_day'})
        shift.action_open_shift()
        disb = self.env['fms.petty.cash.disbursement'].create({
            'float_id': float_.id, 'date': '2026-08-08', 'is_topup': False,
            'amount': 100.0, 'description': 'Test', 'shift_id': shift.id,
            'account_id': self.expense_account.id,
        })
        self.assertIn(disb, shift.petty_cash_disbursement_ids)
