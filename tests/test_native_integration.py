"""
test_native_integration.py — Tests for Phase B native Odoo integration.

Coverage:
  Suite 1: Invoice auto-populate from active shift
  Suite 2: Invoice blocked when no active shift
  Suite 3: Vehicle / driver → customer auto-resolution
  Suite 4: Vehicle / customer mismatch validation
  Suite 5: Odometer field on invoice
"""

from odoo.tests import TransactionCase
from odoo.exceptions import ValidationError, UserError


class NativeIntegrationBase(TransactionCase):

    def setUp(self):
        super().setUp()
        # Reset any open shifts
        self.env['fms.shift'].search([('state', 'in', ['open', 'closing'])]).write({'state': 'draft'})

        company = self.env.company

        # Minimal GL setup
        Account = self.env['account.account']
        self.revenue_account = Account.search([
            ('account_type', '=', 'income'),
            ('company_ids', 'in', company.id),
        ], limit=1)
        if not self.revenue_account:
            self.revenue_account = Account.create({
                'code': 'NIT9001', 'name': 'NIT Revenue',
                'account_type': 'income', 'company_ids': [(4, company.id)],
            })

        # Shift prerequisites
        self.supervisor = self.env['hr.employee'].search([], limit=1)
        if not self.supervisor:
            self.supervisor = self.env['hr.employee'].create({'name': 'NIT Supervisor'})

        # Sales journal
        self.sales_journal = self.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', company.id),
        ], limit=1)
        if not self.sales_journal:
            self.sales_journal = self.env['account.journal'].create({
                'name': 'NIT Sales', 'type': 'sale', 'code': 'NIS',
                'company_id': company.id,
            })

        # Customer partner
        self.customer = self.env['res.partner'].create({
            'name': 'NIT Fleet Customer',
            'is_company': True,
            'fms_is_fleet_customer': True,
        })

        # Another customer (for mismatch tests)
        self.other_customer = self.env['res.partner'].create({
            'name': 'NIT Other Customer',
            'is_company': True,
        })

    def _open_shift(self):
        shift = self.env['fms.shift'].create({
            'date': '2026-08-21',
            'label': '1_day',
            'supervisor_id': self.supervisor.id,
        })
        shift.write({'state': 'open'})
        return shift

    def _make_vehicle(self, partner=None, drivers=None):
        vehicle = self.env['fms.vehicle'].create({
            'license_plate': 'KAA 000X',
            'partner_id': (partner or self.customer).id,
        })
        if drivers:
            vehicle.driver_ids = [(6, 0, [d.id for d in drivers])]
        return vehicle

    def _make_driver(self, partner=None, vehicles=None):
        driver = self.env['fms.driver'].create({
            'name': 'NIT Driver',
            'partner_id': (partner or self.customer).id,
        })
        if vehicles:
            driver.vehicle_ids = [(6, 0, [v.id for v in vehicles])]
        return driver


class TestInvoiceAutoPopulate(NativeIntegrationBase):

    def test_default_get_populates_shift_from_active_shift(self):
        """default_get sets fms_shift_id and invoice_date from active shift."""
        shift = self._open_shift()
        vals = self.env['account.move'].with_context(
            fms_invoice_context=True,
            default_move_type='out_invoice',
        ).default_get(['fms_shift_id', 'invoice_date', 'move_type'])
        self.assertEqual(vals.get('fms_shift_id'), shift.id)
        self.assertEqual(str(vals.get('invoice_date')), '2026-08-21')

    def test_create_auto_populates_shift(self):
        """Creating with fms_invoice_context auto-links to active shift."""
        shift = self._open_shift()
        move = self.env['account.move'].with_context(
            fms_invoice_context=True,
        ).create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
        })
        self.assertEqual(move.fms_shift_id, shift)

    def test_fms_station_derived_from_shift(self):
        """fms_station_id = shift.company_id (auto-derived)."""
        shift = self._open_shift()
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
            'fms_shift_id': shift.id,
        })
        self.assertEqual(move.fms_station_id, self.env.company)


class TestInvoiceBlockedNoShift(NativeIntegrationBase):

    def test_create_raises_if_no_active_shift(self):
        """Invoice creation from Forecourt context blocked when no active shift."""
        # All shifts are in draft (setUp reset them)
        with self.assertRaises((ValidationError, UserError)):
            self.env['account.move'].with_context(
                fms_invoice_context=True,
            ).create({
                'move_type': 'out_invoice',
                'partner_id': self.customer.id,
            })

    def test_create_without_context_not_blocked(self):
        """Normal (non-FMS-context) invoice creation is never blocked."""
        # Should not raise even with no active shift
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
        })
        self.assertEqual(move.move_type, 'out_invoice')


class TestVehicleDriverAutoResolution(NativeIntegrationBase):

    def test_vehicle_auto_populates_driver_when_one_driver(self):
        """Selecting vehicle with one driver auto-sets fms_driver_id via onchange."""
        driver = self._make_driver()
        vehicle = self._make_vehicle(drivers=[driver])

        move = self.env['account.move'].new({'move_type': 'out_invoice'})
        move.fms_vehicle_id = vehicle
        move._onchange_fms_vehicle_id()

        self.assertEqual(move.fms_driver_id, driver)

    def test_vehicle_does_not_auto_set_driver_when_multiple(self):
        """Selecting vehicle with multiple drivers does NOT auto-set driver (ambiguous)."""
        d1 = self._make_driver()
        d2 = self.env['fms.driver'].create({'name': 'NIT Driver 2', 'partner_id': self.customer.id})
        vehicle = self._make_vehicle(drivers=[d1, d2])

        move = self.env['account.move'].new({'move_type': 'out_invoice'})
        move.fms_vehicle_id = vehicle
        move._onchange_fms_vehicle_id()

        self.assertFalse(move.fms_driver_id)

    def test_driver_auto_populates_customer(self):
        """Selecting driver auto-sets partner_id from driver.partner_id."""
        driver = self._make_driver()

        move = self.env['account.move'].new({'move_type': 'out_invoice'})
        move.fms_driver_id = driver
        move._onchange_fms_driver_id()

        self.assertEqual(move.partner_id, self.customer)

    def test_driver_auto_populates_vehicle_when_one_vehicle(self):
        """Selecting driver with one vehicle auto-sets fms_vehicle_id."""
        vehicle = self._make_vehicle()
        driver = self._make_driver(vehicles=[vehicle])

        move = self.env['account.move'].new({'move_type': 'out_invoice'})
        move.fms_driver_id = driver
        move._onchange_fms_driver_id()

        self.assertEqual(move.fms_vehicle_id, vehicle)

    def test_vehicle_auto_populates_customer(self):
        """Selecting vehicle auto-sets partner_id from vehicle.partner_id."""
        vehicle = self._make_vehicle()

        move = self.env['account.move'].new({'move_type': 'out_invoice'})
        move.fms_vehicle_id = vehicle
        move._onchange_fms_vehicle_id()

        self.assertEqual(move.partner_id, self.customer)


class TestVehicleCustomerMismatch(NativeIntegrationBase):

    def test_vehicle_customer_mismatch_raises(self):
        """Constraint blocks invoice where vehicle.partner != invoice partner."""
        vehicle = self._make_vehicle(partner=self.customer)

        with self.assertRaises(ValidationError):
            self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': self.other_customer.id,
                'fms_vehicle_id': vehicle.id,
            })

    def test_vehicle_customer_match_ok(self):
        """Invoice with matching vehicle.partner passes constraint."""
        vehicle = self._make_vehicle(partner=self.customer)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
            'fms_vehicle_id': vehicle.id,
        })
        self.assertEqual(move.fms_vehicle_id, vehicle)

    def test_driver_customer_mismatch_raises(self):
        """Constraint blocks invoice where driver.partner != invoice partner."""
        driver = self._make_driver(partner=self.customer)

        with self.assertRaises(ValidationError):
            self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': self.other_customer.id,
                'fms_driver_id': driver.id,
            })


class TestOdometerField(NativeIntegrationBase):

    def test_odometer_stored_on_invoice(self):
        """fms_odometer is saved and readable."""
        vehicle = self._make_vehicle()
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
            'fms_vehicle_id': vehicle.id,
            'fms_odometer': 152340.5,
        })
        self.assertAlmostEqual(move.fms_odometer, 152340.5, places=1)
